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
            mock.patch.object(server, "nutrition", return_value={}),
            mock.patch.object(server, "offer_specials"),
            mock.patch.object(server, "accepted_specials", return_value=[]),
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


def fresh_db(test):
    patch = mock.patch.object(server, "DATABASE_URL", os.path.join(tempfile.mkdtemp(), "test.db"))
    patch.start()
    test.addCleanup(patch.stop)
    con = server.connect().thing
    test.addCleanup(con.close)
    for statement in server.SCHEMA:
        server.sql(con, statement)
    return con


class Picks(unittest.TestCase):
    def setUp(self):
        self.con, self.prompts = fresh_db(self), []
        hits = [{"id": 1, "label": "Hellmann's Mayonaise (430 ml)", "price": 3.49, "bonus": "2e halve prijs",
                 "nutriscore": "E", "bought": True},
                {"id": 2, "label": "AH Mayonaise (500 ml)", "price": 1.99, "nutriscore": "E"}]
        for patch in (mock.patch.object(server, "search", return_value=hits),
                      mock.patch.object(server, "nutrition", return_value={2: {"protein": "1.0 g", "fat": "78.0 g"}}),
                      mock.patch.object(server, "llm", side_effect=lambda system, prompt: (
                          self.prompts.append(prompt) or {"pick": 1, "sure": True})),
                      mock.patch.object(server, "log")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_prompt_carries_preferences_price_bonus_and_nutrition(self):
        server.preferences(self.con, add="Hellmann's   for mayonnaise")
        self.assertEqual(server.product(self.con, "mayo")["id"], 1)
        for text in ("- Hellmann's for mayonnaise", "€3.49 on bonus (2e halve prijs)", "Nutri-Score E",
                     "protein 1.0 g, fat 78.0 g", "bought before"):
            self.assertIn(text, self.prompts[-1])

    def test_a_new_preference_redoes_llm_picks_but_keeps_maps(self):
        server.save_product(self.con, "ketchup", 9, "Heinz", True, "manual")
        for _ in range(2):
            server.product(self.con, "mayo")
            server.product(self.con, "ketchup")
        self.assertEqual(len(self.prompts), 1)
        server.preferences(self.con, add="Heinz for ketchup")
        server.product(self.con, "mayo")
        self.assertEqual(server.product(self.con, "ketchup")["id"], 9)
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(server.preferences(self.con, remove=1), [])
        with self.assertRaises(ValueError):
            server.preferences(self.con, remove=1)


class Specials(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db(self)
        self.posts, self.calls, self.lines, self.taps = [], [], {}, {}
        soon = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3 * 86400))
        deals = [{"id": 7, "label": "Hellmann's (430 ml)", "price": 1.75, "bonus": "2e halve prijs", "until": soon},
                 {"id": 8, "label": "AH melk (1 l)", "price": 1.05, "bonus": "5% volume voordeel", "until": "2999-12-31"}]
        for patch in (mock.patch.object(server, "WRITE", True),
                      mock.patch.object(server, "slack", side_effect=self.post),
                      mock.patch.object(server, "slack_call", side_effect=self.slack_call),
                      mock.patch.object(server, "bot_user", return_value="UBOT"),
                      mock.patch.object(server, "products_by_id", return_value=deals),
                      mock.patch.object(server, "user_token", return_value="token"),
                      mock.patch.object(server, "basket", side_effect=lambda token: ({}, dict(self.lines))),
                      mock.patch.object(server, "booked", return_value=""),
                      mock.patch.object(server, "add_items", side_effect=lambda token, q: self.lines.update(q)),
                      mock.patch.object(server, "log")):
            patch.start()
            self.addCleanup(patch.stop)
        server.save_product(self.con, "mayo", 7, "Hellmann's (430 ml)", True, "manual")
        server.save_product(self.con, "melk", 8, "AH melk (1 l)", True, "llm")
        server.save_product(self.con, "iets raars", 9, "AH Iets (1 st)", False, "llm")

    def post(self, text, thread=None):
        self.posts.append((text, thread))
        return f"t{len(self.posts)}"

    def slack_call(self, method, body=None, query=None):
        self.calls.append((method, body))
        replies = [{"ts": ts, "reactions": [{"name": "shopping_trolley", "users": users}]}
                   for ts, users in self.taps.items()]
        return {"ok": True, "messages": replies}

    def test_specials_go_out_once_a_week_and_a_tap_adds_one(self):
        server.sync([])
        server.sync([])
        self.assertCountEqual(server.products_by_id.call_args.args[0], [7, 8])  # not the unsure guess
        self.assertEqual([thread for _, thread in self.posts], [None, "t1"])  # one header, one deal
        self.assertIn(("reactions.add", {"name": "shopping_trolley", "timestamp": "t2"}), self.calls)
        self.taps["t2"] = ["UBOT"]
        server.sync([])
        self.assertEqual(self.lines, {})  # the bot's own 🛒 is not a yes
        self.taps["t2"] = ["UBOT", "UPERSON"]
        server.sync([])
        server.sync([])
        self.assertEqual(self.lines, {7: 1})
        self.assertIn(("reactions.add", {"name": "white_check_mark", "timestamp": "t2"}), self.calls)
        self.assertIn("on bonus: Hellmann's", self.posts[-1][0])


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
