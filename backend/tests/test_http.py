"""Transport checks against a fake service; no running game is needed."""

from __future__ import annotations

import json
import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import unquote
from urllib.request import Request, urlopen

from backend import http
from backend.__main__ import _renderer_config, _renderer_configuration, main
from backend.client import BackendClient, BackendError, MAX_IMAGE_BYTES, credential_from_env_or_file
from backend.errors import ServiceError
from backend.observations import ObservationImage
from backend.store import Store


def observation_image(**changes):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    data = (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * (3 * 64)) * 64)) + chunk(b"IEND", b""))
    return replace(ObservationImage(data, "a" * 32, "b" * 32, "c" * 32, "world",
                                    64, 64, "none", False, 0), **changes)


class StubService:
    def __init__(self):
        self.calls = []
        self.event_calls = []
        self.wait_calls = []
        self.image_calls = []

    def handle(self, method, path, token, body=None, query=None):
        if token != "secret":
            raise ServiceError("unauthorized", "Invalid credential", 401)
        self.calls.append((method, path, body, query))
        if path == "/v1/fail":
            raise RuntimeError("SECRET INTERNAL FAILURE")
        return 200, {"method": method, "path": path, "body": body, "query": query}

    def events(self, token, after=None, limit=100):
        if token != "secret":
            raise ServiceError("unauthorized", "Invalid credential", 401)
        self.event_calls.append(after)
        if after is None:
            return {"events": [{"id": "cursor-1", "type": "message", "data": {"body": "hello"}}], "cursor": "cursor-1"}
        return {"events": [], "cursor": after}

    def wait_for_events(self, token, after, timeout):
        self.wait_calls.append((after, timeout))
        time.sleep(min(timeout, 0.02))
        return self.events(token, after=after)

    def image(self, token, image_id):
        if token != "secret":
            raise ServiceError("not_found", "private artifact path and tick 100", 404)
        self.image_calls.append(image_id)
        return b"\x89PNG\r\n\x1a\n" + b"example"


class HTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = StubService()
        cls.server = http.create_server(cls.service, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def fetch(self, path, *, method="GET", body=None, headers=None):
        req = Request(self.origin + path, data=body, method=method, headers=headers or {})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, response.read().decode()
        except HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_auth_and_internal_error_redaction(self):
        status, body = self.fetch("/v1/me")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")
        status, body = self.fetch("/v1/fail", headers={"Authorization": "Bearer secret"})
        self.assertEqual(status, 500)
        self.assertNotIn("SECRET INTERNAL FAILURE", body)

    def test_json_validation_and_size(self):
        headers = {"Authorization": "Bearer secret", "Content-Type": "application/json"}
        for body in (b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b"{"):
            with self.subTest(body=body):
                status, _ = self.fetch("/v1/test", method="POST", body=body, headers=headers)
                self.assertEqual(status, 400)
        status, _ = self.fetch("/v1/test", method="POST", body=b"{" + b" " * 65536, headers=headers)
        self.assertEqual(status, 413)
        status, _ = self.fetch("/v1/test", method="POST", body=b"{}", headers={"Authorization": "Bearer secret"})
        self.assertEqual(status, 415)

    def test_query_and_json_events(self):
        headers = {"Authorization": "Bearer secret"}
        status, body = self.fetch("/v1/events?after=a", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["query"], {"after": "a"})
        status, _ = self.fetch("/v1/me?x=1&x=2", headers=headers)
        self.assertEqual(status, 400)

    def test_sse_resume_and_reference_client(self):
        previous = http.STREAM_LIFETIME
        http.STREAM_LIFETIME = 0.05
        try:
            client = BackendClient(self.origin, "secret", timeout=2)
            self.assertEqual(client.request("GET", "/v1/me")["path"], "/v1/me")
            events = list(client.iter_events(reconnect=False))
            self.assertEqual(events, [{"id": "cursor-1", "type": "message", "data": {"body": "hello"}}])
            self.assertEqual(list(client.iter_events(after="cursor-1", reconnect=False)), [])
            self.assertIn("cursor-1", self.service.event_calls)
            self.assertTrue(self.service.wait_calls)
            self.assertTrue(all(0 <= timeout <= http.HEARTBEAT_INTERVAL for _, timeout in self.service.wait_calls))
            with self.assertRaises(BackendError) as exc:
                BackendClient(self.origin, "bad").request("GET", "/v1/me")
            self.assertEqual(exc.exception.status, 401)
        finally:
            http.STREAM_LIFETIME = previous

    def test_capacity_above_fifty_connections(self):
        self.assertGreaterEqual(http.MAX_STREAMS, 50)
        self.assertGreaterEqual(http.MAX_HANDLERS, 50)
        acquired = [self.server.stream_slots.acquire(blocking=False) for _ in range(50)]
        try:
            self.assertTrue(all(acquired))
        finally:
            for held in acquired:
                if held:
                    self.server.stream_slots.release()

    def test_exact_public_static_files(self):
        previous = http._WEB_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("index.html", "viewer.js", "viewer.css", "events.mjs", "world-status.mjs"):
                (root / name).write_text(name, encoding="utf-8")
            http._WEB_ROOT = root
            try:
                for path, name in (("/", "index.html"), ("/viewer.js", "viewer.js"),
                                   ("/viewer.css", "viewer.css"), ("/events.mjs", "events.mjs"),
                                   ("/world-status.mjs", "world-status.mjs")):
                    with self.subTest(path=path):
                        status, body = self.fetch(path)
                        self.assertEqual((status, body), (200, name))
                status, _ = self.fetch("/other.js")
                self.assertEqual(status, 401)
                if hasattr(os, "O_NOFOLLOW"):
                    (root / "viewer.js").unlink()
                    (root / "viewer.js").symlink_to(root / "index.html")
                    status, _ = self.fetch("/viewer.js")
                    self.assertEqual(status, 404)
            finally:
                http._WEB_ROOT = previous

    def test_authenticated_binary_image(self):
        req = Request(self.origin + "/v1/images/image_1", headers={"Authorization": "Bearer secret"})
        with urlopen(req, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("image_1", self.service.image_calls)
        status, body = self.fetch("/v1/images/image_1", headers={"Authorization": "Bearer bad"})
        self.assertEqual(status, 404)
        self.assertNotIn("private artifact path", body)
        status, _ = self.fetch("/v1/images/image_1")
        self.assertEqual(status, 401)
        before = len(self.service.image_calls)
        self.fetch("/v1/images/image_1/extra", headers={"Authorization": "Bearer secret"})
        self.assertEqual(len(self.service.image_calls), before)

    def test_selected_observation_is_finite_png_with_only_public_metadata(self):
        for kind, path in (("world", "/v1/view/observe/world"),
                           ("native_info", "/v1/view/observe/native-info")):
            with self.subTest(kind=kind):
                image = observation_image(kind=kind, causal_state="satisfied", reused=True, age_ms=12)
                object.__setattr__(image, "source_frame", 123)
                object.__setattr__(image, "private_path", "/private/artifact.png")
                request = Request(self.origin + path, data=b"{}", method="POST", headers={
                    "Authorization": "Bearer secret", "Content-Type": "application/json", "Range": "bytes=0-2",
                })
                with mock.patch.object(self.service, "handle", return_value=(200, image)):
                    with urlopen(request, timeout=3) as response:
                        self.assertEqual((response.status, response.read()), (200, image.data))
                        self.assertEqual(response.headers["Content-Type"], "image/png")
                        self.assertEqual(response.headers["Content-Length"], str(len(image.data)))
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                        self.assertEqual({key: value for key, value in response.headers.items()
                                          if key.startswith("X-OpenTTD-")}, {
                            "X-OpenTTD-Observation-Id": "a" * 32,
                            "X-OpenTTD-Image-Id": "b" * 32,
                            "X-OpenTTD-Observation-Generation": "c" * 32,
                            "X-OpenTTD-Capture-Kind": kind,
                            "X-OpenTTD-Image-Width": "64", "X-OpenTTD-Image-Height": "64",
                            "X-OpenTTD-Causal-State": "satisfied", "X-OpenTTD-Image-Reused": "true",
                            "X-OpenTTD-Image-Age-Ms": "12", "X-OpenTTD-Image-Purpose": "observation",
                        })
                        self.assertIsNone(response.headers["ETag"])
                        self.assertIsNone(response.headers["Accept-Ranges"])

    def test_selected_observation_pending_stays_finite_json(self):
        pending = {"status": "pending", "observation_request_id": "obs1:1234567890000:" + "a" * 32}
        with mock.patch.object(self.service, "handle", return_value=(202, pending)):
            status, body = self.fetch("/v1/view/observe/world", method="POST", body=b"{}", headers={
                "Authorization": "Bearer secret", "Content-Type": "application/json",
            })
        self.assertEqual((status, json.loads(body)), (202, pending))

    def test_invalid_selected_observation_never_commits_png_headers(self):
        valid = observation_image()
        for changes in (
            {"observation_id": "a" * 32 + "\r\nPrivate: path"}, {"image_id": "bad"},
            {"generation": "A" * 32}, {"kind": "native_info"}, {"width": True},
            {"width": 63}, {"height": 65}, {"reused": 1}, {"causal_state": "private"},
            {"age_ms": -1}, {"age_ms": 65001}, {"data": valid.data[:-1]},
        ):
            with self.subTest(changes=list(changes)):
                request = Request(self.origin + "/v1/view/observe/world", data=b"{}", method="POST", headers={
                    "Authorization": "Bearer secret", "Content-Type": "application/json",
                })
                with mock.patch.object(self.service, "handle", return_value=(200, replace(valid, **changes))):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=3)
                    with error.exception as response:
                        self.assertEqual(response.code, 500)
                        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
                        self.assertEqual(json.loads(response.read())["error"]["code"], "image_unavailable")
                        self.assertFalse(any(key.startswith("X-OpenTTD-") for key in response.headers))

    def test_eight_capture_slots_preserve_normal_requests(self):
        paths = ("/v1/view/images", "/v1/view/observe/world", "/v1/view/observe/native-info")
        release = threading.Event()
        started = 0
        lock = threading.Lock()
        original = self.service.handle

        def blocking_capture(method, path, token, body=None, query=None):
            nonlocal started
            if method == "POST" and path in paths:
                with lock:
                    started += 1
                if not release.wait(5):
                    raise RuntimeError("capture gate timed out")
            return original(method, path, token, body, query)

        headers = {"Authorization": "Bearer secret", "Content-Type": "application/json"}
        with mock.patch.object(self.service, "handle", side_effect=blocking_capture), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.fetch, paths[index % 3], method="POST", body=b"{}", headers=headers)
                       for index in range(8)]
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with lock:
                        count = started
                    if count == 8:
                        break
                    time.sleep(0.01)
                self.assertEqual(count, 8)
                for path in paths:
                    status, body = self.fetch(path, method="POST", body=b"{}", headers=headers)
                    self.assertEqual((status, json.loads(body)["error"]["code"]), (503, "captures_busy"))
                for method, path, body in (("POST", "/v1/actions", b"{}"),
                                           ("POST", "/v1/threads/t/messages", b"{}"), ("GET", "/v1/me", None)):
                    status, _ = self.fetch(path, method=method, body=body, headers=headers)
                    self.assertEqual(status, 200)
                request = Request(self.origin + "/v1/images/image_1", headers={"Authorization": "Bearer secret"})
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
                previous = http.STREAM_LIFETIME
                http.STREAM_LIFETIME = 0.05
                try:
                    status, _ = self.fetch("/v1/events", headers={
                        "Authorization": "Bearer secret", "Accept": "text/event-stream",
                    })
                    self.assertEqual(status, 200)
                finally:
                    http.STREAM_LIFETIME = previous
                self.assertEqual(started, 8)
            finally:
                release.set()
            self.assertTrue(all(future.result()[0] == 200 for future in futures))
        status, _ = self.fetch(paths[0], method="POST", body=b"{}", headers=headers)
        self.assertEqual(status, 200)

    def test_encoded_and_slash_capture_routes_share_the_generation_budget(self):
        release = threading.Event()
        started = 0
        lock = threading.Lock()
        original = self.service.handle

        def domain_routing_capture(method, path, token, body=None, query=None):
            nonlocal started
            parts = [unquote(part) for part in path.strip("/").split("/")]
            if method == "POST" and parts == ["v1", "view", "images"]:
                with lock:
                    started += 1
                if not release.wait(5):
                    raise RuntimeError("capture gate timed out")
            return original(method, path, token, body, query)

        headers = {"Authorization": "Bearer secret", "Content-Type": "application/json"}
        with mock.patch.object(self.service, "handle", side_effect=domain_routing_capture), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.fetch, "/v1/view/%69mages", method="POST", body=b"{}", headers=headers)
                       for _ in range(8)]
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with lock:
                        count = started
                    if count == 8:
                        break
                    time.sleep(0.01)
                self.assertEqual(count, 8)
                for path in ("/v1/view/images", "/v1/view/%69mages", "/v1/view/images/",
                             "//v1/view/images//", "/%761/view/images",
                             "/v1/view/%6fbserve/world", "/v1/view/observe/world/",
                             "//v1/view/observe/world//", "/v1/view/%6fbserve/native-info",
                             "/v1/view/observe/native-info/", "//v1/view/observe/native-info//"):
                    with self.subTest(path=path):
                        status, body = self.fetch(path, method="POST", body=b"{}", headers=headers)
                        self.assertEqual((status, json.loads(body)["error"]["code"]), (503, "captures_busy"))
                status, _ = self.fetch("/v1/actions", method="POST", body=b"{}", headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(started, 8)
            finally:
                release.set()
            self.assertTrue(all(future.result()[0] == 200 for future in futures))
        status, _ = self.fetch("/v1/view/%69mages", method="POST", body=b"{}", headers=headers)
        self.assertEqual(status, 200)

    def test_selected_observation_alternate_spellings_return_png(self):
        for kind, path in (("world", "/v1/view/%6fbserve/world/"),
                           ("native_info", "//v1/view/observe/native-info//")):
            with self.subTest(path=path):
                image = observation_image(kind=kind)
                request = Request(self.origin + path, data=b"{}", method="POST", headers={
                    "Authorization": "Bearer secret", "Content-Type": "application/json",
                })
                with mock.patch.object(self.service, "handle", return_value=(200, image)):
                    with urlopen(request, timeout=3) as response:
                        self.assertEqual((response.status, response.read()), (200, image.data))
                        self.assertEqual(response.headers["X-OpenTTD-Capture-Kind"], kind)

    def test_credential_file_owner_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "token")
            with open(path, "w", encoding="utf-8") as file:
                file.write("secret\n")
            os.chmod(path, 0o600)
            self.assertEqual(credential_from_env_or_file(path), "secret")
            os.chmod(path, 0o644)
            with self.assertRaises(ValueError):
                credential_from_env_or_file(path)
            if hasattr(os, "O_NOFOLLOW"):
                link = os.path.join(directory, "link")
                os.symlink(path, link)
                with self.assertRaises(OSError):
                    credential_from_env_or_file(link)


