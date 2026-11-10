import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import server

HITS = {"melk": [{"id": 1525, "label": "AH Halfvolle melk (1 l)"}],
        "kaas": [{"id": 3, "label": "AH Jong belegen kaas (1 kg)"}]}


class Sync(unittest.TestCase):
    def setUp(self):
        self.lines, self.puts, self.posts, self.booking = {1525: 1}, [], [], ""
        patches = [
            mock.patch.object(server, "DATABASE_URL", os.path.join(tempfile.mkdtemp(), "test.db")),
            mock.patch.object(server, "WRITE", True),
            mock.patch.object(server, "search", side_effect=lambda query, token=None: HITS.get(query, [])),
            mock.patch.object(server, "llm", side_effect=lambda system, prompt: (
                {"pick": 1, "sure": True} if "\n1. " in prompt else {"pick": None, "sure": False})),
            mock.patch.object(server, "slack", side_effect=self.posts.append),
            mock.patch.object(server, "user_token", return_value="token"),
            mock.patch.object(server, "basket", side_effect=lambda token: ({}, dict(self.lines))),
            mock.patch.object(server, "add_items", side_effect=self.add_items),
            mock.patch.object(server, "booked", side_effect=lambda token, raw: self.booking),
            mock.patch.object(server, "log"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with server.connect() as con:
            for statement in server.SCHEMA:
                server.sql(con, statement)
            con.commit()
        self.items = [{"id": "A", "title": "2x melk"}, {"id": "B", "title": "kaas"}, {"id": "C", "title": "iets raars"}]

    def add_items(self, token, quantities):
        self.puts.append(dict(quantities))
        for pid, qty in quantities.items():
            self.lines[pid] = self.lines.get(pid, 0) + qty

    def test_adds_on_top_of_the_basket_and_ticks_what_landed(self):
        complete, _ = server.sync(self.items)
        self.assertEqual(self.puts, [{1525: 2, 3: 1}])
        self.assertCountEqual(complete, ["A", "B"])
        self.assertIn("Added to the AH basket", self.posts[-1])
        self.assertTrue(any("iets raars" in p for p in self.posts))

    def test_a_second_sync_ticks_again_without_adding_again(self):
        server.sync(self.items)
        posts = len(self.posts)
        complete, _ = server.sync(self.items)
        self.assertEqual(len(self.puts), 1)
        self.assertCountEqual(complete, ["A", "B"])
        self.assertEqual(len(self.posts), posts)  # nothing new to say, unsure items were told once

    def test_items_ah_didnt_take_stay_open(self):
        with mock.patch.object(server, "add_items", side_effect=lambda token, q: self.lines.update({3: 1})):
            complete, _ = server.sync(self.items)
        self.assertEqual(complete, ["B"])
        self.assertIn("didn't take", self.posts[-1])

    def test_booked_delivery_adds_nothing_and_says_so_once(self):
        self.booking = "a delivery is booked for 12 Nov"
        for _ in range(2):
            complete, _ = server.sync(self.items)
            self.assertEqual(complete, [])
        self.assertEqual(self.puts, [])
        self.assertEqual(sum("booked" in p for p in self.posts), 1)

    def test_logged_out_asks_for_a_login_once(self):
        with mock.patch.object(server, "user_token", side_effect=server.LoggedOut("not logged in")):
            for _ in range(2):
                self.assertEqual(server.sync(self.items)[0], [])
        self.assertEqual(sum("/login" in p for p in self.posts), 1)

    def test_read_only_mode_adds_nothing(self):
        with mock.patch.object(server, "WRITE", False):
            complete, _ = server.sync(self.items)
        self.assertEqual((complete, self.puts), ([], []))
        self.assertTrue(any(p.startswith("Read-only") for p in self.posts))

    def test_dry_run_changes_nothing(self):
        complete, notes = server.sync(self.items, dry_run=True)
        self.assertEqual((complete, self.puts, self.posts), ([], [], []))
        self.assertIn("would add", notes[-1])

    def test_not_from_ah_items_are_left_alone(self):
        with server.connect() as con:
            server.set_mapping(con, "iets raars", None)
        server.sync(self.items)
        self.assertFalse(any("iets raars" in p for p in self.posts))


class Tokens(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(server, "DATABASE_URL", os.path.join(tempfile.mkdtemp(), "test.db"))
        patch.start()
        self.addCleanup(patch.stop)
        self.con = server.connect().thing
        self.addCleanup(self.con.close)
        for statement in server.SCHEMA:
            server.sql(self.con, statement)
        server.put(self.con, "ah_tokens", {"access": "old", "refresh": "r1", "expires": time.time() - 1})

    def test_expired_access_token_is_refreshed_and_saved(self):
        with mock.patch.object(server, "ah", return_value={"access_token": "new", "refresh_token": "r2",
                                                            "expires_in": 600}) as call:
            self.assertEqual(server.user_token(self.con), "new")
        self.assertEqual(call.call_args.args[1], {"clientId": server.CLIENT, "refreshToken": "r1"})
        self.assertEqual(server.get(self.con, "ah_tokens")["refresh"], "r2")

    def test_a_dead_refresh_token_logs_out(self):
        dead = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch.object(server, "ah", side_effect=dead), self.assertRaises(server.LoggedOut):
            server.user_token(self.con)
        self.assertIsNone(server.get(self.con, "ah_tokens"))


class Booked(unittest.TestCase):
    def check(self, basket, active=None, placed=()):
        def fake_ah(path, body=None, token=None, method=None):
            if path.startswith("/mobile-services/order/"):
                if active is None:
                    raise urllib.error.HTTPError(path, 404, "no order", {}, None)
                return active
            return {"data": {"orderFulfillments": {"result": list(placed)}}}
        with mock.patch.object(server, "ah", side_effect=fake_ah):
            return server.booked("token", basket)

    def test_a_plain_basket_is_free(self):
        self.assertEqual(self.check({"itemsInOrder": None, "summary": {"shoppingType": None}}), "")

    def test_any_sign_of_an_order_stops_writes(self):
        self.assertIn("2026-11-12", self.check({"summary": {"deliveryDate": "2026-11-12"}}))
        self.assertIn("REOPENED", self.check({}, active={"id": 9, "state": "REOPENED"}))
        self.assertIn("booked", self.check({}, placed=[{"delivery": {"slot": {"date": "2026-11-12"}}}]))

    def test_an_unexpected_answer_stops_writes(self):
        with mock.patch.object(server, "ah", side_effect=urllib.error.HTTPError("u", 500, "x", {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                server.booked("token", {})


class LoginCode(unittest.TestCase):
    def test_code_from_what_people_paste(self):
        for pasted in ("appie://login-exit?code=ab-12", " ab-12 ",
                       '{"pageProps":{"__N_REDIRECT":"appie://login-exit?code=ab-12","__N_REDIRECT_STATUS":302}}'):
            self.assertEqual(server.code_from(pasted), "ab-12", pasted)
        self.assertEqual(server.code_from("  "), "")


class Http(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.dict(os.environ, {"BRIDGE_TOKEN": "s3cret"}),
                      mock.patch.object(server, "sync", return_value=(["A"], [])),
                      mock.patch.object(server, "log")):
            patch.start()
            self.addCleanup(patch.stop)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/api/sync"

    def post(self, headers):
        req = urllib.request.Request(self.url, data=b'{"items": []}', headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)

    def test_api_needs_the_bridge_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post({"Authorization": "Bearer wrong"})
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.post({"Authorization": "Bearer s3cret"})["complete"], ["A"])


class ParseItem(unittest.TestCase):
    def test_quantities(self):
        for title, want in [("melk", ("melk", 1)), ("2x melk", ("melk", 2)), ("2 X Melk", ("Melk", 2)),
                            ("melk x3", ("melk", 3)), ("eieren 2x", ("eieren", 2)),
                            ("2 xylitol gum", ("2 xylitol gum", 1)), ("  volkoren   brood ", ("volkoren brood", 1))]:
            self.assertEqual(server.parse_item(title), want, title)


if __name__ == "__main__":
    unittest.main()
