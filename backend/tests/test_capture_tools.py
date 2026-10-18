"""Capture tool transport, immutable intents, and partial-success evidence."""

import base64
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.capture_tools import CaptureTools, MAX_MESSAGE_BYTES, PROTOCOL_VERSION, serve, tool_definitions
from backend.client import BackendError
from backend.json_tools import dumps
from backend.observations import ObservationImage, validate_request
from backend.player_actions import SavedActions
from backend.tests.test_renderer import png


ROOT = Path(__file__).resolve().parents[2]
WORLD = {"selection": {"kind": "map_center", "tile": {"x": 12, "y": 23}, "zoom_level": "normal"},
         "output": {"width": 64, "height": 64}, "labels": {"town_names": False}}
INFO = {"panel": "company_finances", "presentation": {"layout": "compact"}}
ACTION = {"request_id": "build-17", "action": {"name": "road.build", "parameters": {
    "start": {"x": 12, "y": 23}, "end": {"x": 13, "y": 23}}}}
DATA = png(64, 64)


class FakeClient:
    def __init__(self):
        self.requests = []
        self.observations = []
        self.serial = 0
        self.status = "completed"
        self.capture_error = None
        self.action_error = None
        self.pending = False
        self.context_resets = 0

    def reset_observation_context(self):
        self.context_resets += 1

    def new_observation_request(self, kind, parameters):
        self.serial += 1
        return validate_request(kind, {**parameters, "observation_generation": "a" * 32,
                                       "observation_request_id": f"obs1:1000000000000:{self.serial:032x}"})

    def observe_world(self, request):
        return self._observe("world", request)

    def observe_native_info(self, request):
        return self._observe("native_info", request)

    def _observe(self, kind, request):
        self.observations.append((kind, deepcopy(request)))
        if self.capture_error:
            raise self.capture_error
        if self.pending:
            return {"status": "pending", "observation_request_id": request["observation_request_id"],
                    "reason": "renderer_busy", "retry_after_ms": 25, "private_frame": 99}
        return ObservationImage(DATA, "b" * 32, "c" * 32, "a" * 32, kind, 64, 64,
                                "satisfied" if "after_request_id" in request else "none", False, 0)

    def request(self, method, path, body=None):
        self.requests.append((method, path, deepcopy(body)))
        if self.action_error:
            raise self.action_error
        return {"request_id": body["request_id"] if body else path.rsplit("/", 1)[1],
                "status": self.status, "result": {"success": True}}


class CaptureToolsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = FakeClient()
        self.adapter = CaptureTools(self.client, self.directory.name, audit=True)

    def test_observation_image_blocks_exact_retry_and_distinct_schemas(self):
        schemas = {tool["name"]: tool["inputSchema"] for tool in tool_definitions()}
        self.assertTrue(all(schema.get("type") == "object" for schema in schemas.values()))
        self.assertIn("selection", schemas["observe_world"]["properties"])
        self.assertNotIn("panel", schemas["observe_world"]["properties"])
        self.assertNotIn("observation_generation", schemas["observe_world"]["properties"])
        self.assertNotIn("observation_request_id", schemas["observe_world"]["required"])
        self.assertIn("anyOf", schemas["observe_native_info"])
        first = self.adapter.call("observe_world", WORLD, call_id=12)
        self.assertFalse(first["isError"])
        self.assertEqual(base64.b64decode(first["content"][1]["data"]), DATA)
        metadata = first["structuredContent"]
        self.assertEqual(metadata["image_sha256"], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(metadata["purpose"], "observation")
        self.assertNotIn("image_path", metadata)
        retry = self.adapter.call("retry_observation", {"observation_request_id": metadata["observation_request_id"]})
        self.assertFalse(retry["isError"])
        self.assertEqual(self.client.observations[0], self.client.observations[1])
        self.assertEqual(self.client.serial, 1)
        panel = self.adapter.call("observe_native_info", INFO)
        self.assertEqual(panel["structuredContent"]["kind"], "native_info")
        self.assertEqual(panel["content"][1]["type"], "image")

    def test_pending_and_failure_keep_retry_identity_without_image(self):
        self.client.pending = True
        pending = self.adapter.call("observe_world", WORLD)
        self.assertEqual(len(pending["content"]), 1)
        self.assertEqual(pending["structuredContent"]["reason"], "renderer_busy")
        self.assertNotIn("private_frame", pending["structuredContent"])
        self.client.pending = False
        self.client.capture_error = BackendError(503, "renderer_unavailable", "Renderer unavailable")
        failed = self.adapter.call("retry_observation", {
            "observation_request_id": pending["structuredContent"]["observation_request_id"]})
        self.assertTrue(failed["isError"])
        self.assertEqual(failed["structuredContent"]["observation_request_id"], pending["structuredContent"]["observation_request_id"])
        self.assertEqual(len(failed["content"]), 1)
        self.assertEqual(self.client.observations[0], self.client.observations[1])

    def test_expiry_refreshes_only_next_new_read_without_rewriting_saved_retry(self):
        self.client.capture_error = BackendError(410, "observation_expired", "Observation expired")
        expired = self.adapter.call("observe_world", WORLD)
        request_id = expired["structuredContent"]["observation_request_id"]
        self.assertEqual(self.client.context_resets, 1)
        self.client.capture_error = None
        retry = self.adapter.call("retry_observation", {"observation_request_id": request_id})
        self.assertFalse(retry["isError"])
        self.assertEqual(self.client.observations[0], self.client.observations[1])
        self.assertEqual(self.client.serial, 1)

    def test_caller_known_key_recovers_after_lost_tool_reply_and_restart(self):
        parameters = {**WORLD, "observation_key": "inspect-road-17"}
        # Treat the entire first returned tool result as lost to its caller.
        self.adapter.call("observe_world", parameters)
        restarted = CaptureTools(self.client, self.directory.name)
        recovered = restarted.call("observe_world", parameters)
        self.assertFalse(recovered["isError"])
        self.assertEqual(recovered["structuredContent"]["observation_key"], "inspect-road-17")
        self.assertEqual(self.client.observations[0], self.client.observations[1])
        self.assertNotIn("observation_key", self.client.observations[1][1])
        self.assertEqual(self.client.serial, 1)
        changed = {**parameters, "labels": {"town_names": True}}
        self.assertTrue(restarted.call("observe_world", changed)["isError"])
        self.assertTrue(restarted.call("observe_native_info", {**INFO, "observation_key": "inspect-road-17"})["isError"])
        self.assertEqual(len(self.client.observations), 2)

    def test_composed_key_conflict_rejects_before_new_action_and_null_key_rejects(self):
        self.adapter.call("observe_world", {**WORLD, "observation_key": "existing-read"})
        result = self.adapter.call("act_and_observe", {"request": ACTION,
            "observation": {"kind": "world", "parameters": {**WORLD, "observation_key": "existing-read"}}})
        self.assertTrue(result["isError"])
        self.assertEqual(self.client.requests, [])
        self.assertTrue(self.adapter.call("observe_world", {**WORLD, "observation_key": None})["isError"])

    def test_malformed_public_replies_become_tool_errors_and_keep_known_receipt(self):
        with patch.object(self.client, "request", return_value=[]):
            result = self.adapter.call("wait_action", {"request_id": "known", "wait_ms": 0})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_response")
        with patch.object(self.client, "request", return_value={"request_id": ACTION["request_id"],
                "status": "completed", "result": {"error": "\ud800"}}):
            result = self.adapter.call("submit_action", {"request": ACTION})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_response")
        with patch.object(self.client, "new_observation_request", side_effect=AttributeError("bad capabilities")):
            result = self.adapter.call("act_and_observe", {"request": ACTION,
                "observation": {"kind": "world", "parameters": WORLD}, "wait_ms": 0})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["receipt"]["status"], "completed")
            self.assertEqual(result["structuredContent"]["observation"]["error"]["code"], "invalid_response")

    def test_lost_action_reply_is_saved_and_restart_retries_exactly(self):
        self.client.action_error = TimeoutError()
        failed = self.adapter.call("submit_action", {"request": ACTION})
        self.assertTrue(failed["isError"])
        self.assertEqual(self.adapter.actions.load(ACTION["request_id"]), ACTION)
        self.client.action_error = None
        restarted = CaptureTools(self.client, self.directory.name)
        retried = restarted.call("retry_action", {"request_id": ACTION["request_id"]})
        self.assertEqual(retried["structuredContent"]["submission"]["kind"], "exact_replay")
        self.assertEqual(self.client.requests[0], self.client.requests[1])
        changed = deepcopy(ACTION)
        changed["action"]["parameters"]["start"]["x"] += 1
        rejected = restarted.call("submit_action", {"request": changed})
        self.assertTrue(rejected["isError"])
        self.assertEqual(len(self.client.requests), 2)

    def test_composition_retains_receipt_when_capture_fails_and_retry_only_observes(self):
        self.client.capture_error = BackendError(503, "renderer_unavailable", "Renderer unavailable")
        result = self.adapter.call("act_and_observe", {"request": ACTION,
            "observation": {"kind": "world", "parameters": WORLD}, "wait_ms": 0})
        self.assertTrue(result["isError"])
        value = result["structuredContent"]
        self.assertEqual(value["receipt"]["status"], "completed")
        self.assertEqual(value["submission"]["kind"], "new_intent")
        self.assertEqual(value["observation"]["status"], "failed")
        self.assertEqual(self.client.observations[0][1]["after_request_id"], ACTION["request_id"])
        self.client.capture_error = None
        retry = self.adapter.call("retry_observation", {"observation_request_id": value["observation"]["observation_request_id"]})
        self.assertFalse(retry["isError"])
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(self.adapter.actions.load(ACTION["request_id"]), ACTION)

    def test_pending_action_does_not_capture_and_wait_does_not_submit(self):
        self.client.status = "queued"
        result = self.adapter.call("act_and_observe", {"request": ACTION,
            "observation": {"kind": "native_info", "parameters": INFO}, "wait_ms": 0})
        self.assertEqual(result["structuredContent"]["receipt"]["status"], "queued")
        self.assertEqual(self.client.observations, [])
        self.client.status = "completed"
        waited = self.adapter.call("wait_action", {"request_id": ACTION["request_id"], "wait_ms": 0})
        self.assertEqual(waited["structuredContent"]["receipt"]["status"], "completed")
        self.assertEqual([row[0] for row in self.client.requests], ["POST", "GET"])

    def test_invalid_observation_or_ui_is_rejected_before_action(self):
        for parameters in ({**WORLD, "observation_generation": "a" * 32},
                           {**WORLD, "after_request_id": "different-action"},
                           {**WORLD, "output": {"width": 1, "height": 1}}):
            result = self.adapter.call("act_and_observe", {"request": ACTION,
                "observation": {"kind": "world", "parameters": parameters}})
            self.assertTrue(result["isError"])
        ui = {"request_id": "click-1", "action": {"name": "ui.apply", "parameters": {}}}
        self.assertTrue(self.adapter.call("submit_action", {"request": ui})["isError"])
        self.assertEqual(self.client.requests, [])

    def test_transport_emission_is_logged_only_after_flush(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "observe_world", "arguments": WORLD}},
        ]
        source = io.BytesIO(b"".join((dumps(message) + "\n").encode() for message in messages))
        output = io.BytesIO()
        self.assertEqual(serve(self.adapter, source, output), 0)
        rows = [json.loads(line) for line in (Path(self.directory.name) / "capture-tools.jsonl").read_text().splitlines()]
        ready = next(row for row in rows if row["event"] == "image_ready")
        emitted = next(row for row in rows if row["event"] == "image_emission")
        self.assertEqual(emitted["state"], "emitted")
        self.assertEqual(emitted["mcp_call_id"], 2)
        self.assertEqual(ready["delivery_id"], emitted["delivery_id"])
        self.assertEqual(ready["image_sha256"], emitted["image_sha256"])
        self.assertNotIn("model_received", emitted)
        result = self.adapter.call("observe_world", WORLD)
        with patch.object(self.adapter, "call", return_value=result):
            self.assertEqual(serve(self.adapter, io.BytesIO(source.getvalue()), BrokenAfterInitialize()), 1)
        rows = [json.loads(line) for line in (Path(self.directory.name) / "capture-tools.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["event"], "image_emission")
        self.assertEqual(rows[-1]["state"], "failed")

    def test_corrupted_observation_journal_cannot_change_retry_identity(self):
        result = self.adapter.call("observe_world", WORLD)
        request_id = result["structuredContent"]["observation_request_id"]
        path = self.adapter._path("observations", request_id)
        saved = json.loads(path.read_text())
        saved["request"]["observation_request_id"] = "obs1:1000000000000:" + "f" * 32
        path.write_text(json.dumps(saved))
        retry = self.adapter.call("retry_observation", {"observation_request_id": request_id})
        self.assertTrue(retry["isError"])
        self.assertEqual(len(self.client.observations), 1)

    def test_bounded_protocol_input_and_initialization(self):
        source = io.BytesIO(b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n" +
                            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
        output = io.BytesIO()
        self.assertEqual(serve(self.adapter, source, output), 0)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([reply["error"]["code"] for reply in replies], [-32600, -32002])

    def test_malformed_tool_name_and_non_utf8_id_do_not_terminate_server(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": []}},
            {"jsonrpc": "2.0", "id": "\ud800", "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        source = io.BytesIO(b"".join((json.dumps(message) + "\n").encode() for message in messages))
        output = io.BytesIO()
        self.assertEqual(serve(self.adapter, source, output), 0)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn("error", replies[1])
        self.assertIn("error", replies[2])
        self.assertIsNone(replies[2]["id"])
        self.assertEqual(replies[3], {"jsonrpc": "2.0", "id": 4, "result": {}})


class BrokenAfterInitialize(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        if self.flushes == 2:
            raise BrokenPipeError()


class SavedActionsTest(unittest.TestCase):
    def test_explicit_new_intent_and_recovery_preserve_canonical_types_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SavedActions(directory)
            parameters = {"vehicle_id": {"id": 1, "generation": 4, "engine_instance_id": "boot-1"},
                          "order_version": "opaque-old-token", "value": True}
            first = journal.new_intent("vehicle.orders.remove", parameters)
            second = journal.new_intent("vehicle.orders.remove", parameters)
            self.assertNotEqual(first["request_id"], second["request_id"])
            parameters["vehicle_id"]["generation"] = 999
            self.assertEqual(journal.load(first["request_id"])["action"]["parameters"]["vehicle_id"]["generation"], 4)
            self.assertFalse(journal.save(first))
            changed = deepcopy(first)
            changed["action"]["parameters"]["value"] = 1
            with self.assertRaises(ValueError):
                journal.save(changed)
            self.assertEqual(SavedActions(directory).load(first["request_id"]), first)


class CaptureStdioWireTest(unittest.TestCase):
    def test_real_stdio_tool_result_carries_exact_authenticated_http_png(self):
        exchanges = []

        class PublicAPI(BaseHTTPRequestHandler):
            def do_GET(self):
                exchanges.append(("GET", self.path, self.headers.get("X-OpenTTD-Attempt-Id"), None))
                body = json.dumps({"selected_observations": {"observation_generation": "a" * 32}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                exchanges.append(("POST", self.path, self.headers.get("X-OpenTTD-Attempt-Id"), request))
                self.server.test_case.assertEqual(self.headers.get("Authorization"), "Bearer own-secret")
                self.server.test_case.assertEqual(self.headers.get("Accept"), "image/png")
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(DATA)))
                headers = {"Observation-Id": "b" * 32, "Image-Id": "c" * 32, "Observation-Generation": "a" * 32,
                           "Capture-Kind": "world" if self.path.endswith("/world") else "native_info",
                           "Image-Purpose": "observation", "Image-Width": "64", "Image-Height": "64",
                           "Causal-State": "none", "Image-Reused": "false", "Image-Age-Ms": "0"}
                for name, value in headers.items():
                    self.send_header("X-OpenTTD-" + name, value)
                self.send_header("X-Private-Frame", "must-not-escape")
                self.end_headers()
                self.wfile.write(DATA)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), PublicAPI)
        server.test_case = self
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "credential"
            token.write_text("own-secret")
            token.chmod(0o600)
            artifacts = Path(directory) / "own-artifacts"
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": "world-call", "method": "tools/call", "params": {"name": "observe_world", "arguments": WORLD}},
                {"jsonrpc": "2.0", "id": "panel-call", "method": "tools/call", "params": {"name": "observe_native_info", "arguments": INFO}},
            ]
            process = subprocess.run([sys.executable, "-m", "backend.capture_tools", "--origin",
                f"http://127.0.0.1:{server.server_address[1]}", "--token-file", str(token),
                "--artifacts", str(artifacts), "--audit"], cwd=ROOT,
                input=b"".join((dumps(message) + "\n").encode() for message in messages),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            self.assertNotIn(b"own-secret", process.stdout)
            replies = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual(len(replies), 4)
            self.assertEqual(replies[0]["result"]["protocolVersion"], PROTOCOL_VERSION)
            self.assertEqual({item["name"] for item in replies[1]["result"]["tools"]} & {"observe_world", "observe_native_info"}, {"observe_world", "observe_native_info"})
            rows = [json.loads(line) for line in (artifacts / "capture-tools.jsonl").read_text().splitlines()]
            for reply in replies[2:]:
                result = reply["result"]
                self.assertFalse(result["isError"], result)
                self.assertEqual(result["content"][1]["type"], "image")
                image = base64.b64decode(result["content"][1]["data"], validate=True)
                metadata = result["structuredContent"]
                self.assertEqual(image, DATA)
                self.assertEqual(hashlib.sha256(image).hexdigest(), metadata["image_sha256"])
                attempt = next(row for row in rows if row["event"] == "http_attempt" and row["http_attempt_id"] == metadata["http_attempt_id"])
                self.assertEqual(attempt["mcp_call_id"], reply["id"])
                emission = next(row for row in rows if row["event"] == "image_emission" and row["delivery_id"] == metadata["delivery_id"])
                self.assertEqual(emission["state"], "emitted")
                self.assertNotIn("must-not-escape", json.dumps(result))
            self.assertEqual([(method, path) for method, path, _, _ in exchanges], [
                ("GET", "/v1/capabilities"), ("POST", "/v1/view/observe/world"), ("POST", "/v1/view/observe/native-info")])
            self.assertTrue(all(attempt for _, _, attempt, _ in exchanges))
            self.assertEqual(len({attempt for _, _, attempt, _ in exchanges}), 3)
            self.assertEqual(len([row for row in rows if row["event"] == "http_attempt"]), 3)


if __name__ == "__main__":
    unittest.main()
