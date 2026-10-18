"""Direct observation responses remain images with finite workflow metadata."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import struct
import threading
import time
import unittest
import zlib

from backend.client import BackendClient, BackendError
from backend.observations import ObservationImage


GENERATION = "a" * 32


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\0" + b"\x12\x34\x56" * 64) * 64)) + chunk(b"IEND", b""))


class ObservationClientTests(unittest.TestCase):
    def setUp(self):
        owner = self
        self.requests = []
        self.data = png()
        self.status = 200
        self.mime = "image/png"
        self.bootstrap = {"selected_observations": {"observation_generation": GENERATION}}
        self.headers = {
            "Observation-Id": "b" * 32, "Image-Id": "c" * 32,
            "Observation-Generation": GENERATION, "Capture-Kind": "world",
            "Image-Width": "64", "Image-Height": "64", "Causal-State": "none",
            "Image-Reused": "false", "Image-Age-Ms": "12", "Image-Purpose": "observation",
            "Native-Frame": "999",  # Never propagated by the public client.
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(("GET", self.path, None, dict(self.headers)))
                raw = json.dumps(owner.bootstrap).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body, dict(self.headers)))
                self.send_response(owner.status)
                self.send_header("Content-Type", owner.mime)
                self.send_header("Content-Length", str(len(owner.data)))
                for name, value in owner.headers.items():
                    self.send_header("X-OpenTTD-" + name, value)
                self.end_headers()
                self.wfile.write(owner.data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = BackendClient("http://127.0.0.1:" + str(self.server.server_port), "secret")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def body(self, **extra):
        return {"observation_generation": GENERATION,
                "observation_request_id": f"obs1:{time.time_ns() // 1_000_000}:" + "d" * 32,
                "selection": {"kind": "map_center", "tile": {"x": 10, "y": 20},
                              "zoom_level": "normal"},
                "output": {"width": 64, "height": 64}, **extra}

    def test_direct_png_one_post_and_allowlisted_metadata_with_attempt_trace(self):
        attempts = []
        self.client.on_http_attempt = attempts.append
        body = self.body()
        image = self.client.observe_world(body)
        self.assertIsInstance(image, ObservationImage)
        self.assertEqual(image.data, self.data)
        self.assertEqual((image.width, image.height, image.kind), (64, 64, "world"))
        self.assertEqual(len(self.requests), 1)
        method, path, sent, headers = self.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/view/observe/world"))
        self.assertEqual(sent["observation_request_id"], body["observation_request_id"])
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(attempts[0]["response_bytes"], len(self.data))
        self.assertEqual(attempts[0]["attempt_id"], headers["X-Openttd-Attempt-Id"])
        self.assertNotIn("secret", json.dumps(attempts))
        self.assertNotIn("999", json.dumps(image.__dict__, default=lambda value: "<bytes>"))

    def test_new_intent_bootstraps_once_and_explicit_reset_reloads_context(self):
        parameters = {name: value for name, value in self.body().items()
                      if name not in ("observation_generation", "observation_request_id")}
        first = self.client.new_observation_request("world", parameters)
        second = self.client.new_observation_request("world", parameters)
        self.assertNotEqual(first["observation_request_id"], second["observation_request_id"])
        self.assertEqual(first["observation_generation"], GENERATION)
        self.assertEqual([r[0] for r in self.requests], ["GET"])
        self.client.reset_observation_context()
        self.client.new_observation_request("world", parameters)
        self.assertEqual([r[0] for r in self.requests], ["GET", "GET"])

    def test_pending_stays_json_and_malformed_pending_never_becomes_an_image(self):
        body = self.body()
        pending = {"status": "pending", "observation_request_id": body["observation_request_id"],
                   "reason": "replica_pending", "retry_after_ms": 100}
        self.status, self.mime, self.data = 202, "application/json", json.dumps(pending).encode()
        self.assertEqual(self.client.observe_world(body), pending)
        for data in (b"[]", b"{", json.dumps({**pending, "native_frame": 1}).encode(),
                     json.dumps({**pending, "reason": []}).encode(),
                     json.dumps({**pending, "reason": {}}).encode(),
                     json.dumps({**pending, "observation_request_id": "different"}).encode()):
            self.data = data
            with self.assertRaises(BackendError):
                self.client.observe_world(body)

    def test_malformed_bootstrap_does_not_poison_cached_context(self):
        parameters = {name: value for name, value in self.body().items()
                      if name not in ("observation_generation", "observation_request_id")}
        for bootstrap in ([], {"selected_observations": {"observation_generation": 11111111111111111111111111111111}}):
            self.bootstrap = bootstrap
            with self.assertRaises(BackendError):
                self.client.new_observation_request("world", parameters)
        self.bootstrap = {"selected_observations": {"observation_generation": GENERATION}}
        self.assertEqual(self.client.new_observation_request("world", parameters)["observation_generation"], GENERATION)

    def test_wrong_purpose_generation_dimensions_and_causal_state_are_rejected(self):
        for name, value in (("Image-Purpose", "interaction"),
                            ("Observation-Generation", "e" * 32), ("Image-Width", "65"),
                            ("Causal-State", "satisfied"), ("Image-Reused", "yes")):
            old = self.headers[name]
            self.headers[name] = value
            with self.subTest(name=name), self.assertRaises(BackendError):
                self.client.observe_world(self.body())
            self.headers[name] = old
        with self.assertRaises(BackendError):
            self.client.observe_world(self.body(output={"width": 128, "height": 128}))
        self.headers["Causal-State"] = "satisfied"
        self.assertEqual(self.client.observe_world(self.body(after_request_id="my-action")).causal_state,
                         "satisfied")

    def test_native_panel_uses_its_distinct_post_and_never_downloads_again(self):
        self.headers["Capture-Kind"] = "native_info"
        request = {"observation_generation": GENERATION,
                   "observation_request_id": self.body()["observation_request_id"],
                   "panel": "company_finances"}
        self.assertEqual(self.client.observe_native_info(request).kind, "native_info")
        self.assertEqual([r[1] for r in self.requests], ["/v1/view/observe/native-info"])

    def test_telemetry_failure_does_not_change_a_known_success(self):
        def broken(record):
            raise OSError("measurement unavailable")
        self.client.on_http_attempt = broken
        self.assertEqual(self.client.observe_world(self.body()).data, self.data)


if __name__ == "__main__":
    unittest.main()
