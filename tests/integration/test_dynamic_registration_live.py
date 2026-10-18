"""Opt-in real-engine checks for dynamic allocation and save-bound ownership.

Run with OPENTTD_TEST_BINARY and OPENTTD_TEST_BASESET. These fixtures own their
servers and native clients; they do not touch an existing public world.
"""

from concurrent.futures import ThreadPoolExecutor
import copy
import os
from pathlib import Path
import subprocess
import time
import unittest
import uuid

from engine_harness import LiveWorld, TERMINAL


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"),
                     "Set integration binary and OpenGFX assets for dynamic registration acceptance")
class DynamicRegistrationLiveTests(unittest.TestCase):
    def setUp(self):
        self.world = LiveWorld(os.environ["OPENTTD_TEST_BINARY"], os.environ["OPENTTD_TEST_BASESET"],
                               port=int(os.environ.get("OPENTTD_TEST_PORT", "13965")), generate=True)
        self.addCleanup(self.world.close)
        self.world.start()
        self.world.finish_recovery()
        self.registrations = []

    def wait(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.03)
        self.fail(f"Native condition did not complete:\n{self.world.log_tail()}")

    def intent(self, mode="text"):
        self.world.sequence += 1
        fields = {"request_id": uuid.uuid4().hex, "request_sequence": self.world.sequence,
                  "owner": {"identity_id": "dynamic-" + uuid.uuid4().hex,
                            "run_id": "registration-" + uuid.uuid4().hex, "mode": mode}}
        self.registrations.append(fields)
        return fields

    def status(self, fields):
        return self.world.rpc("action.status", request_type="company.register", **fields)

    def complete_registration(self, fields, reply=None):
        reply = reply or self.world.rpc("company.register", **fields)
        if reply["status"] not in TERMINAL:
            reply = self.wait(lambda: (value if (value := self.status(fields))["status"] in TERMINAL else None))
        self.assertEqual(reply["status"], "completed", reply)
        actor = dict(fields["owner"], company_id=reply["result"]["company_id"])
        self.assertEqual(reply["result"]["owner"], actor)
        self.assertIs(reply["result"]["owner_bound"], True)
        self.world.completed(self.world.rpc("session.bind", actor=actor, owner_bound=True))
        return actor, reply

    def exists(self, company):
        return self.world.rpc("company.ensure", company_id=company, create=False)["status"] == "completed"

    def restart(self):
        self.world.stop()
        self.world.start(self.world.checkpoint["save_path"])
        for fields in self.registrations:
            self.world.rpc("recovery.reconcile", request_type="company.register", **fields)
        self.world.finish_recovery(self.world.accepted)

    def native_client(self, company):
        index = uuid.uuid4().hex
        directory = self.world.directory / ("native-client-" + index)
        config = directory / "config/openttd"
        data = directory / "data/openttd"
        config.mkdir(parents=True)
        data.mkdir(parents=True)
        (data / "baseset").symlink_to(self.world.baseset, target_is_directory=True)
        cfg = config / "openttd.cfg"
        cfg.write_text("[network]\nclient_name = Registration test " + index[:8] + "\n[misc]\nlanguage = english.lng\n")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("OPENTTD_INTEGRATION_")}
        environment.update(XDG_CONFIG_HOME=str(directory / "config"), XDG_DATA_HOME=str(directory / "data"))
        if os.environ.get("OPENTTD_TEST_LIBRARY_PATH"):
            environment["LD_LIBRARY_PATH"] = os.environ["OPENTTD_TEST_LIBRARY_PATH"] + os.pathsep + environment.get("LD_LIBRARY_PATH", "")
        log = (directory / "client.log").open("wb")
        process = subprocess.Popen([str(self.world.binary), "-n", f"127.0.0.1:{self.world.port}#{company}",
                                    "-v", "framebuffer", "-s", "null", "-m", "null", "-I", "OpenGFX", "-x", "-c", str(cfg)],
                                   cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT)
        def close():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.close()
        self.addCleanup(close)
        return process

    def test_first_allocation_exact_retry_name_checkpoint_and_unknown_owner(self):
        self.assertEqual(self.world.completed(self.world.rpc("hello"))["company_registration"], {"version": 1})
        self.assertFalse(self.exists(0))
        fields = self.intent()
        invalid = copy.deepcopy(fields)
        invalid["owner"]["run_id"] = uuid.uuid4().hex
        self.assertEqual(self.world.rpc("company.register", **invalid)["status"], "rejected")
        self.assertFalse(self.exists(0))
        actor, first = self.complete_registration(fields)
        self.assertEqual(actor["company_id"], 0)
        repeated = self.world.rpc("company.register", **fields)
        self.assertEqual(repeated["result"], first["result"])
        self.assertFalse(self.exists(1))
        changed = copy.deepcopy(fields)
        changed["owner"]["mode"] = "visual"
        self.assertEqual(self.world.rpc("company.register", **changed)["error"]["code"], "request_conflict")
        self.assertEqual(self.status(changed)["error"]["code"], "request_conflict")
        unknown = dict(actor, run_id="registration-" + uuid.uuid4().hex)
        self.assertEqual(self.world.rpc("session.bind", actor=unknown)["error"]["code"], "session_unbound")
        renamed = self.world.action("company.rename", {"name": "Dynamic actual allocation"}, actor=actor)
        self.assertEqual(renamed["status"], "completed", renamed)
        self.assertEqual(self.world.observe("company", actor=actor)["name"], "Dynamic actual allocation")
        self.world.checkpoint_now()
        self.restart()
        restored = self.status(fields)
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(restored["result"]["checkpoint_state"], "checkpointed")
        self.world.completed(self.world.rpc("session.bind", actor=actor, owner_bound=True))
        self.assertEqual(self.world.observe("company", actor=actor)["name"], "Dynamic actual allocation")

    def test_pre_checkpoint_crash_is_uncertain_and_does_not_allocate_again(self):
        self.world.checkpoint_now()
        fields = self.intent()
        self.complete_registration(fields)
        self.restart()
        restored = self.status(fields)
        self.assertEqual(restored["status"], "uncertain", restored)
        repeated = self.world.rpc("company.register", **fields)
        self.assertEqual(repeated["status"], "uncertain", repeated)
        self.assertFalse(self.exists(0))

    def test_deleted_company_id_reuse_retains_tombstone_across_restart(self):
        old_fields = self.intent()
        old, _ = self.complete_registration(old_fields)
        self.world.console("reset_company 1")
        self.wait(lambda: not self.exists(0))
        new_fields = self.intent()
        new, _ = self.complete_registration(new_fields)
        self.assertEqual(old["company_id"], new["company_id"])
        self.assertEqual(self.world.rpc("session.bind", actor=old)["error"]["code"], "stale_company_binding")
        self.world.checkpoint_now()
        self.restart()
        self.assertEqual(self.world.rpc("session.bind", actor=old)["error"]["code"], "stale_company_binding")
        self.world.completed(self.world.rpc("session.bind", actor=new, owner_bound=True))
        # A historical create receipt remains historical; it does not rebind.
        self.assertEqual(self.world.rpc("company.register", **old_fields)["status"], "completed")
        self.assertEqual(self.world.rpc("session.bind", actor=old)["error"]["code"], "stale_company_binding")

    def test_two_registrations_and_native_human_get_distinct_actual_companies(self):
        first, second = self.intent(), self.intent()
        human = self.native_client(254)
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(lambda fields: self.world.rpc("company.register", **fields), (first, second)))
        actors = [self.complete_registration(fields, reply)[0] for fields, reply in zip((first, second), replies)]
        roster = self.wait(lambda: (value if (value := self.world.completed(self.world.rpc("roster"))) and value[0]["company_id"] is not None else None))
        self.assertIsNone(human.poll())
        ids = [actor["company_id"] for actor in actors] + [roster[0]["company_id"]]
        self.assertEqual(len(set(ids)), 3, ids)
        self.assertEqual(set(ids), {0, 1, 2})

    def test_capture_attach_requires_exact_owner_closed_policy_and_live_key(self):
        fields = self.intent("visual")
        actor, _ = self.complete_registration(fields)
        spectator = self.native_client(255)
        roster = self.wait(lambda: self.world.completed(self.world.rpc("roster")))
        self.assertIsNone(spectator.poll())
        worker = roster[0]
        key = worker["public_key"].lower()
        denied = self.world.rpc("renderer.attach", owner=actor, native_public_key=key)
        self.assertEqual(denied["error"]["code"], "renderer_not_authorized", denied)
        granted = self.world.action("company.participant.authorize", {"participant": worker["native_handle"], "allowed": True}, actor=actor)
        self.assertEqual(granted["status"], "completed", granted)
        attached = self.world.rpc("renderer.attach", owner=actor, native_public_key=key)
        self.assertEqual(attached["status"], "completed", attached)
        self.assertIs(attached["result"]["attached"], True)
        self.assertEqual(self.world.rpc("renderer.attach", owner=actor, native_public_key=key)["status"], "completed")
        wrong_owner = dict(actor, run_id="registration-" + uuid.uuid4().hex)
        self.assertEqual(self.world.rpc("renderer.attach", owner=wrong_owner, native_public_key=key)["error"]["code"], "stale_company_binding")
        self.assertEqual(self.world.rpc("renderer.attach", owner=actor, native_public_key="0" * 64)["error"]["code"], "renderer_not_authorized")
        opened = self.world.action("company.join_policy.set", {"policy": "any"}, actor=actor)
        self.assertEqual(opened["status"], "completed", opened)
        self.assertEqual(self.world.rpc("renderer.attach", owner=actor, native_public_key=key)["error"]["code"], "renderer_not_authorized")
        closed = self.world.action("company.join_policy.set", {"policy": "listed"}, actor=actor)
        self.assertEqual(closed["status"], "completed", closed)
        second, _ = self.complete_registration(self.intent("visual"))
        grant_second = self.world.action("company.participant.authorize", {"participant": worker["native_handle"], "allowed": True}, actor=second)
        self.assertEqual(grant_second["status"], "completed", grant_second)
        self.assertEqual(self.world.rpc("renderer.attach", owner=second, native_public_key=key)["error"]["code"], "renderer_binding_conflict")
