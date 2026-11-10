import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auto_heijn as bridge


class Bridge(unittest.TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp()) / "config.json"
        self.config.write_text(json.dumps({"list": "Boodschappen", "server": "https://ah.test/"}))
        self.calls = []
        self.reminders = [{"id": "A1", "title": "2x melk"}, {"id": "B2", "title": "kaas"}]
        for patch in (mock.patch.object(bridge, "CONFIG", self.config),
                      mock.patch.object(bridge, "remindctl", side_effect=self.remindctl),
                      mock.patch.object(bridge, "log")):
            patch.start()
            self.addCleanup(patch.stop)

    def remindctl(self, cmd, *args, **_):
        self.calls.append((cmd, *args))
        return {"status": "full-access"} if cmd == "status" else self.reminders if cmd == "show" else None

    def test_sends_open_items_and_ticks_what_the_server_confirms(self):
        with mock.patch.object(bridge, "call", return_value={"complete": ["B2"], "log": ["added kaas"]}) as call:
            bridge.sync()
        call.assert_called_once_with("/api/sync", {"items": self.reminders, "dry_run": False})
        self.assertIn(("show", "open", "--list", "Boodschappen"), self.calls)
        self.assertIn(("complete", "B2"), self.calls)

    def test_dry_run_ticks_nothing(self):
        with mock.patch.object(bridge, "call", return_value={"complete": ["B2"]}):
            bridge.sync(dry_run=True)
        self.assertFalse([c for c in self.calls if c[0] == "complete"])

    def test_server_is_required(self):
        self.config.write_text("{}")
        with self.assertRaises(SystemExit):
            bridge.load_config()


if __name__ == "__main__":
    unittest.main()
