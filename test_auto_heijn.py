import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auto_heijn as ah


class FakeReminders:
    """In-memory stand-in for the remindctl calls auto_heijn makes."""

    def __init__(self, *titles):
        self.items, self.next_id, self.lists_used = {}, 0, set()
        for title in titles:
            self.add(title)

    def add(self, title, url=None, notes=None):
        rid, self.next_id = f"ID{self.next_id:04d}", self.next_id + 1
        self.items[rid] = {"id": rid, "title": title, "url": url, "notes": notes, "isCompleted": False}
        return dict(self.items[rid])

    def __call__(self, cmd, *args):
        opts = lambda a: dict(zip(a[::2], a[1::2]))  # noqa: E731
        if "--list" in args:
            self.lists_used.add(args[args.index("--list") + 1])
        if cmd == "status":
            return {"status": "full-access"}
        if cmd == "show":
            return [dict(r) for r in self.items.values()]
        if cmd == "add":
            o = opts(args)
            return self.add(o["--title"], o["--url"], o["--notes"])
        if cmd == "edit":
            o = opts(args[1:])
            self.items[args[0]].update(title=o["--title"], url=o["--url"], notes=o["--notes"])
        elif cmd == "complete":
            for rid in args:
                self.items[rid]["isCompleted"] = True
        elif cmd == "delete":
            for rid in args[1:]:
                del self.items[rid]

    def open_titles(self):
        return sorted(r["title"] for r in self.items.values() if not r["isCompleted"])

    def links(self):
        return [r for r in self.items.values() if not r["isCompleted"] and (r["url"] or "").startswith(ah.LINK_BASE)]


class Sync(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.config = tmp / "config.json"
        self.config.write_text(json.dumps({"products": {"melk": 1, "Brood": 2, "kaas": 3, "shampoo": None}}))
        self.search = mock.Mock(side_effect=lambda name, token: [{"id": 9, "label": "AH Appels (1 kg)"}] if name == "appels" else [])
        for patch in (mock.patch.object(ah, "STATE", tmp / "state.json"),
                      mock.patch.object(ah, "CONFIG", self.config),
                      mock.patch.object(ah, "anon_token", return_value="t"),
                      mock.patch.object(ah, "search", self.search),
                      mock.patch.object(ah, "log")):
            patch.start()
            self.addCleanup(patch.stop)

    def run_sync(self, fake, dry_run=False):
        with mock.patch.object(ah, "remindctl", fake):
            ah.sync(dry_run)

    def test_link_then_tick_completes_only_linked_items(self):
        fake = FakeReminders("melk", "2x brood", "shampoo", "iets raars", "appels")
        self.run_sync(fake)
        [link] = fake.links()
        self.assertEqual(link["url"], ah.LINK_BASE + "p=1:1&p=2:2&p=9:1")
        self.assertEqual(link["title"], "Add 3 items to AH")
        self.assertIn("- appels: AH Appels (1 kg)", link["notes"])
        self.assertIn("- iets raars", link["notes"])

        fake.items[link["id"]]["isCompleted"] = True
        self.run_sync(fake)
        self.assertEqual(fake.open_titles(), ["iets raars", "shampoo"])
        self.assertEqual(fake.links(), [])
        self.assertEqual(self.search.call_count, 2)  # appels and iets raars, each searched once

    def test_new_item_edits_link_and_deleted_link_comes_back(self):
        fake = FakeReminders("melk")
        self.run_sync(fake)
        [link] = fake.links()
        fake.add("kaas x2")
        self.run_sync(fake)
        [edited] = fake.links()
        self.assertEqual(edited["id"], link["id"])
        self.assertEqual(edited["url"], ah.LINK_BASE + "p=1:1&p=3:2")

        del fake.items[link["id"]]
        self.run_sync(fake)
        [again] = fake.links()
        self.assertNotEqual(again["id"], link["id"])
        self.assertEqual(fake.open_titles(), ["Add 2 items to AH", "kaas x2", "melk"])

    def test_over_limit_splits_links(self):
        fake = FakeReminders(*[f"item {i}" for i in range(ah.MAX_PER_LINK + 1)])
        self.config.write_text(json.dumps({"list": "Boodschappen", "products": {f"item {i}": 100 + i for i in range(30)}}))
        self.run_sync(fake)
        self.assertCountEqual([r["title"] for r in fake.links()],
                              [f"Add {ah.MAX_PER_LINK} items to AH (1/2)", "Add 1 item to AH (2/2)"])
        self.assertEqual(fake.lists_used, {"Boodschappen"})

    def test_dry_run_writes_nothing(self):
        fake = FakeReminders("melk")
        self.run_sync(fake, dry_run=True)
        self.assertEqual(fake.links(), [])
        self.assertFalse(ah.STATE.exists())


class ParseItem(unittest.TestCase):
    def test_quantities(self):
        for title, want in [("melk", ("melk", 1)), ("2x melk", ("melk", 2)), ("2 X Melk", ("Melk", 2)),
                            ("melk x3", ("melk", 3)), ("eieren 2x", ("eieren", 2)),
                            ("2 xylitol gum", ("2 xylitol gum", 1)), ("  volkoren   brood ", ("volkoren brood", 1))]:
            self.assertEqual(ah.parse_item(title), want, title)


if __name__ == "__main__":
    unittest.main()
