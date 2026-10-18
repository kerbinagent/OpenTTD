"""Public starter kit bounds, reproducibility and unauthenticated transport."""

import asyncio
import hashlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from backend import agent_assets, http
from backend.errors import ServiceError

try:
    from backend import asgi
except ImportError:
    asgi = None


def source_fixture(root):
    for name in agent_assets.SOURCE_PATHS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + "\n").encode())
    (root / "private-credential.txt").write_text("MUST_NOT_BE_PUBLISHED")


class AssetBuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        source_fixture(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_exact_immutable_deterministic_kit_and_manifest(self):
        first = agent_assets.build_agent_assets(self.root)
        second = agent_assets.build_agent_assets(self.root)
        self.assertEqual(first, second)
        self.assertEqual(set(first), agent_assets.PUBLIC_PATHS)
        with self.assertRaises(TypeError):
            first["/secret"] = (b"secret", "text/plain")
        archive, content_type = first["/agent/client.zip"]
        self.assertEqual(content_type, "application/zip")
        self.assertNotIn(b"MUST_NOT_BE_PUBLISHED", archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as kit:
            self.assertEqual(kit.namelist(), sorted([*agent_assets.SOURCE_PATHS, "manifest.json"]))
            manifest = json.loads(kit.read("manifest.json"))
            self.assertEqual(manifest["format_version"], 1)
            self.assertEqual(set(manifest["files"]), set(agent_assets.SOURCE_PATHS))
            for name in agent_assets.SOURCE_PATHS:
                data = kit.read(name)
                self.assertEqual(data, (self.root / name).read_bytes())
                self.assertEqual(manifest["files"][name], hashlib.sha256(data).hexdigest())
                self.assertEqual(kit.getinfo(name).date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(kit.read("docs/llms.txt"), first["/llms.txt"][0])
            self.assertEqual(kit.read("docs/llms-capture.txt"), first["/llms-capture.txt"][0])
            self.assertEqual(kit.read("backend/schemas/public-api-v1.json"), first["/agent/public-api-v1.json"][0])
            self.assertEqual(kit.read("backend/schemas/engine-catalog-v1.json"), first["/agent/engine-catalog-v1.json"][0])

    def test_missing_source_fails_instead_of_partial_kit(self):
        (self.root / "backend/agent_client.py").unlink()
        with self.assertRaises(OSError):
            agent_assets.build_agent_assets(self.root)

    def test_symlink_source_and_parent_are_rejected(self):
        source = self.root / "backend/agent_client.py"
        source.unlink()
        source.symlink_to(self.root / "private-credential.txt")
        with self.assertRaises(OSError):
            agent_assets.build_agent_assets(self.root)
        source.unlink()
        source.write_text("client")
        (self.root / "docs").rename(self.root / "real-docs")
        (self.root / "docs").symlink_to(self.root / "real-docs", target_is_directory=True)
        with self.assertRaises(OSError):
            agent_assets.build_agent_assets(self.root)

    def test_nonregular_and_oversized_sources_are_rejected(self):
        source = self.root / "backend/agent_client.py"
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(ValueError):
            agent_assets.build_agent_assets(self.root)
        source.unlink()
        source.write_bytes(b"x" * (agent_assets.MAX_SOURCE + 1))
        with self.assertRaises(ValueError):
            agent_assets.build_agent_assets(self.root)

    def test_aggregate_and_archive_bounds(self):
        size = sum((self.root / name).stat().st_size for name in agent_assets.SOURCE_PATHS)
        for limit in (size - 1, size + 1):
            with self.subTest(limit=limit), mock.patch.object(agent_assets, "MAX_KIT", limit):
                with self.assertRaises(ValueError):
                    agent_assets.build_agent_assets(self.root)

    def test_server_startup_fails_if_kit_cannot_be_built(self):
        with mock.patch("backend.http.build_agent_assets", side_effect=ValueError("missing source")):
            with self.assertRaises(ValueError):
                http.create_server(object(), port=0)
        if asgi is not None:
            with mock.patch("backend.asgi.build_agent_assets", side_effect=ValueError("missing source")):
                with self.assertRaises(ValueError):
                    asgi.create_app(object())


class PrivateService:
    def handle(self, *args, **kwargs):
        raise ServiceError("unauthorized", status=401)


class PublicAgentHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        source_fixture(root)
        cls.assets = agent_assets.build_agent_assets(root)
        with mock.patch("backend.http.build_agent_assets", return_value=cls.assets) as builder:
            cls.server = http.create_server(PrivateService(), port=0)
            builder.assert_called_once_with()
        # Changing files after startup cannot change this release's public bytes.
        (root / "docs/llms.txt").write_text("later source version")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.directory.cleanup()

    def fetch(self, path, method="GET", body=None, headers=None):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_literal_assets_get_head_and_security_headers_without_token(self):
        for path, (data, content_type) in self.assets.items():
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    status, body, headers = self.fetch(path, method)
                    self.assertEqual(status, 200)
                    self.assertEqual(body, data if method == "GET" else b"")
                    self.assertEqual(headers["Content-Length"], str(len(data)))
                    self.assertEqual(headers["Content-Type"], content_type)
                    self.assertEqual(headers["Cache-Control"], "no-cache")
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_asset_query_method_body_and_alias_rejection(self):
        for query in ("token=secret", "x=1", "x=1&x=2", "%74oken=secret"):
            with self.subTest(query=query):
                self.assertEqual(self.fetch("/llms.txt?" + query)[0], 400)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                self.assertEqual(self.fetch("/llms.txt", method)[0], 405)
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual(self.fetch("/llms.txt", method, b"{}")[0], 400)
        for path in ("/llms%2Etxt", "/llms.txt/", "/agent/client.zip/", "/backend/client.py", "/agent/../backend/client.py"):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 401)

    def test_raw_malformed_targets_cannot_normalize_to_public_assets(self):
        for target, expected in ((b"\x00/llms.txt", 400), (b"\x01/llms.txt", 400),
                                 (b"\x1f/llms.txt", 400), (b"/llms.\ttxt", 400),
                                 (b"/llms.txt\x7f", 400), (b"//llms.txt", 401),
                                 (b"///llms.txt", 401), (b"http://localhost/llms.txt", 400),
                                 (b"https://localhost/agent/client.zip", 400)):
            with self.subTest(target=target):
                with socket.create_connection(self.server.server_address, timeout=3) as connection:
                    connection.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    with connection.makefile("rb") as response:
                        status = int(response.readline().split()[1])
                        data = response.read()
                self.assertEqual(status, expected)
                self.assertNotIn(self.assets["/llms.txt"][0], data)


async def asgi_call(app, path, method="GET", query=b"", body=b"", headers=()):
    messages = []
    request_sent = False
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
             "headers": list(headers), "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    data = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return start["status"], data, dict(start["headers"])


@unittest.skipIf(asgi is None, "ASGI dependencies not installed")
class PublicAgentASGITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        source_fixture(root)
        self.assets = agent_assets.build_agent_assets(root)
        with mock.patch("backend.asgi.build_agent_assets", return_value=self.assets) as builder:
            self.app = asgi.create_app(PrivateService())
            builder.assert_called_once_with()

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_literal_get_head_match_http_bytes_and_headers(self):
        for path, (data, content_type) in self.assets.items():
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    status, body, headers = await asgi_call(self.app, path, method)
                    self.assertEqual(status, 200)
                    self.assertEqual(body, data if method == "GET" else b"")
                    self.assertEqual(headers[b"content-length"], str(len(data)).encode())
                    self.assertEqual(headers[b"content-type"], content_type.encode())
                    self.assertEqual(headers[b"cache-control"], b"no-cache")
                    self.assertEqual(headers[b"x-content-type-options"], b"nosniff")
                    self.assertEqual(headers[b"referrer-policy"], b"no-referrer")

    async def test_query_method_body_and_alias_rejection_matches_http(self):
        for query in (b"token=secret", b"x=1", b"x=1&x=2", b"%74oken=secret"):
            with self.subTest(query=query):
                self.assertEqual((await asgi_call(self.app, "/llms.txt", query=query))[0], 400)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                self.assertEqual((await asgi_call(self.app, "/llms.txt", method))[0], 405)
        for method in ("GET", "HEAD"):
            for headers in ((), ((b"content-length", b"2"),)):
                with self.subTest(method=method, headers=headers):
                    self.assertEqual((await asgi_call(self.app, "/llms.txt", method, body=b"{}", headers=headers))[0], 400)
        for path in ("/llms%2Etxt", "/llms.txt/", "/agent/client.zip/", "/backend/client.py", "/agent/../backend/client.py"):
            with self.subTest(path=path):
                self.assertEqual((await asgi_call(self.app, path))[0], 401)


if __name__ == "__main__":
    unittest.main()
