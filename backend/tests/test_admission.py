"""Finite company admission, durable claim recovery and private pool validation."""

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest

from backend.admission import AgentAdmission
from backend.errors import ServiceError
from backend.renderer import RendererGateway
from backend.service import Service
from backend.store import Store
from backend.tests.test_renderer import FakeRenderer
from backend.tests.test_service import FakeEngine


class AdmissionEngine(FakeEngine):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.company_status = "completed"
        self.on_company = None

    def call(self, envelope):
        if self.store.connection.in_transaction:
            raise AssertionError("Native RPC inside SQLite transaction")
        if envelope["type"] == "company.ensure":
            self.calls.append(copy.deepcopy(envelope))
            if self.on_company:
                self.on_company()
            return {"status": self.company_status}
        return super().call(envelope)


class AdmissionRenderer:
    def __init__(self, runs):
        self.runs = set(runs)
        self.checked = []
        self.fail = False

    def available(self, principal):
        return principal.run_id in self.runs

    def configure_authority(self, instance, lag):
        self.authority = instance, lag

    def check_ready(self, principal):
        self.checked.append(principal.run_id)
        if self.fail:
            raise ServiceError("renderer_unavailable", status=503)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / "world.sqlite3"
        self.store = Store(self.database, world_id="world")
        self.tokens = {}
        self.principals = []
        self.settings = {}
        for company, mode in enumerate(("text", "visual", "text", "visual")):
            token = self.store.provision("world", f"agent-{company}", company, mode)
            principal = self.store.authenticate(token)
            self.principals.append(principal)
            self.tokens[principal.run_id] = token
            path = self.root / f"token-{company}"
            self.write_private(path, token + "\n")
            self.settings[principal.run_id] = {"token_file": str(path)}
        self.renderer = AdmissionRenderer(p.run_id for p in self.principals if p.mode == "visual")
        self.pool_path = self.root / "pool.json"
        self.config = {"version": 1, "world_id": "world", "world_epoch": "initial", "runs": self.settings}
        self.save_config()
        self.engine = AdmissionEngine(self.store)
        self.admission = AgentAdmission.from_file(self.pool_path, self.store, "world", self.renderer)
        self.service = Service(self.store, self.engine, autostart=False, world_id="world",
                               renderer=self.renderer, admission=self.admission)

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def write_private(path, text):
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    def save_config(self):
        self.write_private(self.pool_path, json.dumps(self.config))

    def join(self, secret=None, mode="text", body=None, query=None):
        return self.service.handle("POST", "/v1/agent/join", secret or secrets.token_urlsafe(32),
                                   {"mode": mode} if body is None else body, query)

    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def claims(self):
        with self.store.lock:
            return self.store.connection.execute("SELECT * FROM agent_join_claims").fetchall()

    def test_disabled_and_only_exact_route_bypasses_authentication(self):
        self.service.admission = None
        self.assertEqual(self.assert_code("join_disabled", self.join).status, 403)
        for method, path in (("GET", "/v1/agent/join"), ("POST", "/v1/agent/join/"),
                             ("POST", "/v1/agent/%6aoin")):
            self.assert_code("unauthorized", lambda: self.service.handle(method, path, "claim"))
        self.assertFalse(self.engine.calls)

    def test_participants_hide_only_unclaimed_pool_runs_without_changing_replay(self):
        # This renderer fixture tests admission only; roster worker filtering is
        # covered by the service tests with a full renderer implementation.
        self.service.renderer = None
        operator_token = self.store.provision("world", "operator-agent", 4, kind="agent", display_name="Operator agent")

        def people():
            return self.service.handle("GET", "/v1/participants", operator_token)[1]["participants"]

        self.assertEqual([person["identity_id"] for person in people()], ["operator-agent"])
        self.assertEqual(len(self.store.principals_for_world("world")), 5)
        secret = secrets.token_urlsafe(32)
        _, joined = self.join(secret)
        visible = {person["identity_id"]: person for person in people()}
        self.assertEqual(set(visible), {"operator-agent", joined["identity_id"]})
        self.assertEqual(visible[joined["identity_id"]]["presence"], "unknown")
        self.assertEqual(self.join(secret), (200, joined))
        self.assertEqual(self.service.handle("GET", "/v1/me", joined["token"])[1]["run_id"], joined["run_id"])
        # Merely reading the roster neither claims another slot nor revokes
        # its credential/membership, even though it is hidden from presentation.
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(len(self.store.principals_for_world("world")), 5)
        for principal in self.principals:
            self.assertEqual(self.store.authenticate(self.tokens[principal.run_id]), principal)

        _, visual = self.join(mode="visual")
        visible = {person["identity_id"]: person for person in people()}
        self.assertEqual(set(visible), {"operator-agent", joined["identity_id"], visual["identity_id"]})
        self.assertEqual(visible[visual["identity_id"]]["presence"], "unknown")
        self.store.revoke_membership("world", joined["identity_id"], joined["company_id"])
        self.assertNotIn(joined["identity_id"], {person["identity_id"] for person in people()})
        self.assert_code("join_revoked", lambda: self.join(secret))

    def test_participants_without_configured_pool_retain_operator_agents(self):
        self.service.renderer = None
        self.service.admission = None
        token = self.tokens[self.principals[0].run_id]
        people = self.service.handle("GET", "/v1/participants", token)[1]["participants"]
        self.assertEqual({person["identity_id"] for person in people}, {p.identity_id for p in self.principals})
        self.assertTrue(all(person["presence"] == "unknown" for person in people))

    def test_claim_is_scoped_and_private_material_is_not_stored(self):
        secret = secrets.token_urlsafe(32)
        status, reply = self.join(secret)
        self.assertEqual(status, 200)
        self.assertEqual(reply["company_id"], 0)
        self.assertEqual(reply["mode"], "text")
        _, me = self.service.handle("GET", "/v1/me", reply["token"])
        self.assertEqual(me["run_id"], reply["run_id"])
        self.assertEqual(me["kind"], "agent")
        dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn(secret, dump)
        self.assertNotIn(reply["token"], dump)
        checks = [call for call in self.engine.calls if call["type"] == "company.ensure"]
        self.assertEqual(len(checks), 1)
        self.assertIs(checks[0]["create"], False)
        self.assertNotIn("actor", checks[0])
        self.assertEqual(checks[0]["engine_instance_id"], "boot")

    def test_lost_response_replay_works_during_outage_and_after_restart(self):
        secret = secrets.token_urlsafe(32)
        first = self.join(secret)
        self.engine.unavailable.add("world")
        self.store.begin_recovery("world", "later-boot")
        calls = len(self.engine.calls)
        self.assertEqual(self.join(secret), first)
        self.assertEqual(len(self.engine.calls), calls)
        self.service.close()
        self.store.close()
        self.store = Store(self.database, world_id="world")
        self.engine = AdmissionEngine(self.store)
        self.engine.unavailable.add("world")
        admission = AgentAdmission.from_file(self.pool_path, self.store, "world", self.renderer)
        self.service = Service(self.store, self.engine, autostart=False, world_id="world", admission=admission)
        self.assertEqual(self.join(secret), first)
        self.assertFalse(self.engine.calls)
        self.assertEqual(len(self.claims()), 1)

    def test_same_secret_changed_mode_conflicts_without_native_call(self):
        secret = secrets.token_urlsafe(32)
        self.join(secret)
        calls = len(self.engine.calls)
        self.assertEqual(self.assert_code("join_conflict", lambda: self.join(secret, "visual")).status, 409)
        self.assertEqual(len(self.engine.calls), calls)
        self.assertEqual(len(self.claims()), 1)

    def test_concurrent_distinct_claims_get_distinct_mode_matching_slots(self):
        barrier = threading.Barrier(4)
        secrets_and_modes = [(secrets.token_urlsafe(32), mode) for mode in ("text", "visual", "text", "visual")]

        def claim(item):
            secret, mode = item
            barrier.wait(timeout=5)
            reply = self.join(secret, mode)[1]
            self.assertEqual(reply["mode"], mode)
            self.assertEqual(self.join(secret, mode)[1], reply)
            return reply

        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(claim, secrets_and_modes))
        self.assertEqual({reply["company_id"] for reply in replies}, {0, 1, 2, 3})
        self.assertEqual(len({reply["token"] for reply in replies}), 4)
        self.assertEqual(len(self.claims()), 4)
        self.assertEqual(len(self.renderer.checked), 2)
        for mode in ("text", "visual"):
            self.assert_code("join_pool_full", lambda mode=mode: self.join(mode=mode))

    def test_same_secret_concurrent_retries_consume_one_slot(self):
        secret = secrets.token_urlsafe(32)
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: self.join(secret), range(16)))
        self.assertTrue(all(reply == replies[0] for reply in replies))
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(sum(call["type"] == "company.ensure" for call in self.engine.calls), 1)

    def test_invalid_requests_never_consume_pool(self):
        for body in ({}, {"mode": "capture"}, {"mode": "text", "company_id": 0}, [],
                     {"mode": True}, {"mode": ["text"]}):
            self.assert_code("invalid_request", lambda body=body: self.join(body=body))
        for secret in ("short", "A" * 42, "A" * 44, "!" * 43, "A" * 42 + "B"):
            self.assert_code("invalid_request", lambda secret=secret: self.join(secret))
        self.assert_code("invalid_request", lambda: self.join(query={"mode": "text"}))
        self.assertFalse(self.claims())
        self.assertFalse(self.engine.calls)

    def test_current_revoked_slot_survives_configuration_but_never_reassigned(self):
        secret = secrets.token_urlsafe(32)
        first = self.join(secret)[1]
        self.store.revoke_membership("world", first["identity_id"], first["company_id"])
        self.service.admission = AgentAdmission.from_file(self.pool_path, self.store, "world", self.renderer)
        self.engine.unavailable.add("world")
        calls = len(self.engine.calls)
        self.assert_code("join_revoked", lambda: self.join(secret))
        self.assertEqual(len(self.engine.calls), calls)
        self.assertEqual(len(self.claims()), 1)
        self.engine.unavailable.clear()
        self.assertEqual(self.join()[1]["company_id"], 2)

    def test_runtime_rotation_revokes_claim_and_stale_file_fails_restart(self):
        secret = secrets.token_urlsafe(32)
        reply = self.join(secret)[1]
        principal = self.store.authenticate(reply["token"])
        self.store.rotate_credential(principal)
        calls = len(self.engine.calls)
        self.assert_code("join_revoked", lambda: self.join(secret))
        self.assertEqual(len(self.engine.calls), calls)
        with self.assertRaisesRegex(ValueError, "credential does not match"):
            AgentAdmission.from_file(self.pool_path, self.store, "world", self.renderer)

    def test_native_unavailable_recovery_and_missing_company_do_not_claim(self):
        self.engine.unavailable.add("world")
        self.assert_code("join_not_ready", self.join)
        self.engine.unavailable.clear()
        self.engine.recovery_required = True
        self.assert_code("world_recovering", self.join)
        self.engine.recovery_required = False
        self.engine.company_status = "rejected"
        self.assert_code("join_not_ready", self.join)
        self.assertFalse(self.claims())

    def test_backend_recovery_and_race_at_commit_do_not_claim(self):
        self.store.begin_recovery("world", "boot")
        self.assert_code("world_recovering", self.join)
        self.assertFalse(self.engine.calls)
        self.store.finish_recovery("world", scan_completed=True)
        self.engine.on_company = lambda: self.store.begin_recovery("world", "replacement")
        self.assert_code("world_recovering", self.join)
        self.assertFalse(self.claims())

    def test_renderer_unavailable_and_catalog_mismatch_do_not_claim(self):
        self.renderer.fail = True
        self.assert_code("join_not_ready", lambda: self.join(mode="visual"))
        self.engine.hello_capabilities["catalog_hash"] = "invalid"
        self.assert_code("join_not_ready", self.join)
        self.assertFalse(self.claims())

    def test_visual_missing_or_malformed_authority_lag_is_finite_not_ready(self):
        for lag in (None, True, 0, -1, 65536, "100", [], 1.5):
            with self.subTest(lag=lag):
                reply = self.engine.valid_hello_reply("world")
                if lag is None:
                    reply["result"].pop("native_max_lag_ticks")
                else:
                    reply["result"]["native_max_lag_ticks"] = lag
                self.engine.hello_reply_override = reply
                error = self.assert_code("join_not_ready", lambda: self.join(mode="visual"))
                self.assertEqual(error.status, 503)
        self.assertFalse(self.claims())
        self.assertFalse(self.renderer.checked)

    def test_private_config_rejects_human_duplicate_company_epoch_and_missing_renderer(self):
        token = self.store.provision("world", "human", 4, kind="human")
        human = self.store.authenticate(token)
        path = self.root / "human-token"
        self.write_private(path, token)
        human_settings = {human.run_id: {"token_file": str(path)}}
        duplicate_token = self.store.provision("world", "duplicate", 0)
        duplicate = self.store.authenticate(duplicate_token)
        duplicate_path = self.root / "duplicate-token"
        self.write_private(duplicate_path, duplicate_token)
        cases = [{**self.config, "runs": human_settings},
                 {**self.config, "world_epoch": "other"},
                 {**self.config, "runs": {**self.settings, duplicate.run_id: {"token_file": str(duplicate_path)}}},
                 {**self.config, "runs": {}}, {**self.config, "extra": True}]
        for config in cases:
            self.write_private(self.pool_path, json.dumps(config))
            with self.assertRaises(ValueError):
                AgentAdmission.from_file(self.pool_path, self.store, "world", self.renderer)
        self.save_config()
        with self.assertRaisesRegex(ValueError, "requires its configured renderer"):
            AgentAdmission.from_file(self.pool_path, self.store, "world")

    def test_private_inputs_reject_permissions_symlinks_relative_and_wrong_tokens(self):
        for unsafe in ("permission", "symlink", "relative", "wrong-token", "oversized"):
            with self.subTest(unsafe=unsafe):
                self.config["runs"] = copy.deepcopy(self.settings)
                self.save_config()
                if unsafe == "permission":
                    self.pool_path.chmod(0o644)
                elif unsafe == "oversized":
                    self.write_private(self.pool_path, " " * 32769)
                elif unsafe == "symlink":
                    target = self.root / "pool-target.json"
                    self.pool_path.rename(target)
                    self.pool_path.symlink_to(target)
                else:
                    first, second = self.principals[:2]
                    self.config["runs"][first.run_id]["token_file"] = (
                        "relative-token" if unsafe == "relative" else self.settings[second.run_id]["token_file"])
                    self.save_config()
                with self.assertRaises(ValueError):
                    AgentAdmission.from_file(self.pool_path, self.store, "world", self.renderer)
                if self.pool_path.is_symlink():
                    self.pool_path.unlink()


