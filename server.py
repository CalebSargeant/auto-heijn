#!/usr/bin/env python3
"""auto-heijn server: match shopping-list items to Albert Heijn products and fill the basket.

The Mac bridge posts open reminders to /api/sync. New item names are matched with AH's
product search and an LLM. Matched items go into the AH basket through the app API, the
server posts what it added to Slack, and it answers with the reminder ids to tick.
"""
import base64
import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = os.environ.get
DATABASE_URL = ENV("DATABASE_URL", "auto-heijn.db")
WRITE = ENV("AH_WRITE", "false").lower() == "true"
PUBLIC_URL = ENV("PUBLIC_URL", "").rstrip("/")
LLM_URL = ENV("LLM_URL", "http://litellm:4000").rstrip("/")
LLM_MODEL = ENV("LLM_MODEL", "agent-light")
SLACK_CHANNEL = ENV("SLACK_CHANNEL", "")

API = "https://api.ah.nl"
CLIENT = "appie-ios"
LOGIN_URL = f"https://login.ah.nl/login?client_id={CLIENT}&response_type=code&redirect_uri=appie://login-exit"
BASKET = "basket { itemsInList { id quantity product { id } } itemsInOrder { id } summary { shoppingType deliveryDate } }"
# The search API answers 500 without x-application.
HEADERS = {"User-Agent": "auto-heijn (personal shopping-list sync)", "x-application": "AHWEBSHOP"}
SCHEMA = [
    "create table if not exists kv (k text primary key, v text)",
    "create table if not exists products (name text primary key, id bigint, label text, sure integer,"
    " source text, told integer default 0, ts double precision)",
    "create table if not exists added (reminder text primary key, name text, id bigint, qty integer,"
    " ts double precision)",
    "create table if not exists specials (id bigint, week text, ts text, label text, added integer default 0,"
    " primary key (id, week))",
]
QUANTITY = [re.compile(p, re.I) for p in (
    r"(?P<n>\d+)\s*[x×]\s+(?P<name>.+)",
    r"(?P<name>.+?)\s+[x×]\s*(?P<n>\d+)",
    r"(?P<name>.+?)\s+(?P<n>\d+)\s*[x×]",
)]
MATCH = """You match one entry from a Dutch household shopping list to a product at Albert Heijn.
You get the entry, the household's preferences, and numbered candidates from AH's product search with
their price, bonus, Nutri-Score and nutrition per 100 g or ml where AH has them.
Pick the product the household most likely means, using these rules in order:
1. It must be the thing on the list. Keep any brand, variety, flavour, fat level or size the entry names.
2. Follow the household's preferences, even when another product is cheaper or healthier.
3. Price: pick the lowest price per kg, litre or piece, counting bonus prices. A bigger pack or a
   multipack is fine when it is cheaper per unit.
4. Health: among products at about the same price per unit, prefer a better Nutri-Score (A best, E worst).
   With equal scores, prefer more protein, then fat over carbohydrates, then fewer kcal.
5. Then prefer a product bought before.
Answer with JSON only: {"pick": <candidate number or null>, "sure": <true or false>, "search": "<Dutch search words>", "why": "<a few words>"}
pick is null when no candidate is the thing on the list; then give search words that would find it.
sure is false when the entry is ambiguous or no candidate clearly fits."""
STALE = 7 * 86400  # prices and bonuses change weekly, so a pick older than this is made again
TICKS = {"shopping_trolley", "white_check_mark", "heavy_check_mark", "+1"}
NUTRIENTS = {"ENER-": "energy", "PRO-": "protein", "FAT": "fat", "CHOAVL": "carbs", "SUGAR-": "sugar"}
LOCK = threading.Lock()


class LoggedOut(Exception):
    pass


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def parse_item(title):
    """'2x melk', 'melk x2' and 'melk 2x' all mean ('melk', 2)."""
    text = " ".join(title.split())
    for pattern in QUANTITY:
        m = pattern.fullmatch(text)
        if m and int(m["n"]) > 0:
            return m["name"], int(m["n"])
    return text, 1


def connect():
    if DATABASE_URL.startswith("postgres"):
        import psycopg
        return closing(psycopg.connect(DATABASE_URL))
    return closing(sqlite3.connect(DATABASE_URL, timeout=30))  # tests and local runs


