#!/usr/bin/python3
"""Keep an Albert Heijn add-to-list link in an Apple Reminders list.

Open items in the list become one reminder, "Add N items to AH", whose URL adds
them to Mijn lijst on ah.nl. Ticking that reminder marks the items it carried
done. Nothing here logs in to AH or touches orders. See README.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REMINDCTL = os.environ.get("REMINDCTL", "/opt/homebrew/bin/remindctl")
CONFIG = Path(os.environ.get("AUTO_HEIJN_CONFIG", "~/.config/auto-heijn/config.json")).expanduser()
STATE = Path.home() / "Library/Application Support/auto-heijn/state.json"

# Double slash on purpose: iOS hands /mijnlijst/add-multiple to the AH app, which adds nothing.
LINK_BASE = "https://www.ah.nl/mijnlijst//add-multiple?"
# shortcut: 25 is the limit others report, not measured here; raise it after a bigger test.
MAX_PER_LINK = 25
API = "https://api.ah.nl"
# The search API answers 500 without x-application.
HEADERS = {"User-Agent": "auto-heijn (personal shopping-list sync)", "x-application": "AHWEBSHOP"}

QUANTITY = [re.compile(p, re.I) for p in (
    r"(?P<n>\d+)\s*[x×]\s+(?P<name>.+)",
    r"(?P<name>.+?)\s+[x×]\s*(?P<n>\d+)",
    r"(?P<name>.+?)\s+(?P<n>\d+)\s*[x×]",
)]


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


def remindctl(*args, timeout=120):
    out = subprocess.run([REMINDCTL, *args, "--json", "--no-input"],
                         capture_output=True, encoding="utf-8", timeout=timeout)
    if out.returncode:
        raise RuntimeError(f"remindctl {args[0]} failed: {(out.stderr or out.stdout).strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def ah(path, body=None, token=None):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def anon_token():
    return ah("/mobile-auth/v1/auth/token/anonymous", {"clientId": "appie"})["access_token"]


def search(name, token, size=10):
    """Orderable hits of AH's product search, best first, as {id, label}."""
    query = urllib.parse.urlencode({"query": name, "page": 0, "size": size, "sortOn": "RELEVANCE"})
    hits = ah("/mobile-services/product/search/v2?" + query, token=token).get("products", [])
    return [{"id": p["webshopId"], "label": f'{p["title"]} ({p.get("salesUnitSize", "?")})'}
            for p in hits if p.get("isOrderable")]


def load_config():
    """'list' names the Reminders list; 'products' maps item names to AH product ids, null for 'not from AH'."""
    raw = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    products = {name.casefold(): pid for name, pid in raw.get("products", {}).items()}
    return raw.get("list", "Shopping List"), products


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"links": {}, "guesses": {}}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def plan(items, products, guesses):
    """Group open items into link reminders of at most MAX_PER_LINK products."""
    lines, guessed, unmatched = {}, [], []
    for r in items:
        name, qty = parse_item(r["title"])
        key = name.casefold()
        if key in products:
            pid = products[key]
            if pid is None:
                continue
        elif guesses.get(key):
            pid = guesses[key]["id"]
            guessed.append(f'- {r["title"]}: {guesses[key]["label"]}')
        else:
            unmatched.append(f'- {r["title"]}')
            continue
        line = lines.setdefault(pid, {"qty": 0, "items": []})
        line["qty"] += qty
        line["items"].append(r["id"])

    pids = list(lines)
    groups = [pids[i:i + MAX_PER_LINK] for i in range(0, len(pids), MAX_PER_LINK)]
    chunks = []
    for n, group in enumerate(groups, 1):
        items_in = [i for pid in group for i in lines[pid]["items"]]
        title = f'Add {len(items_in)} item{"s" if len(items_in) > 1 else ""} to AH'
        if len(groups) > 1:
            title += f" ({n}/{len(groups)})"
        notes = ["Tap the link with no delivery slot booked, then tick this reminder. "
                 "Ticking marks the items in the link done."]
        if n == 1 and guessed:
            notes += ["", "Guessed, fix under products in config.json if wrong:", *guessed]
        if n == 1 and unmatched:
            notes += ["", "Not in the link, no AH match:", *unmatched]
        url = LINK_BASE + "&".join(f'p={pid}:{lines[pid]["qty"]}' for pid in group)
        chunks.append({"url": url, "title": title, "notes": "\n".join(notes), "items": items_in})
    return chunks


def sync(dry_run=False):
    def write(cmd, *args):
        what = args[args.index("--title") + 1] if "--title" in args else " ".join(a for a in args if a[:2] != "--")
        log(f"{'would ' if dry_run else ''}{cmd}: {what}")
        return None if dry_run else remindctl(cmd, *args)

    if remindctl("status")["status"] == "not-determined":
        log("waiting for you to answer the macOS prompt to let Python use Reminders")
        remindctl("authorize", timeout=None)  # the prompt can sit unanswered for hours

    shopping_list, products = load_config()
    state = load_state()
    links = state["links"]
    reminders = remindctl("show", "all", "--list", shopping_list)
    by_id = {r["id"]: r for r in reminders}

    done = []
    for lid in list(links):
        r = by_id.get(lid)
        if r is not None and not r["isCompleted"]:
            continue
        if r is not None:  # ticked, so these items went to AH
            done += [i for i in links[lid]["items"] if i in by_id and not by_id[i]["isCompleted"]]
        del links[lid]
    if done:
        write("complete", *done)
        log(f"completed {len(done)} items: " + ", ".join(by_id[i]["title"] for i in done))

    is_link = [r for r in reminders if (r.get("url") or "").startswith(LINK_BASE)]
    for r in is_link:
        if r["id"] not in links and not r["isCompleted"]:  # left over from a lost state file
            write("delete", "--force", r["id"])
    skip = set(done) | {r["id"] for r in is_link}
    items = [r for r in reminders if not r["isCompleted"] and r["id"] not in skip]

    guesses, token = state["guesses"], None
    for r in items:
        name = parse_item(r["title"])[0]
        if name.casefold() in products or name.casefold() in guesses:
            continue
        try:
            token = token or anon_token()
            hits = search(name, token)
        except (OSError, ValueError, KeyError) as e:
            log(f"AH search failed, retrying next run: {e}")
            break
        guesses[name.casefold()] = hits[0] if hits else None
        log(f"guess {name!r}: {hits[0]['label'] if hits else 'no match'}")

    chunks = plan(items, products, guesses)
    current = list(links.items())
    for n, chunk in enumerate(chunks):
        fields = ["--title", chunk["title"], "--notes", chunk["notes"], "--url", chunk["url"]]
        if n < len(current):
            lid, old = current[n]
            if [old[k] for k in ("url", "title", "notes")] != [chunk[k] for k in ("url", "title", "notes")]:
                write("edit", lid, *fields)
        else:
            new = write("add", "--list", shopping_list, *fields)
            lid = new["id"] if new else f"dry-run-{n}"
        links[lid] = chunk
        if dry_run:
            log(f'{chunk["title"]}: {chunk["url"]}\n{chunk["notes"]}')
    for lid, _ in current[len(chunks):]:
        write("delete", "--force", lid)
        del links[lid]

    if not dry_run:
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument("--search", metavar="NAME", help="list AH product ids for NAME, for config.json")
    args = parser.parse_args()
    if args.search:
        for hit in search(args.search, anon_token()):
            print(hit["id"], hit["label"])
        return
    sync(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
