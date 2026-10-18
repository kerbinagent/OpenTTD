"""The metadata companion cannot dispatch game, image, or event requests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import __main__
try:
    from backend.profiles import create_app
except ImportError:
    create_app = None
from backend.service import Service
from backend.store import Store
from backend.tests.test_asgi import call


@unittest.skipIf(create_app is None, "ASGI dependencies not installed")
class ProfilesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "state.db")
        self.store = Store(self.path)
        self.token = self.store.provision("world", "actor", 0)
        self.service = Service(self.store, None, autostart=False, world_id="world")
        self.app = create_app(self.service)

    async def asyncTearDown(self):
        self.service.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_only_metadata_routes_and_canonical_auth(self):
        report = {"model_name": "example/model", "effort_level": "high"}
        self.assertEqual((await call(self.app, token=None))[0], 401)
        status, body, _ = await call(self.app, "/v1/me/model", method="PUT", token=self.token,
                                     body=json.dumps(report).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model_report"], {**report, "self_reported": True})
        for path in ("/v1/me", "/v1/participants"):
            self.assertEqual((await call(self.app, path, token=self.token))[0], 200)
        self.assertEqual((await call(self.app, "/v1/me/model", method="DELETE", token=self.token))[0], 200)
        self.assertIsNone(self.service._thread)

    async def test_filter_precedes_all_dispatch_including_static_events_and_images(self):
        with patch.object(self.service, "handle", side_effect=AssertionError("service called")), \
                patch.object(self.service, "events", side_effect=AssertionError("events called")), \
                patch.object(self.service, "image", side_effect=AssertionError("image called")):
            for method, path, raw_path in (
                ("GET", "/", None), ("GET", "/viewer.js", None),
                ("GET", "/v1/events", None), ("GET", "/v1/images/abc", None),
                ("POST", "/v1/commands", None), ("GET", "/v1/me/model", None),
                ("HEAD", "/v1/me", None), ("POST", "/v1/me", None),
                ("GET", "/v1/me", b"/v1/%6de"),
            ):
                with self.subTest(method=method, path=path, raw_path=raw_path):
                    self.assertEqual((await call(self.app, path, method=method, token=self.token,
                                                 raw_path=raw_path))[0], 404)

    async def test_cli_opens_no_engine_or_broker(self):
        # Use the same existing database and canonical auth, just as the live companion does.
        with patch("backend.bridge.UnixEngine", side_effect=AssertionError("engine opened")), \
                patch.object(Service, "start", side_effect=AssertionError("broker started")), \
                patch("uvicorn.run") as run:
            self.assertEqual(__main__.main(["profiles", "--database", self.path,
                                           "--world", "world", "--port", "18186"]), 0)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 18186)

    async def test_existing_table_contents_unchanged_by_companion_startup(self):
        def snapshot(connection):
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {name: sorted(repr(tuple(row)) for row in connection.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '"')) for name in names}

        # An existing full backend has already initialized its social/broker tables.
        self.store.connection.execute("DROP TABLE run_model_reports")
        before = snapshot(self.store.connection)
        companion_store = Store(self.path)
        companion = Service(companion_store, None, autostart=False, world_id="world")
        try:
            after = snapshot(companion_store.connection)
            self.assertEqual(set(after) - set(before), {"run_model_reports"})
            self.assertEqual({name: after[name] for name in before}, before)
        finally:
            companion.close()
            companion_store.close()


if __name__ == "__main__":
    unittest.main()