def sql(con, query, args=()):
    if isinstance(con, sqlite3.Connection):
        query = query.replace("%s", "?")
    return con.execute(query, args)


def get(con, key):
    row = sql(con, "select v from kv where k = %s", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def put(con, key, value):
    if value is None:
        sql(con, "delete from kv where k = %s", (key,))
    else:
        sql(con, "insert into kv values (%s, %s) on conflict (k) do update set v = excluded.v",
            (key, json.dumps(value)))
    con.commit()


def http_json(url, body=None, headers=(), method=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else None


def ah(path, body=None, token=None, method=None):
    headers = dict(HEADERS, **({"Authorization": "Bearer " + token} if token else {}))
    return http_json(API + path, body, headers, method)


_anon = {"token": "", "until": 0.0}


def anon_token():
    if time.time() > _anon["until"]:
        tok = ah("/mobile-auth/v1/auth/token/anonymous", {"clientId": CLIENT})
        _anon.update(token=tok["access_token"], until=time.time() + tok.get("expires_in", 3600) - 60)
    return _anon["token"]


def save_tokens(con, tok, old_refresh=None):
    put(con, "ah_tokens", {"access": tok["access_token"],
                           "refresh": tok.get("refresh_token") or old_refresh,
                           "expires": time.time() + tok.get("expires_in", 3600) - 60})


def login(con, code):
    save_tokens(con, ah("/mobile-auth/v1/auth/token", {"clientId": CLIENT, "code": code}))


def code_from(pasted):
    """The login code from a pasted appie:// address, a pasted ingelogd.json response, or the bare code."""
    found = re.search(r"code=([^&\s\"'\\]+)", pasted) or re.fullmatch(r"\s*(\S+)\s*", pasted)
    return urllib.parse.unquote(found[1]) if found else ""


def installation_id(con):
    """AH's app sends a fraud-detection installation id with its login; keep one per server."""
    fdii = get(con, "fdii")
    if not fdii:
        fdii = str(uuid.uuid4()).upper()
        put(con, "fdii", fdii)
    return fdii


def user_token(con):
    saved = get(con, "ah_tokens")
    if not saved:
        raise LoggedOut("not logged in")
    if time.time() < saved["expires"]:
        return saved["access"]
    try:
        tok = ah("/mobile-auth/v1/auth/token/refresh", {"clientId": CLIENT, "refreshToken": saved["refresh"]})
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):  # the refresh token is dead; a 403 is more likely Akamai, so keep it
            put(con, "ah_tokens", None)
            raise LoggedOut(f"AH refused the saved login (HTTP {e.code})") from e
        raise
    save_tokens(con, tok, saved["refresh"])
    return tok["access_token"]


def graphql(token, query, variables=None):
    out = ah("/graphql", {"query": query, "variables": variables or {}}, token)
    if out.get("errors") or not out.get("data"):
        raise ValueError("AH said: " + ((out.get("errors") or [{}])[0].get("message") or "no data"))
    return out["data"]


def label(p):
    return f'{p["title"]} ({p.get("salesUnitSize", "?")})'


def search(query, token=None, size=20):
    """Orderable hits; with a user token AH also marks what the household bought before."""
    q = urllib.parse.urlencode({"query": query, "page": 0, "size": size, "sortOn": "RELEVANCE"})
    hits = ah("/mobile-services/product/search/v2?" + q, token=token or anon_token()).get("products", [])
    return [hit(p) for p in hits if p.get("isOrderable")]


def hit(p):
    deal = bool(p.get("isBonus") and p.get("currentPrice"))  # currentPrice is 0 off bonus
    return {"id": p["webshopId"], "label": label(p), "bought": p.get("isPreviouslyBought") is True,
            "price": p["currentPrice"] if deal else p.get("priceBeforeBonus"),
            "bonus": p.get("bonusMechanism") if deal else None, "until": p.get("bonusEndDate") if deal else None,
            "nutriscore": p.get("nutriscore")}


def products_by_id(ids):
    out = []
    for i in range(0, len(ids), 50):
        q = urllib.parse.urlencode([("ids", pid) for pid in ids[i:i + 50]] + [("sortOn", "INPUT_PRODUCT_IDS")])
        out += [hit(p) for p in ah("/mobile-services/product/search/v2/products?" + q, token=anon_token()) or []]
    return out


def lookup(pid):
    return next((h["label"] for h in products_by_id([pid]) if h["id"] == pid), f"product {pid}")


def nutrition(ids):
    """{product id: {"protein": "4.3 g", ...}} per 100 g or ml, in one batched GraphQL query."""
    if not ids:
        return {}
    fields = "tradeItem { nutritions { nutrients { type value } } }"
    query = "query Nutrition { " + " ".join(f"p{int(i)}: product(id: {int(i)}) {{ {fields} }}" for i in ids) + " }"
    data = (ah("/graphql", {"query": query}, anon_token()) or {}).get("data") or {}
    out = {}
    for key, p in data.items():
        tables = ((p or {}).get("tradeItem") or {}).get("nutritions") or []
        if tables:  # the first table is per 100 g or ml, later ones per portion
            out[int(key[1:])] = {NUTRIENTS[n["type"]]: n["value"] for n in tables[0].get("nutrients") or []
                                 if n.get("type") in NUTRIENTS}
    return out


def candidates(hits, facts):
    rows = []
    for n, h in enumerate(hits, 1):
        parts = [f'{n}. {h["label"]}']
        if h.get("price") is not None:
            parts.append(f'€{h["price"]:.2f}' + (f' on bonus ({h["bonus"]})' if h.get("bonus") else ""))
        if h.get("nutriscore"):
            parts.append(f'Nutri-Score {h["nutriscore"]}')
        if facts.get(h["id"]):
            parts.append(", ".join(f"{k} {v}" for k, v in facts[h["id"]].items()))
        if h.get("bought"):
            parts.append("bought before")
        rows.append(" | ".join(parts))
    return "\n".join(rows) or "(none)"


def basket(token):
    """The basket and its product lines as {product id: quantity}."""
    raw = graphql(token, "query Basket { " + BASKET + " }")["basket"]
    if not isinstance(raw, dict):
        raise ValueError("AH sent no basket")
    return raw, {(i.get("product") or {}).get("id") or i["id"]: i["quantity"] for i in raw.get("itemsInList") or []}


def booked(token, raw):
    """Why adding now could change a placed or half-booked order, or '' when there is none.

    Any one sign of an order is enough: with a slot booked, AH puts new items in that order.
    """
    summary = raw.get("summary") or {}
    if raw.get("itemsInOrder") is not None or summary.get("shoppingType") or summary.get("deliveryDate"):
        return f'an order is open for {summary.get("deliveryDate") or "a chosen slot"}'
    try:
        order = ah("/mobile-services/order/v1/summaries/active?sortBy=DEFAULT", token=token)
    except urllib.error.HTTPError as e:
        if e.code != 404:  # anything but "no order" stops the write
            raise
        order = None
    if order and order.get("id"):
        return f'order {order["id"]} is {order.get("state") or "open"}'
    placed = graphql(token, "query Placed { orderFulfillments(status: OPEN) { result { orderId"
                            " delivery { slot { date } } } } }")["orderFulfillments"]
    for f in (placed or {}).get("result") or []:
        return f'a delivery is booked for {((f.get("delivery") or {}).get("slot") or {}).get("date", "a slot")}'
    return ""


def add_items(token, quantities):
    """Add these quantities to the basket lines (basketItemsAdd adds, it doesn't set)."""
    out = graphql(token, "mutation Add($items: [BasketMutation!]!) { basketItemsAdd(items: $items)"
                         " { status errorMessage } }",
                  {"items": [{"id": pid, "quantity": qty} for pid, qty in quantities.items()]})["basketItemsAdd"]
    if (out or {}).get("status") not in (None, "", "SUCCESS"):
        raise ValueError(f'AH refused the items: {out.get("errorMessage") or out["status"]}')


def llm(system, prompt):
    out = http_json(LLM_URL + "/v1/chat/completions", {"model": LLM_MODEL, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": prompt}]},
        {"Authorization": "Bearer " + ENV("LLM_API_KEY", "")}, timeout=90)
    text = out["choices"][0]["message"].get("content") or ""
    found = re.search(r"\{.*\}", text, re.S)
    return json.loads(found.group()) if found else {}


def match(name, token=None, prefs=()):
    """Best AH product for a list entry, as {id, label, sure, why}; id None when nothing fits."""
    query = name
    wants = "\n".join(f"- {p}" for p in prefs) or "(none)"
    for _ in range(2):
        hits = search(query, token)
        try:
            facts = nutrition([h["id"] for h in hits])
        except (OSError, ValueError, KeyError) as e:
            log(f"no nutrition for {query!r}: {e}")
            facts = {}
        answer = llm(MATCH, f'Entry: "{name}"\nHousehold preferences:\n{wants}\nCandidates:\n'
                     + candidates(hits, facts))
        pick = answer.get("pick")
        if isinstance(pick, int) and 1 <= pick <= len(hits):
            return dict(hits[pick - 1], sure=answer.get("sure") is True, why=str(answer.get("why") or ""))
        query = str(answer.get("search") or "").strip()
        if not query or query.casefold() == name.casefold():
            break
    return {"id": None, "label": "no match", "sure": False}


def product(con, name, token=None):
    key = name.casefold()
    row = sql(con, "select id, label, sure, source, told, ts from products where name = %s", (key,)).fetchone()
    if row and (row[3] == "manual" or time.time() - row[5] < STALE):
        return dict(zip(("id", "label", "sure", "source", "told"), row))
    found = match(name, token, preferences(con))
    save_product(con, key, found["id"], found["label"], found["sure"], "llm")
    log(f"matched {name!r}: {found['label']}{'' if found['sure'] else ' (not sure)'} {found.get('why', '')}")
    return dict(found, source="llm", told=0)


def preferences(con, add=None, remove=None):
    """The household's standing preferences, which beat price and health when matching."""
    prefs = get(con, "preferences") or []
    if add is None and remove is None:
        return prefs
    if add:
        prefs.append(" ".join(str(add).split()))
    if remove is not None:
        if not 1 <= int(remove) <= len(prefs):
            raise ValueError(f"there is no preference {remove}")
        prefs.pop(int(remove) - 1)
    put(con, "preferences", prefs)
    sql(con, "update products set ts = 0 where source = 'llm'")  # pick again with the new preferences
    con.commit()
    return prefs


def save_product(con, key, pid, label, sure, source):
    sql(con, "insert into products values (%s, %s, %s, %s, %s, 0, %s) on conflict (name) do update set"
             " id = excluded.id, label = excluded.label, sure = excluded.sure, source = excluded.source,"
             " told = 0, ts = excluded.ts", (key, pid, label, int(sure), source, time.time()))
    con.commit()


def set_mapping(con, name, pid):
    text = "not from AH" if pid is None else lookup(pid)
    save_product(con, name.casefold(), pid, text, True, "manual")
    return text


def slack_call(method, body=None, query=None):
    """Call a Slack Web API method for the channel; None when Slack is off or says no."""
    token = ENV("SLACK_BOT_TOKEN")
    if not (token and SLACK_CHANNEL):
        log(f"slack not set up: {method} {body or query}")
        return None
    url = "https://slack.com/api/" + method
    if query is not None:  # read methods take form parameters, not JSON
        url += "?" + urllib.parse.urlencode(dict(query, channel=SLACK_CHANNEL))
    try:
        out = http_json(url, None if query is not None else dict(body or {}, channel=SLACK_CHANNEL),
                        {"Authorization": "Bearer " + token})
    except OSError as e:
        log(f"slack {method} failed: {e}")
        return None
    if not out.get("ok"):
        log(f"slack {method} refused: {out.get('error')}")
        return None
    return out


def slack(text, thread=None):
    """Post to the channel, or into a thread, and return the message's ts."""
    body = {"text": text, "unfurl_links": False, **({"thread_ts": thread} if thread else {})}
    return (slack_call("chat.postMessage", body) or {}).get("ts")


_bot = {}


def bot_user():
    if not _bot.get("id"):
        _bot["id"] = (slack_call("auth.test") or {}).get("user_id")
    return _bot["id"]


def week():
    return time.strftime("%G-W%V")  # AH's bonus week runs Monday to Sunday, like the ISO week


def offer_specials(con):
    """Once a bonus week, post the household's usual products that are on bonus, each with a 🛒 to tap."""
    if get(con, "specials:week") == week():
        return
    ids = [row[0] for row in sql(con, "select distinct id from products where id is not null and sure = 1")]
    soon = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 14 * 86400))
    deals = [h for h in products_by_id(ids) if h["bonus"] and (h["until"] or "9") <= soon]  # skips standing deals
    if deals:
        thread = slack("On bonus this week, from things that were on your list. "
                       "Tap 🛒 on one and I'll add it to the AH basket.")
        if not thread:
            return  # try again next run
        for h in deals:
            ts = slack(f'{h["label"]}: {h["bonus"]}, €{h["price"]:.2f}, until {h["until"]}', thread)
            if ts:
                slack_call("reactions.add", {"name": "shopping_trolley", "timestamp": ts})
                sql(con, "insert into specials values (%s, %s, %s, %s, 0) on conflict do nothing",
                    (h["id"], week(), ts, h["label"]))
        put(con, "specials:thread", thread)
    put(con, "specials:week", week())


