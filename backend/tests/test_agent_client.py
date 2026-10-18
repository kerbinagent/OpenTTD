"""Portable onboarding, exact gameplay recovery and actual saved PNGs."""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.agent_client import AgentClient
from backend.capture_tools import CaptureTools
from backend.client import BackendError
from backend.errors import ServiceError
from backend.tests.test_capture_tools import DATA, FakeClient, WORLD


ROOT = Path(__file__).resolve().parents[2]
BINDING = {"world_id": "test-world", "company_id": 2, "identity_id": "test-agent",
           "run_id": "test-run", "mode": "text"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(("POST", self.path, deepcopy(body)))
        if getattr(self.server, "real_service", None) is not None:
            return self.delegate("POST", body)
        if self.path == "/v1/agent/join":
            if "intent" in body and not self.server.dynamic:
                return self.reply(400, {"error": {"code": "invalid_request", "message": "Invalid join shape"}})
            claim = self.headers.get("Authorization", "").removeprefix("Bearer ")
            saved = json.loads((self.server.state / "session.json").read_text())
            self.server.claim_durable.append(saved["claim"] == claim and len(claim) == 43)
            if self.server.dynamic:
                self.server.intent_durable.append(saved.get("create_intent", {}).get("name") == "Harbor Transit"
                    and len(saved.get("create_intent", {}).get("request_id", "")) == 32)
            if self.server.admission_error_code is not None:
                code = claim if self.server.admission_error_code == "echo_bearer" else self.server.admission_error_code
                return self.reply(503, {"error": {"code": code, "message": claim}})
            self.server.claims.add(claim)
            if self.server.lose_join_reply:
                self.server.lose_join_reply = False
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.server.dynamic:
                return self.reply(200, {"api_version": "v1", **BINDING, "mode": body["mode"],
                    "token": claim, "world_epoch": "test-epoch", "registration_id": BINDING["run_id"]})
            return self.reply(200, {"api_version": "v1", **BINDING, "mode": body["mode"], "token": "private-game-token"})
        if self.path == "/v1/actions":
            self.server.actions.append(deepcopy(body))
            if self.server.dynamic:
                saved = self.server.state / "requests" / (body["request_id"] + ".json")
                self.server.rename_durable.append(json.loads(saved.read_text()) == body)
                self.server.receipts.setdefault(body["request_id"], {
                    "request_id": body["request_id"], **deepcopy(self.server.action_receipt)})
                if self.server.lose_action_reply:
                    self.server.lose_action_reply = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                return self.reply(200, self.server.receipts[body["request_id"]])
            return self.reply(200, {"request_id": body["request_id"], "status": "completed", "result": {"cost": 75}})
        return self.reply(404, {"error": {"code": "not_found", "message": "Not found"}})

    def do_GET(self):
        self.server.calls.append(("GET", self.path, None))
        if getattr(self.server, "real_service", None) is not None:
            return self.delegate("GET")
        if self.path == "/v1/me":
            if self.server.me_error_code is not None:
                bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
                code = bearer if self.server.me_error_code == "echo_bearer" else self.server.me_error_code
                return self.reply(503, {"error": {"code": code, "message": bearer}})
            if self.server.fail_me:
                return self.reply(503, {"error": {"code": "unavailable", "message": "private-game-token"}})
            self.server.me_auth.append(self.headers.get("Authorization"))
            if self.server.dynamic:
                saved = json.loads((self.server.state / "session.json").read_text())
                return self.reply(200, {**BINDING, "mode": saved["mode"], "kind": "agent",
                    "world_epoch": self.server.me_epoch, "registration_id": BINDING["run_id"]})
            return self.reply(200, {**BINDING, "kind": "agent"})
        return self.reply(200, {"ok": True})

    def delegate(self, method, body=None):
        try:
            status, value = self.server.real_service.handle(
                method, self.path, self.headers.get("Authorization", "").removeprefix("Bearer "), body)
        except ServiceError as error:
            status, value = error.status, {"error": {"code": error.code, "message": "Request rejected"}}
        return self.reply(status, value)


class AgentClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "player"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.state = self.state
        self.server.calls, self.server.actions, self.server.claim_durable = [], [], []
        self.server.claims, self.server.me_auth = set(), []
        self.server.lose_join_reply, self.server.fail_me = False, False
        self.server.dynamic, self.server.lose_action_reply = False, False
        self.server.admission_error_code, self.server.me_error_code = None, None
        self.server.me_epoch = "test-epoch"
        self.server.intent_durable, self.server.rename_durable = [], []
        self.server.receipts = {}
        self.server.action_receipt = {"status": "completed", "result": {"success": True, "cost": 0}}
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)

    def join(self):
        return AgentClient.join(self.origin, self.state, "text")

    def test_claim_is_private_durable_before_post_and_binding_verified(self):
        player = self.join()
        self.assertEqual(player.binding, BINDING)
        self.assertEqual(self.server.claim_durable, [True])
        self.assertEqual(self.server.me_auth, ["Bearer private-game-token"])
        session = json.loads((self.state / "session.json").read_text())
        self.assertTrue(session["verified"])
        if os.name == "posix":
            self.assertEqual((self.state / "session.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        audit = (self.state / "public-io.jsonl").read_text()
        self.assertNotIn(session["claim"], audit)
        self.assertNotIn(session["token"], audit)
        counts = json.loads((self.state / "http-counts.json").read_text())
        self.assertEqual(counts["attempts"], 2)
        self.assertEqual(AgentClient.open(self.state).binding, BINDING)

    def new(self, **changes):
        self.server.dynamic = True
        return AgentClient.new(changes.get("origin", self.origin), self.state,
                               changes.get("mode", "text"), changes.get("name", "Harbor Transit"))

    def test_explicit_new_persists_immutable_intent_and_exact_name_before_http(self):
        player = self.new()
        self.assertEqual(player.setup["operation"], "new")
        self.assertEqual(player.setup["binding"], BINDING)
        self.assertEqual(player.setup["world_epoch"], "test-epoch")
        self.assertEqual(player.setup["registration_id"], BINDING["run_id"])
        self.assertTrue(player.setup["naming"]["success"])
        self.assertEqual(self.server.intent_durable, [True])
        self.assertEqual(self.server.rename_durable, [True])
        self.assertEqual(self.server.calls[0], ("POST", "/v1/agent/join", {"mode": "text", "intent": "new"}))
        saved = json.loads((self.state / "session.json").read_text())
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["token"], saved["claim"])
        self.assertEqual(self.server.actions[0]["request_id"], saved["create_intent"]["request_id"])
        self.assertEqual(self.server.actions[0]["action"], {"name": "company.rename", "parameters": {"name": "Harbor Transit"}})
        self.assertNotIn(saved["claim"], json.dumps(player.setup))
        self.assertNotIn(saved["claim"], (self.state / "public-io.jsonl").read_text())

    def test_new_rejects_legacy_pool_without_consuming_a_reserved_claim(self):
        with self.assertRaises(BackendError) as caught:
            AgentClient.new(self.origin, self.state, "text", "Harbor Transit")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.server.claims, set())
        self.assertTrue((self.state / "session.json").is_file())

    def test_new_exact_recovery_keeps_one_claim_binding_and_name_action(self):
        self.server.lose_join_reply = True
        with self.assertRaises(OSError):
            self.new()
        with self.assertRaises(ValueError):
            AgentClient.resume(self.state)
        self.assertEqual(len(self.server.calls), 1)
        player = self.new()
        replay = self.new()
        self.assertEqual(replay.setup["operation"], "recovered_new")
        self.assertEqual(player.binding, replay.binding)
        self.assertEqual(len(self.server.claims), 1)
        self.assertEqual(len(self.server.receipts), 1)
        self.assertEqual(self.server.actions[0], self.server.actions[1])
        self.assertEqual(self.server.intent_durable, [True, True])

    def test_new_refuses_different_intent_legacy_and_unrelated_state_before_http(self):
        self.new()
        calls = len(self.server.calls)
        for changes in ({"mode": "visual"}, {"name": "Different Name"}, {"origin": "http://localhost:1"}):
            with self.assertRaises(ValueError):
                self.new(**changes)
        with self.assertRaises(ValueError):
            AgentClient.join(self.origin, self.state, "text")
        self.assertEqual(len(self.server.calls), calls)
        other = Path(self.tmp.name) / "unrelated"
        other.mkdir(mode=0o700)
        (other / "notes.txt").write_text("existing work")
        with self.assertRaises(ValueError):
            AgentClient.new(self.origin, other, "text", "Harbor Transit")
        self.assertEqual({p.name for p in other.iterdir()}, {"notes.txt"})
        self.assertEqual(len(self.server.calls), calls)

    def test_substituted_or_nonstring_dynamic_token_is_rejected_before_http(self):
        self.new()
        original = json.loads((self.state / "session.json").read_text())
        self.server.calls.clear()
        for token in ("different-capability", None, 123):
            with self.subTest(token_type=type(token).__name__):
                (self.state / "session.json").write_text(json.dumps({**original, "token": token}))
                for command in (self.new, lambda: AgentClient.resume(self.state), lambda: AgentClient.open(self.state)):
                    with self.assertRaises(ValueError):
                        command()
                self.assertEqual(self.server.calls, [])
        (self.state / "session.json").write_text(json.dumps(original))
        self.assertEqual(AgentClient.resume(self.state).binding, BINDING)

    def test_new_local_name_limits_reject_before_any_company_admission(self):
        for value in ("", "a" * 32, "bad\x00name", None):
            with self.assertRaises(ValueError):
                self.new(name=value)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.server.calls, [])
        self.assertTrue(self.new(name="🚂" * 31).setup["naming"]["success"])

    def test_resume_is_read_only_and_accepts_saved_token_before_first_verification(self):
        self.server.fail_me = True
        with self.assertRaises(BackendError):
            self.new()
        saved = json.loads((self.state / "session.json").read_text())
        self.assertFalse(saved["verified"])
        self.assertEqual(self.server.actions, [])
        self.server.fail_me = False
        self.server.calls.clear()
        with patch("backend.agent_client.secrets.token_urlsafe", side_effect=AssertionError("no new secret")):
            player = AgentClient.resume(self.state)
        self.assertEqual(self.server.calls, [("GET", "/v1/me", None)])
        self.assertEqual(player.setup["operation"], "resumed")
        self.assertEqual(player.setup["naming"]["status"], "not_started")
        self.assertEqual(self.server.actions, [])
        self.assertTrue(AgentClient.open(self.state).session["verified"])

    def test_resume_missing_state_and_world_mismatch_never_create(self):
        with self.assertRaises(FileNotFoundError):
            AgentClient.resume(self.state)
        self.assertFalse(self.state.exists())
        self.new()
        self.server.calls.clear()
        self.server.me_epoch = "replacement-epoch"
        with self.assertRaises(ValueError):
            AgentClient.resume(self.state)
        self.assertEqual(self.server.calls, [("GET", "/v1/me", None)])
        self.assertEqual(len(self.server.claims), 1)

    def test_legacy_state_resumes_without_admission_or_rename(self):
        self.join()
        self.server.calls.clear()
        with self.assertRaises(ValueError):
            AgentClient.new(self.origin, self.state, "text", "Harbor Transit")
        self.assertEqual(self.server.calls, [])
        player = AgentClient.resume(self.state)
        self.assertEqual(player.binding, BINDING)
        self.assertEqual(player.setup["operation"], "resumed")
        self.assertEqual(self.server.calls, [("GET", "/v1/me", None)])

    def test_lost_initial_name_reply_keeps_known_company_and_exact_recovery(self):
        self.server.lose_action_reply = True
        player = self.new()
        self.assertEqual(player.binding, BINDING)
        self.assertEqual(player.setup["naming"]["status"], "transport_unknown")
        action = deepcopy(self.server.actions[0])
        self.server.calls.clear()
        resumed = AgentClient.resume(self.state)
        self.assertEqual(resumed.setup["naming"]["status"], "transport_unknown")
        self.assertEqual(self.server.calls, [("GET", "/v1/me", None)])
        recovered = self.new()
        self.assertTrue(recovered.setup["naming"]["success"])
        self.assertEqual(self.server.actions, [action, action])
        self.assertEqual(len(self.server.receipts), 1)
        self.assertEqual(len(self.server.claims), 1)

    def test_native_name_rejection_or_uncertain_result_keeps_ownership(self):
        self.server.action_receipt = {"status": "completed", "result": {"success": False, "error": "Name is in use"}}
        player = self.new()
        self.assertFalse(player.setup["naming"]["success"])
        self.assertEqual(player.setup["naming"]["error"], "Name is in use")
        self.assertEqual(AgentClient.resume(self.state).binding, BINDING)
        self.assertEqual(len(self.server.claims), 1)
        self.server.receipts.clear()
        self.server.action_receipt = {"status": "uncertain"}
        player = self.new()
        self.assertEqual(player.setup["naming"]["status"], "uncertain")
        self.assertEqual(len(self.server.claims), 1)

    def test_generated_claim_works_with_actual_admission_and_store_contract(self):
        from backend.tests.test_admission import AdmissionTests
        fixture = AdmissionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.server.real_service = fixture.service
        player = self.join()
        self.assertEqual(player.binding["company_id"], 0)
        self.assertEqual(player.binding["mode"], "text")
        self.assertEqual(len(fixture.claims()), 1)
        self.assertEqual(self.join().binding, player.binding)
        self.assertEqual(len(fixture.claims()), 1)

    def test_lost_join_reply_reuses_durable_claim_after_restart(self):
        self.server.lose_join_reply = True
        with self.assertRaises(OSError):
            self.join()
        saved_claim = json.loads((self.state / "session.json").read_text())["claim"]
        with self.assertRaises(ValueError):
            AgentClient.open(self.state)
        self.assertEqual(self.join().binding, BINDING)
        self.assertEqual(self.server.claims, {saved_claim})
        self.assertEqual(self.server.claim_durable, [True, True])

    def test_response_saved_before_me_failure_and_resumed_without_another_join(self):
        self.server.fail_me = True
        with self.assertRaises(BackendError):
            self.join()
        saved = json.loads((self.state / "session.json").read_text())
        self.assertFalse(saved["verified"])
        self.assertIn("token", saved)
        audit = (self.state / "public-io.jsonl").read_text()
        self.assertNotIn(saved["token"], audit)
        self.assertNotIn(saved["claim"], audit)
        self.server.fail_me = False
        self.assertEqual(self.join().binding, BINDING)
        self.assertEqual(len(self.server.claim_durable), 1)

    def test_saved_state_refuses_origin_mode_and_insecure_or_malformed_state(self):
        self.join()
        calls = len(self.server.calls)
        for origin, mode in ((self.origin, "visual"), ("http://localhost:1", "text")):
            with self.assertRaises(ValueError):
                AgentClient.join(origin, self.state, mode)
        self.assertEqual(len(self.server.calls), calls)
        if os.name == "posix":
            (self.state / "session.json").chmod(0o644)
            with self.assertRaises(ValueError):
                AgentClient.open(self.state)
            (self.state / "session.json").chmod(0o600)
        (self.state / "session.json").write_text("{broken")
        with self.assertRaises(ValueError):
            AgentClient.open(self.state)

    def test_symlink_state_or_credential_is_rejected(self):
        self.join()
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(ValueError):
            AgentClient.open(alias)
        target = self.state / "other.json"
        (self.state / "session.json").rename(target)
        (self.state / "session.json").symlink_to(target)
        with self.assertRaises(OSError):
            AgentClient.open(self.state)

    def test_actions_are_saved_before_send_and_retried_unchanged(self):
        player = self.join()
        body = {"request_id": "actor-rail", "action": {"name": "rail.build", "parameters": {
            "tile": {"x": 447, "y": 463}, "track": "right"}}}
        player.request("POST", "/v1/%61ctions", body)
        body["action"]["parameters"]["track"] = "upper"
        reopened = AgentClient.open(self.state)
        reopened.retry("actor-rail", wait_ms=0)
        self.assertEqual(self.server.actions[0], self.server.actions[1])
        self.assertEqual(self.server.actions[1]["action"]["parameters"]["track"], "right")
        with self.assertRaises(ValueError):
            reopened.submit(body)
        saved = self.state / "requests" / "actor-rail.json"
        self.assertTrue(saved.is_file())
        audit = (self.state / "public-io.jsonl").read_text()
        self.assertIn('"kind":"exact_replay"', audit)
        self.assertIn('"track":"right"', audit)

    def test_lost_action_reply_keeps_original_id_and_parameters(self):
        player = self.join()
        with patch.object(player.client, "request", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                player.action("rail.build", {"tile": 7, "track": "right"}, request_id="lost-action")
        result = AgentClient.open(self.state).retry("lost-action", wait_ms=0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.server.actions[0]["request_id"], "lost-action")

    def test_raw_admission_paths_cannot_print_or_log_join_secrets(self):
        player = self.join()
        for path in ("/v1/agent/join", "/v1/agent/%6aoin", "/v1/agent/join/?x=1"):
            with self.assertRaises(ValueError):
                player.request("POST", path, {"mode": "text"})
        self.assertEqual(len(self.server.claims), 1)

    def test_known_secrets_are_removed_from_error_audits_and_selected_output(self):
        player = self.join()
        token, claim = player.session["token"], player.session["claim"]
        with patch.object(player.client, "request", side_effect=BackendError(503, "unavailable", token + claim)):
            with self.assertRaises(BackendError):
                player.request("GET", "/v1/capabilities")
        fake = FakeClient()
        fake.capture_error = BackendError(503, "unavailable", token + claim)
        player.capture_tools = CaptureTools(fake, self.state)
        metadata = player.selected("world", WORLD)
        self.assertNotIn(token, json.dumps(metadata))
        self.assertNotIn(claim, json.dumps(metadata))
        audit = (self.state / "public-io.jsonl").read_text()
        self.assertNotIn(token, audit)
        self.assertNotIn(claim, audit)

    def test_selected_png_recovery_reuses_existing_adapter_journal(self):
        player = self.join()
        fake = FakeClient()
        player.capture_tools = CaptureTools(fake, self.state)
        fake.capture_error = TimeoutError()
        first = player.selected("world", WORLD, key="chosen-site")
        self.assertEqual(first["status"], "failed")
        fake.capture_error = None
        restarted = AgentClient.open(self.state)
        restarted.capture_tools = CaptureTools(fake, self.state)
        result = restarted.retry_observation(first["observation_request_id"])
        self.assertEqual(Path(result["image_path"]).read_bytes(), DATA)
        self.assertEqual(fake.observations[0], fake.observations[1])
        self.assertEqual(fake.serial, 1)
        same = restarted.selected("world", WORLD, key="chosen-site")
        self.assertEqual(same["observation_request_id"], first["observation_request_id"])
        self.assertEqual(fake.serial, 1)
        self.assertFalse(any("data" in key for key in result))

    def test_legacy_capture_control_and_ui_use_the_same_public_paths(self):
        player = self.join()
        image_id = "a" * 32
        replies = {"/v1/view/images": {"image_id": image_id, "purpose": "interactive"},
                   "/v1/view/controls": {"ok": True}, "/v1/actions": {
                       "request_id": "click", "status": "completed"}}
        calls = []

        def request(method, path, body=None):
            calls.append((method, path, deepcopy(body)))
            return replies[path]

        with patch.object(player.client, "request", side_effect=request), patch.object(player.client, "image", return_value=DATA):
            original_session = (self.state / "session.json").read_bytes()
            result = player.capture("session")
            player.control(image_id, {"operation": "pan", "dx": 10, "dy": 0})
            player.ui(image_id, {"operation": "click", "x": 10, "y": 10}, request_id="click")
        self.assertEqual(Path(result["image_path"]).read_bytes(), DATA)
        self.assertEqual((self.state / "session.json").read_bytes(), original_session)
        self.assertTrue((self.state / "images" / "capture-session.json").is_file())
        self.assertEqual(calls[1][1], "/v1/view/controls")
        self.assertEqual(calls[2][2]["action"]["name"], "ui.apply")
        self.assertEqual(player.actions.load("click")["action"]["parameters"]["image_id"], image_id)

    def test_wait_is_bounded_and_does_not_resubmit_or_poll_at_100ms(self):
        player = self.join()
        queued = {"request_id": "pending", "status": "queued"}
        with patch.object(player, "status", return_value=queued) as status, patch("backend.agent_client.time.sleep") as sleep:
            self.assertEqual(player.wait("pending", wait_ms=0), queued)
            status.assert_called_once()
            sleep.assert_not_called()
        with patch.object(player, "status", side_effect=[queued, {**queued, "status": "completed"}]), \
                patch("backend.agent_client.time.sleep") as sleep:
            player.wait("pending", wait_ms=20_000)
            sleep.assert_called_once_with(0.5)

    def test_cli_errors_hide_bearer_codes_for_new_and_resume_preserving_recovery_codes(self):
        self.server.dynamic = True
        self.server.admission_error_code = "echo_bearer"
        # A canonical all-lowercase claim also resembles an ordinary error
        # identifier, so a shape check alone cannot protect the CLI boundary.
        with patch("backend.agent_client.secrets.token_urlsafe", return_value="a" * 42 + "c"):
            with self.assertRaises(BackendError):
                self.new()
        secret = json.loads((self.state / "session.json").read_text())["claim"]
        command = [sys.executable, "-S", "-m", "backend.agent_client", "--state", str(self.state)]
        creation = ["new", "--origin", self.origin, "--mode", "text", "--name", "Harbor Transit"]
        result = subprocess.run(command + creation, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stderr)["error"], {"status": 503, "code": "backend_error"})
        self.server.admission_error_code = "join_not_ready"
        result = subprocess.run(command + creation, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "join_not_ready")
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.server.admission_error_code = None
        self.new()
        self.server.me_error_code = "echo_bearer"
        self.server.calls.clear()
        result = subprocess.run(command + ["resume"], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["error"], {"status": 503, "code": "backend_error"})
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(self.server.calls, [("GET", "/v1/me", None)])

    def test_cli_new_and_resume_have_safe_explicit_outputs(self):
        self.server.dynamic = True
        command = [sys.executable, "-S", "-m", "backend.agent_client", "--state", str(self.state)]
        result = subprocess.run(command + ["new", "--origin", self.origin, "--mode", "visual", "--name", "Harbor Transit"],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        setup = json.loads(result.stdout)
        self.assertEqual(setup["operation"], "new")
        self.assertEqual(setup["binding"]["mode"], "visual")
        secret = json.loads((self.state / "session.json").read_text())["claim"]
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.server.calls.clear()
        result = subprocess.run(command + ["resume"], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["operation"], "resumed")
        self.assertEqual(self.server.calls, [("GET", "/v1/me", None)])
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_stdlib_cli_from_copied_bundle_outside_repository(self):
        extracted = Path(self.tmp.name) / "download"
        package = extracted / "backend"
        package.mkdir(parents=True)
        for name in ("__init__.py", "agent_client.py", "client.py", "observations.py", "errors.py", "json_tools.py",
                     "player_actions.py", "player_mechanics.py", "capture_tools.py"):
            shutil.copyfile(ROOT / "backend" / name, package / name)
        command = [sys.executable, "-S", "-m", "backend.agent_client", "--state", str(self.state)]
        result = subprocess.run(command + ["join", "--origin", self.origin, "--mode", "text"],
                                cwd=extracted, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), BINDING)
        self.assertNotIn("private-game-token", result.stdout + result.stderr)
        claim = json.loads((self.state / "session.json").read_text())["claim"]
        self.assertNotIn(claim, result.stdout + result.stderr)
        result = subprocess.run(command + ["action", "company.rename", '{"name":"Own choices"}', "--id", "own-name"],
                                cwd=extracted, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")
        self.server.fail_me = True
        result = subprocess.run(command + ["join", "--origin", self.origin, "--mode", "text"],
                                cwd=extracted, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("private-game-token", result.stdout + result.stderr)
        self.assertNotIn(claim, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
