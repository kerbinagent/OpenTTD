"""Self-reported model labels stay scoped to the authenticated active run."""

from dataclasses import replace
import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http
from backend.errors import ServiceError
from backend.service import Service
from backend.store import Store
from backend.tests.test_service import FakeEngine

try:
    from backend import asgi
    from backend.tests.test_asgi import call as asgi_call
except ImportError:
    asgi = None


REPORT = {"model_name": "example/model-v2", "effort_level": "high"}
PUBLIC_REPORT = {**REPORT, "self_reported": True}


class ModelReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.db")
        self.store = Store(self.path)
        self.engine = FakeEngine()
        self.service = Service(self.store, self.engine, autostart=False)
        self.token = self.store.provision("w1", "alice", 1)
        self.principal = self.store.authenticate(self.token)

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.tmp.cleanup()

    def error(self, code, fn):
        with self.assertRaises(ServiceError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def report(self, token=None, body=REPORT):
        return self.service.handle("PUT", "/v1/me/model", token or self.token, body)

    def test_replace_clear_and_reads_do_not_call_engine_or_change_identity(self):
        self.assertNotIn("model_report", self.service.handle("GET", "/v1/me", self.token)[1])
        for _ in range(2):
            self.assertEqual(self.report(), (200, {"model_report": PUBLIC_REPORT}))
        self.assertEqual(self.service.handle("GET", "/v1/me", self.token)[1]["model_report"], PUBLIC_REPORT)
        changed = {"model_name": "another-provider/model", "effort_level": "custom effort"}
        self.assertEqual(self.report(body=changed)[1]["model_report"], {**changed, "self_reported": True})
        self.assertEqual(self.store.authenticate(self.token), self.principal)
        for _ in range(2):
            self.assertEqual(self.service.handle("DELETE", "/v1/me/model", self.token),
                             (200, {"model_report": None}))
        self.assertNotIn("model_report", self.service.handle("GET", "/v1/me", self.token)[1])
        self.assertEqual(self.engine.calls, [])

    def test_invalid_labels_and_unknown_owner_fields_are_rejected_atomically(self):
        self.report()
        invalid = [None, [], {}, {"model_name": "model"}, {**REPORT, "run_id": "other"},
                   {**REPORT, "company_id": 2}, {**REPORT, "identity_id": "bob"}]
        for name, maximum in (("model_name", 100), ("effort_level", 32)):
            for value in (None, True, 12, [], {}, "", " " , " padded", "padded ", "a" * (maximum + 1),
                          "line\nbreak", "tab\tlabel", "null\x00label", "bidi\u202elabel", "line\u2028label"):
                invalid.append({**REPORT, name: value})
        for body in invalid:
            with self.subTest(body=body):
                self.error("invalid_request", lambda body=body: self.report(body=body))
        for method, body, query in (("PUT", REPORT, {"company": "2"}),
                                    ("DELETE", {"model_name": "other"}, {}),
                                    ("DELETE", {}, {"run": "other"})):
            self.error("invalid_request", lambda: self.service.handle(
                method, "/v1/me/model", self.token, body, query))
        self.assertEqual(self.store.model_reports_for_world("w1"), {self.principal.run_id: PUBLIC_REPORT})
        self.assertEqual(self.engine.calls, [])

    def test_allowed_for_both_agent_modes_but_not_humans_or_revoked_runs(self):
        visual = self.store.provision("w1", "visual", 2, mode="visual")
        self.assertEqual(self.report(visual)[1]["model_report"], PUBLIC_REPORT)
        human = self.store.provision("w1", "human", 3, kind="human")
        self.error("forbidden", lambda: self.report(human))
        self.error("forbidden", lambda: self.service.handle("DELETE", "/v1/me/model", human))
        self.error("unauthorized", lambda: self.store.set_model_report(
            replace(self.principal, company_id=2), REPORT))
        self.store.revoke_membership("w1", "alice", 1)
        self.error("unauthorized", self.report)
        self.error("unauthorized", lambda: self.store.set_model_report(self.principal, REPORT))

    def test_reports_follow_latest_selected_run_without_inheriting_and_keep_other_actors(self):
        self.report()
        bob = self.store.provision("w1", "bob", 1)
        bob_report = {"model_name": "Bob Model", "effort_level": "low"}
        self.report(bob, bob_report)
        latest = self.store.provision("w1", "alice", 1, mode="visual")
        people = self.service.handle("GET", "/v1/participants", self.token)[1]["participants"]
        self.assertEqual({person["identity_id"] for person in people}, {"alice", "bob"})
        by_id = {person["identity_id"]: person for person in people}
        self.assertNotIn("model_report", by_id["alice"])
        self.assertEqual(by_id["bob"]["model_report"], {**bob_report, "self_reported": True})
        newest_report = {"model_name": "Latest Model", "effort_level": "ultra"}
        self.report(latest, newest_report)
        people = self.service.handle("GET", "/v1/participants", self.token)[1]["participants"]
        alice = next(person for person in people if person["identity_id"] == "alice")
        self.assertEqual(alice["model_report"], {**newest_report, "self_reported": True})
        self.assertEqual(self.service.handle("GET", "/v1/me", self.token)[1]["model_report"], PUBLIC_REPORT)

    def test_store_report_projection_does_not_cross_worlds(self):
        # A service binds one world; exercise the underlying store's filter
        # independently before that binding is established.
        elsewhere = self.store.provision("w2", "alice", 1)
        other = self.store.authenticate(elsewhere)
        self.store.set_model_report(self.principal, REPORT)
        self.store.set_model_report(other, {"model_name": "Other Model", "effort_level": "low"})
        self.assertEqual(self.store.model_reports_for_world("w1"), {self.principal.run_id: PUBLIC_REPORT})
        self.assertEqual(set(self.store.model_reports_for_world("w2")), {other.run_id})

    def test_metadata_only_service_omits_native_rows_and_cannot_start_a_broker(self):
        self.service.close()
        self.service = Service(self.store, None, autostart=False, world_id="w1")
        self.store.provision("w1", "api-human", 1, kind="human")
        self.service.social.update_native_roster("w1", [
            {"public_key": "a" * 64, "company_id": 1, "label": "Native Human"},
            {"public_key": "b" * 64, "company_id": 2, "label": "Renderer Worker"},
        ])
        self.assertTrue(any(member.mode == "native" for member in self.store.principals_for_world("w1")))
        self.report()
        people = self.service.handle("GET", "/v1/participants", self.token)[1]["participants"]
        self.assertEqual({person["identity_id"] for person in people}, {"alice", "api-human"})
        alice = next(person for person in people if person["identity_id"] == "alice")
        self.assertEqual(alice["model_report"], PUBLIC_REPORT)
        self.assertEqual(self.service.handle("GET", "/v1/me", self.token)[1]["model_report"], PUBLIC_REPORT)
        self.service.handle("DELETE", "/v1/me/model", self.token)
        with self.assertRaises(ValueError):
            self.service.start()
        self.assertIsNone(self.service._thread)
        with self.assertRaises(ValueError):
            Service(self.store, None, world_id="w1")

    def test_existing_database_upgrade_rotation_and_restart_preserve_run_scope(self):
        # Emulate an existing database that predates this additive table.
        self.store.connection.execute("DROP TABLE run_model_reports")
        self.service.close()
        self.store.close()
        self.store = Store(self.path)
        self.service = Service(self.store, self.engine, autostart=False)
        self.assertEqual(self.store.authenticate(self.token), self.principal)
        self.assertEqual(self.store.model_reports_for_world("w1"), {})
        self.store.set_model_report(self.principal, REPORT)
        rotated = self.store.rotate_credential(self.principal)
        self.service.close()
        self.store.close()
        self.store = Store(self.path)
        self.service = Service(self.store, self.engine, autostart=False)
        self.assertEqual(self.store.authenticate(rotated), self.principal)
        self.assertEqual(self.store.model_reports_for_world("w1"), {self.principal.run_id: PUBLIC_REPORT})
        self.error("unauthorized", lambda: self.store.authenticate(self.token))
        self.store.enforce_single_world("w1")
        self.error("world_scope", lambda: self.store.model_reports_for_world("w2"))

    def test_http_put_and_delete_use_authentication_and_strict_json(self):
        server = http.create_server(self.service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_address[1])

        def request(method, body=None, token=None):
            headers = {"Authorization": "Bearer " + (token or self.token)}
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = Request(origin + "/v1/me/model", method=method, data=body, headers=headers)
            try:
                response = urlopen(req, timeout=3)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.loads(response.read())

        try:
            self.assertEqual(request("PUT", json.dumps(REPORT).encode()), (200, {"model_report": PUBLIC_REPORT}))
            self.assertEqual(request("PUT", b'{"model_name":"a","model_name":"b","effort_level":"high"}')[0], 400)
            self.assertEqual(request("DELETE", token="bad")[0], 401)
            self.assertEqual(request("DELETE"), (200, {"model_report": None}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@unittest.skipIf(asgi is None, "ASGI dependencies not installed")
class ModelReportASGITests(unittest.IsolatedAsyncioTestCase):
    async def test_put_get_and_clear_through_real_service(self):
        store = Store(":memory:")
        engine = FakeEngine()
        service = Service(store, engine, autostart=False)
        token = store.provision("w1", "alice", 1)
        app = asgi.create_app(service)
        try:
            status, payload, _ = await asgi_call(app, "/v1/me/model", method="PUT", token=token,
                                               body=json.dumps(REPORT).encode())
            self.assertEqual((status, json.loads(payload)), (200, {"model_report": PUBLIC_REPORT}))
            status, payload, _ = await asgi_call(app, token=token)
            self.assertEqual((status, json.loads(payload)["model_report"]), (200, PUBLIC_REPORT))
            status, payload, _ = await asgi_call(app, "/v1/me/model", method="DELETE", token=token)
            self.assertEqual((status, json.loads(payload)), (200, {"model_report": None}))
            self.assertEqual(engine.calls, [])
        finally:
            service.close()
            store.close()