def accepted_specials(con):
    """This week's specials someone ticked in Slack that aren't in the basket yet, as (id, ts, label)."""
    rows = sql(con, "select id, ts, label from specials where week = %s and added = 0", (week(),)).fetchall()
    thread = get(con, "specials:thread")
    me = bot_user() if rows and thread else None
    if not me:  # without the bot's own id its 🛒 would count as a yes
        return []
    replies = (slack_call("conversations.replies", query={"ts": thread}) or {}).get("messages", [])
    ticked = {m["ts"] for m in replies for r in m.get("reactions", [])
              if r.get("name") in TICKS and set(r.get("users", [])) - {me}}
    return [row for row in rows if row[1] in ticked]


HELP = """Post one of these here:
search pindakaas: AH products with their ids
map pindakaas 220739: always use that product (map shampoo none: not from AH)
prefer Hellmann's for mayonnaise: a preference that beats price and health
preferences: list the preferences
unprefer 2: remove preference 2"""
COMMAND = re.compile(r"(search|map|prefer|preferences|unprefer|help)\b\s*(.*)", re.I | re.S)


def command(con, text, token=None):
    """The answer to one message in the channel, or None when it isn't a command."""
    found = COMMAND.fullmatch(text)
    if not found:
        return None
    verb, rest = found[1].lower(), found[2].strip()
    mapping = re.fullmatch(r"(.+?)\s+(\d+|none)", rest, re.I)
    if verb == "search" and rest:
        return "\n".join(f'{h["id"]} {h["label"]}' for h in search(rest, token)[:10]) or "Nothing found."
    if verb == "map" and mapping:
        pid = None if mapping[2].lower() == "none" else int(mapping[2])
        return f"{mapping[1]}: {set_mapping(con, mapping[1], pid)}"
    if verb == "prefer" and rest:
        prefs = preferences(con, add=rest)
    elif verb == "unprefer" and rest.isdigit():
        prefs = preferences(con, remove=int(rest))
    elif verb == "preferences" and not rest:
        prefs = preferences(con)
    else:
        return HELP
    return "\n".join(f"{n}. {p}" for n, p in enumerate(prefs, 1)) or "No preferences yet."