class RedirectTest(unittest.TestCase):
    def test_client_never_forwards_bearer_to_redirect_host(self):
        with self.assertRaises(ValueError):
            BackendClient("file:///tmp/token", "secret")
        seen = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_address[1]}/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            client = BackendClient(f"http://127.0.0.1:{source.server_address[1]}", "secret")
            with self.assertRaises(BackendError) as request_error:
                client.request("GET", "/v1/me")
            self.assertEqual(request_error.exception.status, 302)
            with self.assertRaises(BackendError) as event_error:
                list(client.iter_events(reconnect=False))
            self.assertEqual(event_error.exception.status, 302)
            with self.assertRaises(BackendError) as image_error:
                client.image("d" * 32)
            self.assertEqual(image_error.exception.status, 302)
            self.assertEqual(seen, [])
        finally:
            source.shutdown()
            sink.shutdown()
            source.server_close()
            sink.server_close()
            source_thread.join(timeout=2)
            sink_thread.join(timeout=2)


class ReferenceImageClientTest(unittest.TestCase):
    def test_authenticated_png_mime_size_and_id_validation(self):
        seen = []

        class Images(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Accept")))
                if self.headers.get("Authorization") != "Bearer secret":
                    data = b'{"error":{"code":"unauthorized","message":"Invalid credential"}}'
                    status, content_type = 401, "application/json"
                elif self.path.endswith("/" + "b" * 32):
                    data = b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE_BYTES + 1 - 8)
                    status, content_type = 200, "image/png"
                elif self.path.endswith("/" + "c" * 32):
                    data = b"\x89PNG\r\n\x1a\nsmall"
                    status, content_type = 200, "application/octet-stream"
                elif self.path.endswith("/" + "d" * 32):
                    data = b"not a PNG"
                    status, content_type = 200, "image/png"
                else:
                    data = b"\x89PNG\r\n\x1a\nsmall"
                    status, content_type = 200, "image/png"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Images)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            client = BackendClient(origin, "secret")
            before = len(seen)
            for invalid in ("../etc/passwd", "a" * 31, "g" * 32, None):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    client.image(invalid)
            self.assertEqual(len(seen), before)
            self.assertEqual(client.image("a" * 32), b"\x89PNG\r\n\x1a\nsmall")
            self.assertEqual(seen[-1], ("/v1/images/" + "a" * 32, "Bearer secret", "image/png"))
            with self.assertRaises(BackendError) as unauthorized:
                BackendClient(origin, "wrong").image("a" * 32)
            self.assertEqual((unauthorized.exception.status, unauthorized.exception.code), (401, "unauthorized"))
            with self.assertRaises(BackendError) as oversized:
                client.image("b" * 32)
            self.assertEqual(oversized.exception.code, "image_too_large")
            with self.assertRaises(BackendError) as wrong_type:
                client.image("c" * 32)
            self.assertEqual(wrong_type.exception.code, "invalid_image")
            with self.assertRaises(BackendError) as wrong_signature:
                client.image("d" * 32)
            self.assertEqual(wrong_signature.exception.code, "invalid_image")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class CLITest(unittest.TestCase):
    def test_operator_budget_arguments_reject_invalid_values_before_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "state.db")
            base = ["serve", "--database", database, "--world", "world",
                    "--engine-socket", os.path.join(directory, "engine.sock")]
            invalid = [("--max-pending-company", value) for value in
                       ("0", "01", "129", "+1", "1.0", "-1", "nan", "١")]
            invalid += [("--max-pending-world", value) for value in
                        ("0", "1025", "+1", "1e2")]
            invalid += [("--capture-interval", value) for value in
                        ("0.9", "61", "nan", "inf", "-inf", "oops", "2")]
            for option, value in invalid:
                with self.subTest(option=option, value=value):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as exit_status:
                            main(base + [option, value])
                    self.assertEqual(exit_status.exception.code, 2)
                    self.assertFalse(os.path.exists(database))

    def test_operator_limits_reach_store_and_both_renderer_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "state.db")
            real_store = Store(database)
            try:
                token = real_store.provision("world", "viewer", 0, mode="visual")
                run_id = real_store.authenticate(token).run_id
            finally:
                real_store.close()
            artifacts = os.path.join(directory, "artifacts")
            os.mkdir(artifacts, 0o700)
            socket_path = os.path.join(directory, "renderer.sock")
            config_path = os.path.join(directory, "renderers.json")
            with open(config_path, "w", encoding="utf-8") as file:
                json.dump({"version": 1, "world_id": "world", "runs": {
                    run_id: {"socket": socket_path, "artifacts": artifacts},
                }}, file)
            os.chmod(config_path, 0o600)
            base = ["serve", "--transport", "http", "--database", database,
                    "--world", "world", "--engine-socket", os.path.join(directory, "engine.sock"),
                    "--max-pending-company", "3", "--max-pending-world", "2",
                    "--capture-interval", "2.5"]
            actual_store = Store
            store_arguments = []

            def make_store(path, **kwargs):
                store_arguments.append(kwargs)
                return actual_store(path, **kwargs)

            for renderer_arguments in (
                ["--renderer-run", run_id, "--renderer-socket", socket_path,
                 "--renderer-artifacts", artifacts],
                ["--renderer-config", config_path],
            ):
                with self.subTest(renderer_arguments=renderer_arguments):
                    with (mock.patch("backend.store.Store", side_effect=make_store),
                          mock.patch("backend.renderer.RendererGateway") as gateway,
                          mock.patch("backend.service.Service"),
                          mock.patch("backend.http.create_server"),
                          contextlib.redirect_stderr(io.StringIO())):
                        self.assertEqual(main(base + renderer_arguments), 0)
                    self.assertEqual(store_arguments[-1], {
                        "max_pending_company": 3, "max_pending_world": 2,
                    })
                    self.assertEqual(gateway.call_args.kwargs["capture_interval"], 2.5)
                    self.assertEqual(set(gateway.call_args.args[0]), {run_id})

    def test_renderer_options_are_all_required(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "state.db")
            base = [sys.executable, "-m", "backend", "serve", "--database", database,
                    "--world", "world", "--engine-socket", os.path.join(directory, "engine.sock")]
            result = subprocess.run(base + ["--renderer-run", "run"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be supplied together", result.stderr)
            self.assertFalse(os.path.exists(database))

    def test_operator_slot_config_needs_no_company_or_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(os.path.join(directory, "state.db"))
            self.addCleanup(store.close)
            artifacts = os.path.join(directory, "images")
            os.mkdir(artifacts, 0o700)
            path = os.path.join(directory, "renderers.json")
            config = {"version": 2, "world_id": "world", "slots": {
                "capture-0": {"socket": os.path.join(directory, "r0.sock"), "artifacts": artifacts},
            }}

            def write(value):
                with open(path, "w", encoding="utf-8") as file:
                    json.dump(value, file)
                os.chmod(path, 0o600)

            write(config)
            self.assertEqual(_renderer_configuration(path, store, "world"), ({}, config["slots"]))
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            with self.assertRaises(ValueError):
                _renderer_config(path, store, "world")
            for invalid in (
                {**config, "extra": {}}, {**config, "world_id": "wrong"},
                {**config, "slots": {"/tmp/public-path": next(iter(config["slots"].values()))}},
                {**config, "slots": {**config["slots"], "capture-1": next(iter(config["slots"].values()))}},
            ):
                write(invalid)
                with self.subTest(config=invalid), self.assertRaises(ValueError):
                    _renderer_configuration(path, store, "world")

            token = store.provision("world", "legacy", 0, mode="visual")
            run = store.authenticate(token).run_id
            legacy_images = os.path.join(directory, "legacy-images")
            os.mkdir(legacy_images, 0o700)
            legacy = {run: {"socket": os.path.join(directory, "legacy.sock"), "artifacts": legacy_images}}
            combined = {**config, "runs": legacy}
            write(combined)
            self.assertEqual(_renderer_configuration(path, store, "world"), (legacy, config["slots"]))
            duplicated = {**combined, "runs": {run: next(iter(config["slots"].values()))}}
            write(duplicated)
            with self.assertRaises(ValueError):
                _renderer_configuration(path, store, "world")
            write({**config, "slots": {f"capture-{i}": next(iter(config["slots"].values())) for i in range(16)}})
            with self.assertRaises(ValueError):
                _renderer_configuration(path, store, "world")

    def test_dynamic_operator_wiring_accepts_a_world_without_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "state.db")
            store = Store(database)
            store.connection.execute("INSERT INTO worlds(id,epoch) VALUES('world','epoch')")
            store.close()
            path = os.path.join(directory, "renderers.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"version": 2, "world_id": "world", "slots": {}}, file)
            os.chmod(path, 0o600)
            with (mock.patch("backend.renderer.RendererGateway") as gateway,
                  mock.patch("backend.admission.DynamicAgentAdmission", create=True) as admission,
                  mock.patch("backend.service.Service") as service,
                  mock.patch("backend.http.create_server"),
                  contextlib.redirect_stderr(io.StringIO())):
                self.assertEqual(main(["serve", "--transport", "http", "--database", database,
                    "--world", "world", "--engine-socket", os.path.join(directory, "engine.sock"),
                    "--renderer-config", path, "--dynamic-agents"]), 0)
            self.assertEqual(gateway.call_args.args[0], {})
            self.assertEqual(gateway.call_args.kwargs["slots"], {})
            self.assertEqual(admission.call_args.args[1:], ("world", gateway.return_value))
            self.assertIsNone(admission.call_args.kwargs["legacy"])
            self.assertIs(service.call_args.kwargs["admission"], admission.return_value)

    def test_four_renderer_operator_map_is_scoped_and_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(os.path.join(directory, "state.db"))
            try:
                runs = {}
                for number in range(4):
                    token = store.provision("world", f"viewer-{number}", 0, mode="visual")
                    run_id = store.authenticate(token).run_id
                    artifacts = os.path.join(directory, f"artifacts-{number}")
                    os.mkdir(artifacts, 0o700)
                    runs[run_id] = {"socket": os.path.join(directory, f"renderer-{number}.sock"),
                                    "artifacts": artifacts}
                path = os.path.join(directory, "renderers.json")

                def write_config(data):
                    with open(path, "w", encoding="utf-8") as file:
                        json.dump(data, file)
                    os.chmod(path, 0o600)

                config = {"version": 1, "world_id": "world", "runs": runs}
                write_config(config)
                self.assertEqual(len(_renderer_config(path, store, "world")), 4)
                wrong = dict(config, extra=True)
                write_config(wrong)
                with self.assertRaises(ValueError):
                    _renderer_config(path, store, "world")
                duplicate = json.loads(json.dumps(config))
                values = list(duplicate["runs"].values())
                values[1]["socket"] = values[0]["socket"]
                write_config(duplicate)
                with self.assertRaises(ValueError):
                    _renderer_config(path, store, "world")
                write_config(config)
                os.chmod(path, 0o644)
                with self.assertRaises(ValueError):
                    _renderer_config(path, store, "world")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
