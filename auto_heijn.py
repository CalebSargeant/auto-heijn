#!/usr/bin/python3
"""Send an Apple Reminders list to an auto-heijn server and tick what it put in the AH basket.

The server matches items to Albert Heijn products and fills the basket. This side only reads
the list, passes it on, and ticks the reminders the server confirms. See README.md.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REMINDCTL = os.environ.get("REMINDCTL", "/opt/homebrew/bin/remindctl")
CONFIG = Path(os.environ.get("AUTO_HEIJN_CONFIG", "~/.config/auto-heijn/config.json")).expanduser()
KEYCHAIN = ["-s", "auto-heijn", "-a", "bridge-token"]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def remindctl(*args, timeout=120):
    out = subprocess.run([REMINDCTL, *args, "--json", "--no-input"],
                         capture_output=True, encoding="utf-8", timeout=timeout)
    if out.returncode:
        raise RuntimeError(f"remindctl {args[0]} failed: {(out.stderr or out.stdout).strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def load_config():
    raw = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    if not raw.get("server"):
        sys.exit(f'Set "server" in {CONFIG}, for example {{"server": "https://ah.example.com"}}')
    return raw.get("list", "Shopping List"), raw["server"].rstrip("/")


def token():
    out = subprocess.run(["/usr/bin/security", "find-generic-password", *KEYCHAIN, "-w"],
                         capture_output=True, encoding="utf-8")
    if out.returncode:
        sys.exit("No server token in the Keychain. Add it with: "
                 "security add-generic-password -U " + " ".join(KEYCHAIN) + " -w")
    return out.stdout.strip()


def call(path, body=None, timeout=300):
    """POST body (or GET without one) to the server's API and return its JSON answer."""
    _, server = load_config()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(server + path, data=data, headers={
        "Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def sync(dry_run=False):
    if remindctl("status")["status"] == "not-determined":
        log("waiting for you to answer the macOS prompt to let remindctl use Reminders")
        remindctl("authorize", timeout=None)  # the prompt can sit unanswered for hours

    shopping_list, _ = load_config()
    reminders = remindctl("show", "open", "--list", shopping_list)
    answer = call("/api/sync", {"items": [{"id": r["id"], "title": r["title"]} for r in reminders],
                                "dry_run": dry_run})
    for line in answer.get("log", []):
        log(line)
    done = answer.get("complete", [])
    if done and not dry_run:
        remindctl("complete", *done)
        titles = {r["id"]: r["title"] for r in reminders}
        log(f"ticked {len(done)}: " + ", ".join(titles.get(i, i) for i in done))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show what the server would do, change nothing")
    parser.add_argument("--search", metavar="NAME", help="list AH product ids for NAME")
    parser.add_argument("--map", nargs=2, metavar=("NAME", "ID"),
                        help="always use product ID for items called NAME (ID 'none': not from AH)")
    parser.add_argument("--prefer", metavar="TEXT",
                        help="add a preference that beats price and health, e.g. \"Hellmann's for mayonnaise\"")
    parser.add_argument("--preferences", action="store_true", help="list the preferences")
    parser.add_argument("--unprefer", type=int, metavar="N", help="remove preference N")
    args = parser.parse_args()
    if args.search:
        for hit in call("/api/search?" + urllib.parse.urlencode({"q": args.search}))["hits"]:
            print(hit["id"], hit["label"])
    elif args.prefer or args.preferences or args.unprefer is not None:
        body = {"add": args.prefer} if args.prefer else {"remove": args.unprefer} if args.unprefer is not None else None
        for n, text in enumerate(call("/api/preferences", body)["preferences"], 1):
            print(n, text)
    elif args.map:
        name, pid = args.map
        call("/api/mappings", {"name": name, "id": None if pid.lower() == "none" else int(pid)})
        print(f"{name}: {pid}")
    else:
        sync(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