def commands(con, token=None):
    """Answer what people posted in the channel since the last run, each in its own thread."""
    seen = get(con, "slack:seen")
    if not seen:
        put(con, "slack:seen", f"{time.time():.6f}")  # start now rather than answer old messages
        return
    # shortcut: reads at most 100 new messages per run, plenty for one household's channel
    out = slack_call("conversations.history", query={"oldest": seen, "limit": 100}) or {}
    for m in sorted(out.get("messages", []), key=lambda m: float(m["ts"])):
        if not (m.get("bot_id") or m.get("subtype")):
            try:
                answer = command(con, html.unescape(m.get("text", "")).strip(), token)
            except (OSError, ValueError, KeyError) as e:
                answer = f"That didn't work: {e}"
            if answer:
                slack(answer, m["ts"])
        put(con, "slack:seen", m["ts"])


def alert(con, kind, text):
    """Post text once, and again only when it changes."""
    if get(con, "alert:" + kind) != text:
        slack(text)
        put(con, "alert:" + kind, text)


def describe(line):
    titles = " + ".join(i["title"] for i in line["items"]) or "on bonus"
    return f'• {titles}: {line["label"]}' + (f' ×{line["qty"]}' if line["qty"] > 1 else "")


def sync(items, dry_run=False):
    """Returns (reminder ids now in the basket, log lines for the bridge)."""
    notes = []
    with LOCK, connect() as con:
        added = {row[0] for row in sql(con, "select reminder from added").fetchall()}
        complete = [i["id"] for i in items if i["id"] in added]
        try:
            token, offline = user_token(con), ""
        except LoggedOut as e:
            token, offline = None, f"auto-heijn can't reach your AH account ({e}). Log in at {PUBLIC_URL}/login."
        except (OSError, ValueError, KeyError) as e:
            token, offline = None, f"AH didn't answer ({e})."
        if not dry_run:
            try:
                commands(con, token)
            except (OSError, ValueError, KeyError) as e:
                notes.append(f"slack commands failed: {e}")
        lines, unsure = {}, []
        for item in items:
            if item["id"] in added:
                continue
            name, qty = parse_item(item["title"])
            try:
                found = product(con, name, token)
            except (OSError, ValueError, KeyError) as e:
                notes.append(f"matching {name!r} failed, trying again next run: {e}")
                continue
            if found["id"] is None and found["source"] == "manual":
                continue  # marked as not from AH
            if found["id"] is None or not found["sure"]:
                unsure.append((name, found))
                continue
            line = lines.setdefault(found["id"], {"label": found["label"], "qty": 0, "items": []})
            line["qty"] += qty
            line["items"].append(dict(item, name=name, qty=qty))

        new_unsure = [(n, f) for n, f in unsure if not f["told"]]
        if new_unsure and not dry_run:
            slack("Not sure about these, so they stay on the list:\n" + "\n".join(
                f'• {n}: ' + (f'best guess {f["label"]} ({f["id"]})' if f["id"] else "no match")
                for n, f in new_unsure) + "\nFix one by posting: map NAME ID")
            for n, _ in new_unsure:
                sql(con, "update products set told = 1 where name = %s", (n.casefold(),))
            con.commit()
        if not dry_run:
            try:
                offer_specials(con)
                for pid, ts, text in accepted_specials(con):
                    line = lines.setdefault(pid, {"label": text, "qty": 0, "items": []})
                    line["qty"] += 1
                    line.setdefault("specials", []).append(ts)
            except (OSError, ValueError, KeyError) as e:
                notes.append(f"specials failed: {e}")
        if not lines:
            return complete, notes

        plan = "\n".join(describe(line) for line in lines.values())
        if dry_run:
            return complete, notes + ["would add:\n" + plan]
        if offline:
            alert(con, "write", offline + " The items stay on the list.")
            return complete, notes + [offline]
        try:
            raw, before = basket(token)
            why = booked(token, raw)
            if why:
                alert(con, "write", f"Not adding to the AH basket: {why}. The items stay on the list.")
                return complete, notes + [why]
            if not WRITE:
                alert(con, "read-only", f"Read-only mode. Your AH basket has {len(before)} products. "
                                        "I would add:\n" + plan)
                return complete, notes + ["read-only, would add:\n" + plan]
            add_items(token, {pid: line["qty"] for pid, line in lines.items()})
            _, after = basket(token)
        except (OSError, ValueError, KeyError) as e:
            alert(con, "write", f"Adding to the AH basket failed ({e}). I'll try again.")
            return complete, notes + [f"AH write failed: {e}"]

        put(con, "alert:write", None)
        want = {pid: before.get(pid, 0) + line["qty"] for pid, line in lines.items()}
        done = [line for pid, line in lines.items() if after.get(pid, 0) >= want[pid]]
        missing = [line for pid, line in lines.items() if after.get(pid, 0) < want[pid]]
        for pid, line in lines.items():
            if line not in done:
                continue
            for i in line["items"]:
                sql(con, "insert into added values (%s, %s, %s, %s, %s) on conflict (reminder) do nothing",
                    (i["id"], i["name"], pid, i["qty"], time.time()))
            for ts in line.get("specials", []):
                sql(con, "update specials set added = 1 where ts = %s", (ts,))
                slack_call("reactions.add", {"name": "white_check_mark", "timestamp": ts})
        con.commit()
        text = "Added to the AH basket:\n" + "\n".join(map(describe, done)) if done else ""
        if done:
            slack(text)
        if missing:  # retried every run, so say it once
            text += "\nAH didn't take these, so they stay on the list:\n" + "\n".join(map(describe, missing))
            alert(con, "missing", text.strip())
        else:
            put(con, "alert:missing", None)
        complete += [i["id"] for line in done for i in line["items"]]
        return complete, notes + [text.strip()]


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>auto-heijn</title><style>body{{font:16px system-ui;max-width:40em;margin:2em auto;padding:0 1em}}
input{{width:100%;font:inherit}}code{{word-break:break-all}}</style>
<h1>auto-heijn</h1><p>{status}</p>
<h2>Log in to AH</h2><ol>
<li>On a computer, open this page in a private window so AH asks for your login. Open the developer
tools on the Network tab and turn on Preserve log.</li>
<li>In that window, open <a href="{login}" rel="noreferrer">the AH login</a> and log in.</li>
<li>AH then says you are logged in. In the Network tab, filter on <code>ingelogd</code>, open the first
<code>ingelogd.json</code> response and copy the address in it that starts with
<code>appie://login-exit?code=</code>. If the browser shows that address itself, copy it from there.</li>
<li>Paste it here within a minute:</li></ol>
<form method="post" action="/login"><p><input name="code" autofocus></p><p><button>Log in</button></p></form>"""


def status(con):
    if not get(con, "ah_tokens"):
        return "Not logged in to AH."
    try:
        token = user_token(con)
        raw, lines = basket(token)
        why = booked(token, raw)
    except (LoggedOut, OSError, ValueError, KeyError) as e:
        return f"Logged in, but AH didn't answer: {e}"
    return (f"Logged in to AH. Basket: {len(lines)} products. "
            + (f"Booked: {why}. " if why else "No booked delivery. ")
            + ("Writes are on." if WRITE else "Read-only: nothing is added to the basket."))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def log_message(self, fmt, *args):
        if not self.path.startswith("/healthz"):
            log(f"{self.address_string()} {fmt % args}")

    def send(self, code, body, kind="application/json", headers=()):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        for key, value in [("Content-Type", kind), ("Content-Length", str(len(data))), *headers]:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def authorised(self):
        given = self.headers.get("Authorization", "")
        if given.startswith("Basic "):
            given = base64.b64decode(given[6:]).decode(errors="replace").partition(":")[2]
        else:
            given = given.removeprefix("Bearer ")
        return hmac.compare_digest(given.encode(), ENV("BRIDGE_TOKEN", "").encode()) and bool(given)

    def route(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if not self.authorised():
            if url.path.startswith("/api/"):
                return self.send(401, {"error": "unauthorised"})
            return self.send(401, "Log in with any user name and the bridge token.", "text/plain",
                             [("WWW-Authenticate", 'Basic realm="auto-heijn"')])
        try:
            if url.path == "/api/sync" and self.command == "POST":
                req = json.loads(self.body() or b"{}")
                complete, notes = sync(req.get("items", []), bool(req.get("dry_run")))
                return self.send(200, {"complete": complete, "log": notes})
            if url.path in ("/", "/login") and self.command == "GET":
                with LOCK, connect() as con:  # LOCK: AH rotates the refresh token, so refresh one at a time
                    page = PAGE.format(status=html.escape(status(con)),
                                       login=html.escape(LOGIN_URL + "&fdii=" + installation_id(con)))
                return self.send(200, page, "text/html; charset=utf-8")
            if url.path == "/login" and self.command == "POST":
                code = code_from(urllib.parse.parse_qs(self.body().decode()).get("code", [""])[0])
                if not code:
                    return self.send(400, "No login code in what you pasted.", "text/plain")
                with LOCK, connect() as con:
                    login(con, code)
                    put(con, "alert:write", None)
                return self.send(303, "", "text/plain", [("Location", "/")])
            return self.send(404, {"error": "not found"})
        except (OSError, ValueError, KeyError) as e:
            log(f"{url.path} failed: {e!r}")
            return self.send(502, {"error": str(e)})


def main():
    with connect() as con:
        for statement in SCHEMA:
            sql(con, statement)
        con.commit()
    log(f"listening, writes {'on' if WRITE else 'off'}")
    ThreadingHTTPServer(("", int(ENV("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