class RendererAdmissionTests(unittest.TestCase):
    def test_checks_only_selected_connected_worker_and_binds_actor(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            workers, engines = {}, []
            for index in range(2):
                directory = root / f"images-{index}"
                directory.mkdir(mode=0o700)
                engine = FakeRenderer(root / f"renderer-{index}.sock", directory)
                engine.native_key = (str(index + 1) * 64)
                engines.append(engine)
                workers[f"run-{index}"] = {"engine": engine, "artifacts": directory}
            engines[1].status = "rejected"
            gateway = RendererGateway(workers, "world")
            try:
                from backend.store import Principal
                principal = Principal("agent", "world", 0, "run-0", "visual", "agent", "Agent")
                gateway.configure_authority("authority", 100)
                gateway.check_ready(principal)
                self.assertEqual([call["type"] for call in engines[0].calls], ["hello", "session.bind"])
                self.assertEqual(engines[0].calls[-1]["actor"]["run_id"], "run-0")
                self.assertFalse(engines[1].calls)
                engines[0].native_key = ""
                with self.assertRaises(ServiceError):
                    gateway.check_ready(principal)
                self.assertEqual(engines[0].calls[-1]["type"], "hello")
            finally:
                gateway.close()


if __name__ == "__main__":
    unittest.main()
