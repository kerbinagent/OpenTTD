"""Authorization and durability checks for social conversations."""

import os
import tempfile
import unittest

from backend.errors import ServiceError
from backend.social import Social
from backend.store import Store


class SocialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "state.sqlite")
        self.store = Store(self.path)
        self.tokens = {
            "alice": self.store.provision("world", "alice", 1, kind="human", display_name="Alice"),
            "bot": self.store.provision("world", "bot", 1, kind="agent", display_name="Route Bot"),
            "rival": self.store.provision("world", "rival", 2, kind="agent", display_name="Rival Bot"),
            "outsider": self.store.provision("other", "outsider", 1, kind="human"),
        }
        self.users = {name: self.store.authenticate(token) for name, token in self.tokens.items()}
        self.social = Social(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_world_company_and_direct_privacy_and_events(self):
        alice, bot, rival, outsider = (self.users[x] for x in ("alice", "bot", "rival", "outsider"))
        public = self.social.create_thread(alice, "world")
        company = self.social.create_thread(bot, "company")
        direct = self.social.create_thread(alice, "direct_company", [1, 2])
        self.assertEqual(direct["thread_id"],
                         self.social.create_thread(rival, "direct_company", [2, 1])["thread_id"])

        public_message = self.social.send(alice, public["id"], "World hello", "public-1", 11)
        company_message = self.social.send(bot, company["id"], "Company plans", "company-1", 12)
        direct_message = self.social.send(rival, direct["id"], "A proposed route", "direct-1", 13)
        self.assertEqual(public_message["sender"]["kind"], "human")
        self.assertEqual(company_message["sender"]["display_name"], "Route Bot")
        self.assertEqual(direct_message["tick"], 13)
        self.assertTrue(direct_message["timestamp"].endswith("Z"))
        self.assertEqual({r["identity_id"] for r in direct_message["recipients"]},
                         {"alice", "bot", "rival"})
        self.assertEqual(len(self.social.history(alice, public["id"])["messages"]), 1)
        self.assertEqual(len(self.social.history(bot, company["id"])["messages"]), 1)
        self.assertEqual(len(self.social.history(rival, direct["id"])["messages"]), 1)
        self.assert_code("thread_unavailable", lambda: self.social.history(rival, company["id"]))
        self.assert_code("thread_unavailable", lambda: self.social.history(outsider, direct["id"]))
        self.assert_code("thread_unavailable", lambda: self.social.send(outsider, direct["id"], "spy", "spy"))
        self.assertEqual([event["data"]["body"] for event in self.store.events(alice)["events"]],
                         ["World hello", "Company plans", "A proposed route"])
        self.assertEqual([event["data"]["body"] for event in self.store.events(rival)["events"]],
                         ["World hello", "A proposed route"])
        self.assertEqual(self.store.events(outsider)["events"], [])

    def test_history_snapshot_and_revocation(self):
        alice, rival = self.users["alice"], self.users["rival"]
        direct = self.social.create_thread(alice, "direct_company", [1, 2])
        old = self.social.send(alice, direct["id"], "Before joining", "m1")
        newcomer_token = self.store.provision("world", "newcomer", 2, kind="agent")
        newcomer = self.store.authenticate(newcomer_token)
        self.assertEqual(self.social.history(newcomer, direct["id"])["messages"], [])
        new = self.social.send(rival, direct["id"], "After joining", "m2")
        page = self.social.history(newcomer, direct["id"], limit=1)
        self.assertEqual([m["id"] for m in page["messages"]], [new["id"]])
        self.assertEqual(page["next_after"], new["id"])
        self.assert_code("history_unavailable", lambda: self.social.history(newcomer, direct["id"], old["id"]))
        self.store.revoke_membership("world", "rival", 2)
        self.assert_code("unauthorized", lambda: self.social.history(rival, direct["id"]))
        self.assert_code("unauthorized", lambda: self.social.report(rival, old["id"], "abuse"))

    def test_same_identity_new_company_does_not_inherit_old_direct_history(self):
        alice = self.users["alice"]
        direct = self.social.create_thread(alice, "direct_company", [1, 2])
        old = self.social.send(alice, direct["id"], "Before second grant", "first")
        second_token = self.store.provision("world", "alice", 2, kind="human", display_name="Alice")
        second = self.store.authenticate(second_token)
        self.assertEqual(self.social.history(second, direct["id"])["messages"], [])
        self.assert_code("history_unavailable", lambda: self.social.history(second, direct["id"], old["id"]))
        self.assertEqual(self.store.events(second)["events"], [])
        self.social.send(self.users["rival"], direct["id"], "After second grant", "second")
        self.assertEqual(len(self.social.history(second, direct["id"])["messages"]), 1)
        self.assertEqual(len(self.store.events(second)["events"]), 1)

    def test_delivery_outbox_and_native_admission_survive_restart(self):
        alice = self.users["alice"]
        world = self.social.create_thread(alice, "world")
        sent = self.social.send(alice, world["id"], "For native client", "outbound")
        pending = self.social.pending_deliveries("world")
        self.assertEqual([x["message_id"] for x in pending], [sent["id"]])
        self.assertEqual(pending[0]["payload"]["recipient_company_ids"], [])
        self.assertFalse(pending[0]["payload"]["tick_authoritative"])
        self.store.close()
        self.store = Store(self.path)
        self.social = Social(self.store)
        self.assertEqual(len(self.social.pending_deliveries("world")), 1)
        self.social.mark_delivered(sent["id"], 77)
        self.social.mark_delivered(sent["id"], 77)
        self.assertEqual(self.social.pending_deliveries("world"), [])
        alice = self.store.authenticate(self.tokens["alice"])
        self.assertEqual(self.social.history(alice, world["id"])["messages"][0]["tick"], 77)
        self.assert_code("delivery_conflict", lambda: self.social.mark_delivered(sent["id"], 78))
        self.social.update_native_roster("world", [
            {"public_key": "key-a", "company_id": 1, "label": "Native A"},
            {"public_key": "key-b", "company_id": 2, "label": "Native B"},
        ])
        native = {"type": "chat.pending", "event_id": "event-1", "message_id": "native-1",
                  "scope": "world", "body": "Native hello", "tick": 80,
                  "sender": {"public_key": "key-a", "company_id": 1,
                             "label": "forged", "kind": "agent"}}
        admitted = self.social.admit_native("world", native)
        self.assertEqual(admitted["disposition"], "accepted")
        self.assertEqual(self.social.admit_native("world", native), admitted)
        ingested = self.social.history(alice, world["id"])["messages"][-1]
        self.assertEqual(ingested["sender"]["display_name"], "Native A")
        self.assertTrue(ingested["tick_authoritative"])
        self.assertEqual(self.social.native_delivery("native-1")["recipient_public_keys"],
                         ["key-a", "key-b"])
        self.assertIn("native-1", [entry["message_id"] for entry in
                                   self.social.pending_deliveries("world")])
        self.social.mark_delivered("native-1", 81)
        self.assertNotIn("native-1", [entry["message_id"] for entry in
                                       self.social.pending_deliveries("world")])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM social_messages WHERE id='native-1'").fetchone()[0], 1)
        self.assertEqual(self.social.history(alice, world["id"])["messages"][-1]["tick"], 80)
        self.assertEqual(len(self.social.history(alice, world["id"])["messages"]), 2)

    def test_outbox_capacity_and_blocked_recipient_filter(self):
        alice, rival = self.users["alice"], self.users["rival"]
        second_token = self.store.provision("world", "second", 2, kind="agent")
        second = self.store.authenticate(second_token)
        social = Social(self.store, max_pending_company=1, max_pending_world=2)
        direct = social.create_thread(alice, "direct_company", [1, 2])
        social.block(rival, "alice")
        sent = social.send(alice, direct["id"], "Only second receives", "one")
        self.assertEqual({r["identity_id"] for r in sent["recipients"]},
                         {"alice", "bot", "second"})
        self.assertEqual(social.history(rival, direct["id"])["messages"], [])
        self.assertEqual(len(social.history(second, direct["id"])["messages"]), 1)
        self.assert_code("chat_backlog", lambda: social.send(alice, direct["id"], "queued", "two"))
        social.mark_delivered(sent["id"], 5)
        social.send(alice, direct["id"], "now available", "two")

    def test_revoked_native_roster_rejects_before_projection(self):
        world = self.social.create_thread(self.users["alice"], "world")
        self.social.update_native_roster("world", [
            {"public_key": "key-a", "company_id": 1, "label": "Native A"}])
        self.social.update_native_roster("world", [])
        native = {"type": "chat.pending", "event_id": "revoked-event",
                  "message_id": "revoked-native", "scope": "world",
                  "body": "must stay private", "tick": 8,
                  "sender": {"public_key": "key-a", "company_id": 1}}
        self.assertEqual(self.social.admit_native("world", native)["disposition"], "rejected")
        self.assertEqual(self.social.history(self.users["rival"], world["id"])["messages"], [])

    def test_native_spectator_world_only_without_public_credential(self):
        self.social.update_native_roster("world", [
            {"public_key": "spectator", "company_id": 255, "label": "Viewer"},
            {"public_key": "player", "company_id": 1, "label": "Player"},
        ])
        spectator_row = self.store.connection.execute(
            "SELECT * FROM social_native_roster WHERE public_key='spectator'").fetchone()
        spectator = self.store.get_principal(spectator_row["run_id"])
        self.assertEqual([t["scope"] for t in self.social.list_threads(spectator)], ["world"])
        self.assertIsNone(self.store.connection.execute(
            "SELECT 1 FROM credentials WHERE run_id=?", (spectator.run_id,)
        ).fetchone())
        base = {"type": "chat.pending", "event_id": "viewer-1", "message_id": "viewer-message",
                "scope": "world", "body": "Watching", "tick": 4,
                "sender": {"public_key": "spectator", "company_id": 255}}
        self.assertEqual(self.social.admit_native("world", base)["disposition"], "accepted")
        self.assertEqual(self.social.native_delivery("viewer-message")["recipient_public_keys"],
                         ["player", "spectator"])
        self.assertIsNone(self.social.native_delivery("viewer-message")["sender"]["company_id"])
        private = {**base, "event_id": "viewer-2", "message_id": "viewer-private",
                   "scope": "direct_company", "target_company_id": 1}
        self.assertEqual(self.social.admit_native("world", private)["disposition"], "rejected")
        self.assertIsNone(self.store.connection.execute(
            "SELECT 1 FROM social_messages WHERE id='viewer-private'").fetchone())

    def test_native_wire_null_spectator_and_key_case(self):
        self.social.update_native_roster("world", [
            {"public_key": "ABCDEF", "company_id": None, "label": "Viewer"},
            {"public_key": "FEDCBA", "company_id": 0, "label": "Player"},
        ])
        before = self.store.connection.execute(
            "SELECT identity_id FROM social_native_roster WHERE public_key='abcdef'").fetchone()[0]
        pending = {"type": "chat.pending", "event_id": "wire-viewer-event",
                   "message_id": "wire-viewer-message", "scope": "world",
                   "body": "Hello world", "tick": 18,
                   "sender": {"public_key": "abcdef", "company_id": None,
                              "label": "untrusted", "kind": "agent"}}
        self.assertEqual(self.social.admit_native("world", pending)["disposition"], "accepted")
        delivery = self.social.native_delivery("wire-viewer-message")
        self.assertEqual(delivery["recipient_public_keys"], ["ABCDEF", "FEDCBA"])
        self.assertIsNone(delivery["sender"]["company_id"])
        company = {**pending, "event_id": "wire-company-event",
                   "message_id": "wire-company-message", "scope": "company"}
        self.assertEqual(self.social.admit_native("world", company)["disposition"], "rejected")
        self.store.connection.execute(
            "UPDATE social_native_roster SET public_key='ABCDEF' WHERE public_key='abcdef'")
        self.store.connection.execute(
            """UPDATE social_message_native_recipients SET public_key='ABCDEF'
               WHERE message_id='wire-viewer-message' AND public_key='abcdef'""")
        self.social = Social(self.store)
        self.assertEqual(self.social.native_delivery("wire-viewer-message")["recipient_public_keys"],
                         ["ABCDEF", "FEDCBA"])
        self.social.update_native_roster("world", [
            {"public_key": "abcdef", "company_id": None, "label": "Viewer"},
            {"public_key": "FEDCBA", "company_id": 0, "label": "Player"},
        ])
        after = self.store.connection.execute(
            "SELECT identity_id FROM social_native_roster WHERE public_key='abcdef'").fetchone()[0]
        self.assertEqual(before, after)

    def test_native_worker_key_lookup_is_scoped_and_does_not_change_chat(self):
        worker_key, human_key = "A" * 64, "B" * 64
        self.social.update_native_roster("world", [
            {"public_key": worker_key, "company_id": 1, "label": "Shared Name"},
            {"public_key": human_key, "company_id": 2, "label": "Shared Name"},
        ])
        agent_token = self.store.provision(
            "world", "same-name-agent", 1, kind="agent", display_name="Shared Name")
        agent = self.store.authenticate(agent_token)
        worker = self.store.connection.execute(
            "SELECT identity_id,company_id FROM social_native_roster WHERE world_id='world' AND public_key=?",
            (worker_key.lower(),),
        ).fetchone()
        human = self.store.connection.execute(
            "SELECT identity_id,company_id FROM social_native_roster WHERE world_id='world' AND public_key=?",
            (human_key.lower(),),
        ).fetchone()
        world = self.social.create_thread(agent, "world")
        message = self.social.send(agent, world["id"], "Shared route", "worker-filter-message")
        before = self.social.native_delivery(message["id"])["recipient_public_keys"]
        self.assertEqual(before, [worker_key, human_key])
        matches = self.social.native_memberships_for_keys(
            "world", {worker_key.lower(), "not-a-key", "C" * 63})
        self.assertEqual(matches, {(worker["identity_id"], worker["company_id"])})
        self.assertNotIn((human["identity_id"], human["company_id"]), matches)
        self.assertNotIn((agent.identity_id, agent.company_id), matches)
        self.assertEqual(self.social.native_delivery(message["id"])["recipient_public_keys"], before)
        self.assertEqual(self.social.native_memberships_for_keys("other", [worker_key]), set())
        self.social.update_native_roster("other", [
            {"public_key": worker_key, "company_id": 2, "label": "Shared Name"}])
        other_matches = self.social.native_memberships_for_keys("other", [worker_key])
        self.assertEqual(len(other_matches), 1)
        self.assertNotEqual(other_matches, matches)
        self.social.update_native_roster("world", [
            {"public_key": human_key, "company_id": 2, "label": "Shared Name"}])
        self.assertEqual(self.social.native_memberships_for_keys("world", [worker_key]), set())

    def test_native_delivery_wire_company_is_numeric_for_agent_only_company(self):
        token = self.store.provision("world", "solo-agent", 0, kind="agent")
        agent = self.store.authenticate(token)
        thread = self.social.create_thread(agent, "company")
        message = self.social.send(agent, thread["id"], "Agent-only route", "agent-only")
        delivery = self.social.native_delivery(message["id"])
        self.assertEqual(delivery["recipient_public_keys"], [])
        self.assertEqual(delivery["sender"]["company_id"], 0)
        self.assertIs(type(delivery["sender"]["company_id"]), int)
        self.assertIs(type(message["sender"]["company_id"]), int)

    def test_engine_chat_body_and_label_byte_limits(self):
        alice = self.users["alice"]
        world = self.social.create_thread(alice, "world")
        self.social.send(alice, world["id"], "é" * 384, "utf8-at-limit")
        self.assert_code("invalid_message", lambda: self.social.send(
            alice, world["id"], "é" * 385, "utf8-over-limit"))
        larger_config = Social(self.store, max_message_bytes=4096)
        self.assertEqual(larger_config.max_message_bytes, 768)
        self.assert_code("invalid_message", lambda: larger_config.send(
            alice, world["id"], "x" * 769, "configured-over-limit"))
        long_label_token = self.store.provision(
            "world", "long-label", 3, kind="agent", display_name="é" * 41)
        long_label = self.store.authenticate(long_label_token)
        self.assert_code("invalid_message", lambda: self.social.send(
            long_label, world["id"], "short body", "long-label-send"))

    def test_native_direct_identity_exact_keys_and_deduped_ack(self):
        self.social.update_native_roster("world", [
            {"public_key": "a", "company_id": 1, "label": "A"},
            {"public_key": "b", "company_id": 2, "label": "B"},
            {"public_key": "c", "company_id": 2, "label": "C"},
        ])
        pending = {"type": "chat.pending", "event_id": "private-event", "message_id": "private-message",
                   "scope": "direct_identity", "body": "For B only", "tick": 55,
                   "sender": {"public_key": "a", "company_id": 1}, "target_public_key": "b"}
        accepted = self.social.admit_native("world", pending)
        self.assertEqual(accepted["disposition"], "accepted")
        self.assertEqual(self.social.admit_native("world", pending), accepted)
        self.assertEqual(self.social.native_delivery("private-message")["recipient_public_keys"], ["a", "b"])
        rows = {row["public_key"]: row for row in self.store.connection.execute(
            "SELECT * FROM social_native_roster WHERE world_id='world'")}
        b = self.store.get_principal(rows["b"]["run_id"])
        c = self.store.get_principal(rows["c"]["run_id"])
        self.assertEqual(len(self.social.history(b, accepted["thread_id"])["messages"]), 1)
        self.assert_code("thread_unavailable", lambda: self.social.history(c, accepted["thread_id"]))
        self.assertEqual(len(self.store.events(b)["events"]), 1)
        self.assertEqual(self.social.admit_native("world", {**pending, "body": "changed"})["disposition"],
                         "rejected")
        self.social.update_native_roster("world", [
            {"public_key": "a", "company_id": 1, "label": "A"},
            {"public_key": "c", "company_id": 2, "label": "C"},
        ])
        self.assertEqual(self.social.native_delivery("private-message")["recipient_public_keys"], ["a"])

    def test_duplicate_native_key_uses_lowest_remote_label_and_clears_conflicted_company(self):
        key = "a" * 64
        first = {"id": 20, "generation": 1, "engine_instance_id": "boot"}
        second = {"id": 4, "generation": 2, "engine_instance_id": "boot"}
        self.social.update_native_roster("world", [
            {"public_key": key, "company_id": 1, "label": "First",
             "native_handle": first},
        ])
        normalized = self.social.normalize_native_roster([
            {"public_key": key.upper(), "company_id": 1,
             "label": "Higher ClientID", "native_handle": first},
            {"public_key": key, "company_id": 2,
             "label": "Lower ClientID", "native_handle": second},
            {"public_key": key, "company_id": 2, "label": "Local server"},
        ])
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["label"], "Lower ClientID")
        self.assertEqual(normalized[0]["company_id"], 255)
        self.assertEqual(normalized[0]["native_handle"], second)
        self.social.update_native_roster("world", normalized)
        row = self.store.connection.execute(
            "SELECT identity_id,company_id,display_name FROM social_native_roster "
            "WHERE world_id='world' AND public_key=?", (key,)).fetchone()
        self.assertEqual((row["company_id"], row["display_name"]),
                         (255, "Lower ClientID"))
        old = self.store.connection.execute(
            "SELECT active FROM memberships WHERE world_id='world' "
            "AND identity_id=? AND company_id=1", (row["identity_id"],)).fetchone()
        self.assertEqual(old["active"], 0)

        for invalid in (True, -1, 1 << 64):
            with self.subTest(generation=invalid):
                self.assert_code("invalid_roster", lambda: self.social.normalize_native_roster([
                    {"public_key": key, "company_id": 1, "label": "Bad",
                     "native_handle": {**first, "generation": invalid}}]))

    def test_retained_worker_membership_lookup_chunks_over_256_keys(self):
        roster = [{"public_key": f"{index:064x}", "company_id": 1,
                   "label": f"Worker {index}"} for index in range(300)]
        self.social.update_native_roster("world", roster)
        keys = {row["public_key"] for row in roster}
        self.assertEqual(len(self.social.native_memberships_for_keys("world", keys)), 300)

    def test_native_company_delivery_filters_personal_blocked_key(self):
        self.social.update_native_roster("world", [
            {"public_key": "a", "company_id": 1, "label": "A"},
            {"public_key": "b", "company_id": 2, "label": "B"},
            {"public_key": "c", "company_id": 2, "label": "C"},
        ])
        rows = {row["public_key"]: row for row in self.store.connection.execute(
            "SELECT * FROM social_native_roster WHERE world_id='world'")}
        a = self.store.get_principal(rows["a"]["run_id"])
        b = self.store.get_principal(rows["b"]["run_id"])
        self.social.block(b, a.identity_id)
        pending = {"type": "chat.pending", "event_id": "company-event",
                   "message_id": "company-native", "scope": "direct_company",
                   "target_company_id": 2, "body": "For other company", "tick": 67,
                   "sender": {"public_key": "a", "company_id": 1}}
        receipt = self.social.admit_native("world", pending)
        self.assertEqual(receipt["disposition"], "accepted")
        self.assertEqual(self.social.native_delivery("company-native")["recipient_public_keys"],
                         ["a", "c"])
        self.assertEqual(self.social.history(b, receipt["thread_id"])["messages"], [])

    def test_native_oversized_pending_gets_durable_rejection(self):
        self.social.update_native_roster("world", [
            {"public_key": "a", "company_id": 1, "label": "A"}])
        pending = {"type": "chat.pending", "event_id": "too-long-event",
                   "message_id": "too-long-message", "scope": "world",
                   "body": "x" * 769, "tick": 1,
                   "sender": {"public_key": "a", "company_id": 1}}
        first = self.social.admit_native("world", pending)
        self.assertEqual(first["disposition"], "rejected")
        self.assertEqual(self.social.admit_native("world", pending), first)
        self.assertEqual(self.store.connection.execute(
            "SELECT disposition FROM social_native_receipts WHERE event_id='too-long-event'"
        ).fetchone()[0], "rejected")

    def test_durable_retry_mute_block_report_and_rate(self):
        alice, rival = self.users["alice"], self.users["rival"]
        direct = self.social.create_thread(alice, "direct_company", [1, 2])
        self.social.mute(rival, direct["id"])
        first = self.social.send(alice, direct["id"], "hello", "key-1")
        self.assertEqual(self.store.events(rival)["events"], [])
        self.assertEqual(self.social.history(rival, direct["id"])["messages"][0]["id"], first["id"])
        self.assertEqual(self.social.send(alice, direct["id"], "hello", "key-1")["id"], first["id"])
        self.assert_code("request_conflict", lambda: self.social.send(alice, direct["id"], "changed", "key-1"))
        report = self.social.report(rival, first["id"], "spam")
        self.assertEqual(report["status"], "received")
        self.social.block(rival, "alice")
        self.assert_code("delivery_unavailable", lambda: self.social.send(alice, direct["id"], "again", "key-2"))
        self.social.block(rival, "alice", False)
        self.store.close()
        self.store = Store(self.path)
        self.social = Social(self.store, message_limit=1, window_seconds=3600)
        alice = self.store.authenticate(self.tokens["alice"])
        self.assertEqual(self.social.history(alice, direct["id"])["messages"][0]["id"], first["id"])
        self.assert_code("rate_limited", lambda: self.social.send(alice, direct["id"], "second", "key-2"))


if __name__ == "__main__":
    unittest.main()
