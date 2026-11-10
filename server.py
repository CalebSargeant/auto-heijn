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
]
QUANTITY = [re.compile(p, re.I) for p in (
    r"(?P<n>\d+)\s*[x×]\s+(?P<name>.+)",
    r"(?P<name>.+?)\s+[x×]\s*(?P<n>\d+)",
    r"(?P<name>.+?)\s+(?P<n>\d+)\s*[x×]",
)]
MATCH = """You match one entry from a Dutch household shopping list to a product at Albert Heijn.
You get the entry and numbered candidates from AH's product search; some are marked [bought before].
Pick the product the household most likely means:
- Keep any brand, variety, fat level, flavour or size the entry names.
- Otherwise prefer a product bought before, then AH's own brand, then the usual single-pack size.
- Pick a multipack only when the entry asks for several.
Answer with JSON only: {"pick": <candidate number or null>, "sure": <true or false>, "search": "<Dutch search words>"}
pick is null when no candidate is the thing on the list; then give search words that would find it.
sure is false when the entry is ambiguous or no candidate clearly fits."""
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
    return [{"id": p["webshopId"], "label": label(p), "bought": p.get("isPreviouslyBought") is True}
            for p in hits if p.get("isOrderable")]


def lookup(pid):
    hits = ah(f"/mobile-services/product/search/v2/products?ids={pid}&sortOn=INPUT_PRODUCT_IDS", token=anon_token())
    return next((label(p) for p in hits or [] if p.get("webshopId") == pid), f"product {pid}")


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


def match(name, token=None):
    """Best AH product for a list entry, as {id, label, sure}; id None when nothing fits."""
    query = name
    for _ in range(2):
        hits = search(query, token)
        lines = [f'{n}. {h["label"]}' + (" [bought before]" if h.get("bought") else "")
                 for n, h in enumerate(hits, 1)]
        answer = llm(MATCH, f'Entry: "{name}"\nCandidates:\n' + ("\n".join(lines) or "(none)"))
        pick = answer.get("pick")
        if isinstance(pick, int) and 1 <= pick <= len(hits):
            return dict(hits[pick - 1], sure=answer.get("sure") is True)
        query = str(answer.get("search") or "").strip()
        if not query or query.casefold() == name.casefold():
            break
    return {"id": None, "label": "no match", "sure": False}


def product(con, name, token=None):
    key = name.casefold()
    row = sql(con, "select id, label, sure, source, told from products where name = %s", (key,)).fetchone()
    if row:
        return dict(zip(("id", "label", "sure", "source", "told"), row))
    found = match(name, token)
    save_product(con, key, found["id"], found["label"], found["sure"], "llm")
    log(f"matched {name!r}: {found['label']}{'' if found['sure'] else ' (not sure)'}")
    return dict(found, source="llm", told=0)


def save_product(con, key, pid, label, sure, source):
    sql(con, "insert into products values (%s, %s, %s, %s, %s, 0, %s) on conflict (name) do update set"
             " id = excluded.id, label = excluded.label, sure = excluded.sure, source = excluded.source,"
             " told = 0, ts = excluded.ts", (key, pid, label, int(sure), source, time.time()))
    con.commit()


def set_mapping(con, name, pid):
    text = "not from AH" if pid is None else lookup(pid)
    save_product(con, name.casefold(), pid, text, True, "manual")
    return text


def slack(text):
    token = ENV("SLACK_BOT_TOKEN")
    if not (token and SLACK_CHANNEL):
        log("slack not set up: " + text)
        return
    try:
        out = http_json("https://slack.com/api/chat.postMessage",
                        {"channel": SLACK_CHANNEL, "text": text, "unfurl_links": False},
                        {"Authorization": "Bearer " + token})
        if not out.get("ok"):
            log(f"slack refused the post: {out.get('error')}")
    except OSError as e:
        log(f"slack post failed: {e}")


def alert(con, kind, text):
    """Post text once, and again only when it changes."""
    if get(con, "alert:" + kind) != text:
        slack(text)
        put(con, "alert:" + kind, text)


def describe(line):
    titles = " + ".join(i["title"] for i in line["items"])
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
                for n, f in new_unsure) + "\nFix one with: auto-heijn --map NAME ID")
            for n, _ in new_unsure:
                sql(con, "update products set told = 1 where name = %s", (n.casefold(),))
            con.commit()
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
            for i in line["items"] if line in done else []:
                sql(con, "insert into added values (%s, %s, %s, %s, %s) on conflict (reminder) do nothing",
                    (i["id"], i["name"], pid, i["qty"], time.time()))
        con.commit()
        text = "Added to the AH basket:\n" + "\n".join(describe(line) for line in done) if done else ""
        if missing:
            text += "\nAH didn't take these, so they stay on the list:\n" + "\n".join(map(describe, missing))
        slack(text.strip())
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
            if url.path == "/api/search":
                query = urllib.parse.parse_qs(url.query).get("q", [""])[0]
                return self.send(200, {"hits": search(query)})
            if url.path == "/api/mappings" and self.command == "POST":
                req = json.loads(self.body() or b"{}")
                pid = None if req.get("id") is None else int(req["id"])
                with LOCK, connect() as con:
                    return self.send(200, {"label": set_mapping(con, req["name"], pid)})
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
