"""Selected native observations retain pixels without creating input leases."""

import copy
from dataclasses import asdict
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.errors import ServiceError
from backend.json_tools import dumps
from backend.observations import ObservationImage
from backend.renderer import RendererGateway
from backend.tests.test_renderer import FakeRenderer, png, principal


class SelectedRenderer(FakeRenderer):
    def __init__(self, socket_path, artifacts):
        super().__init__(socket_path, artifacts)
        self.observation_version = 1
        self.frame = 17
        self.revision = 1
        self.busy = False
        self.lose_reply = False
        self.observations = {}
        self.render_count = 0
        self.observation_override = {}

    def call(self, message):
        kind = message["type"]
        if not kind.startswith("observation."):
            reply = super().call(message)
            if kind == "hello":
                reply["result"]["observation_version"] = self.observation_version
            return reply
        self.calls.append(copy.deepcopy(message))
        params = message["parameters"]
        token = params["observation_request_id"]
        reply = {"protocol_version": 1, "correlation_id": message["correlation_id"],
                 "world_id": "world", "world_epoch": "initial",
                 "engine_instance_id": self.instance, "status": "completed", "result": {}}
        error = ("renderer_busy" if self.busy else "replica_pending"
                 if params.get("minimum_source_frame", 0) > self.frame else None)
        if error:
            reply.update(status="rejected", error={"code": error})
            return reply
        if token in self.observations:
            fingerprint, result = self.observations[token]
            if fingerprint != dumps(params):
                reply.update(status="rejected", error={"code": "invalid_request"})
                return reply
        else:
            self.render_count += 1
            image_id = f"{1000 + self.render_count:032x}"
            width, height = ((params["output"]["width"], params["output"]["height"])
                             if kind == "observation.world" else (96, 64))
            path = self.artifacts / f"observation-{image_id}.png"
            path.write_bytes(png(width, height))
            path.chmod(0o600)
            result = {"image_id": image_id, "path": str(path), "width": width,
                      "height": height, "content_type": "image/png", "source_frame": self.frame,
                      "authority_instance_id": params["authority_instance_id"],
                      "world_epoch": "initial", "worker_instance_id": self.instance,
                      "renderer_lifecycle_revision": self.revision,
                      "capture_kind": kind.split(".", 1)[1]}
            result.update(self.observation_override)
            self.observations[token] = (dumps(params), result)
        reply["result"] = copy.deepcopy(result)
        if self.lose_reply:
            self.lose_reply = False
            raise ServiceError("engine_uncertain", status=503)
        return reply


class SelectedRendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "frames"
        self.artifacts.mkdir(mode=0o700)
        self.native = SelectedRenderer(self.root / "renderer.sock", self.artifacts)
        self.now = 100.0
        self.wall = 1_800_000_000.0
        self.sequence = 0
        self.gateway = self.make_gateway()
        self.addCleanup(self.gateway.close)
        self.p = principal()
        self.generation = self.gateway.observation_capabilities(self.p)["observation_generation"]

    def make_gateway(self, **options):
        gateway = RendererGateway({"run1": (self.native, self.artifacts)}, "world",
                                  clock=lambda: self.now, wall_clock=lambda: self.wall, **options)
        gateway.configure_authority("authority-1", 100)
        return gateway

    def request(self, kind="world", **changes):
        self.sequence += 1
        body = {"observation_generation": self.generation,
                "observation_request_id": f"obs1:{int(self.wall * 1000):013d}:{self.sequence:032x}",
                "wait_ms": 0}
        if kind == "world":
            body.update(selection={"kind": "map_center", "tile": {"x": 20, "y": 30},
                                   "zoom_level": "normal"}, output={"width": 64, "height": 64})
        else:
            body.update(panel="company_finances")
        body.update(changes)
        return body

    def observe(self, body, kind="world", barrier=None):
        return self.gateway.observe(self.p, kind, body, barrier or (lambda _: ("authority-1", None)))

    def assert_code(self, expected, function):
        with self.assertRaises(ServiceError) as caught:
            function()
        self.assertEqual(caught.exception.code, expected)

    def test_direct_pixels_exact_retry_and_closed_workflow_metadata(self):
        body = self.request()
        first = self.observe(body)
        self.assertIsInstance(first, ObservationImage)
        self.assertEqual(first.data, png(64, 64))
        self.assertEqual((first.width, first.height, first.causal_state), (64, 64, "none"))
        self.assertFalse(first.reused)
        self.assertEqual(set(asdict(first)), {"data", "observation_id", "image_id", "generation",
                                            "kind", "width", "height", "causal_state", "reused", "age_ms"})
        self.now += 0.25
        again = self.observe(body, barrier=lambda _: self.fail("exact retry resolved action again"))
        self.assertEqual(first.data, again.data)
        self.assertEqual(first.image_id, again.image_id)
        self.assertEqual(first.observation_id, again.observation_id)
        self.assertTrue(again.reused)
        self.assertEqual(again.age_ms, 250)
        self.assertEqual(self.native.render_count, 1)
        self.assertEqual(self.gateway.image(self.p, first.image_id), first.data)

    def test_observation_never_replaces_interaction_snapshot(self):
        interaction = self.gateway.capture(self.p)
        observed = self.observe(self.request())
        self.assertEqual(self.gateway._workers["run1"].latest[0], interaction["image_id"])
        self.assert_code("image_unavailable", lambda: self.gateway.control(
            self.p, {"image_id": observed.image_id, "action": "inspect", "x": 1, "y": 1}))
        self.assert_code("image_unavailable", lambda: self.gateway.validate_gesture(
            self.p, {"image_id": observed.image_id, "gesture": "click", "x": 1, "y": 1}))

    def test_observation_eviction_preserves_live_input_and_retry_tombstone(self):
        gateway = self.make_gateway(max_images=2)
        self.addCleanup(gateway.close)
        self.gateway = gateway
        self.generation = gateway.observation_capabilities(self.p)["observation_generation"]
        interaction = gateway.capture(self.p)
        old_body = self.request()
        old_image = self.observe(old_body)
        self.observe(self.request())
        self.assertEqual(gateway.image(self.p, interaction["image_id"]), png())
        self.assert_code("observation_expired", lambda: self.observe(old_body))
        self.assertEqual(self.native.render_count, 2)
        self.assert_code("image_unavailable", lambda: gateway.image(self.p, old_image.image_id))

    def test_input_cache_publication_is_atomic_against_another_workers_observation(self):
        other_artifacts = self.root / "other"
        other_artifacts.mkdir(mode=0o700)
        other = SelectedRenderer(self.root / "other.sock", other_artifacts)
        gateway = RendererGateway(
            {"run1": (self.native, self.artifacts), "run2": (other, other_artifacts)}, "world",
            clock=lambda: self.now, wall_clock=lambda: self.wall, max_images=1)
        self.addCleanup(gateway.close)
        gateway.configure_authority("authority-1", 100)
        published, finish_publication = threading.Event(), threading.Event()
        other_hello, other_done = threading.Event(), threading.Event()
        captures, observations, errors = [], [], []
        cache_image = gateway._cache_image
        other_call = other.call

        def paused_cache(*args, **kwargs):
            cache_image(*args, **kwargs)
            if args[0] == "run1" and not kwargs.get("protect_input"):
                published.set()
                if not finish_publication.wait(2):
                    raise AssertionError("input publication gate timed out")

        def signaled_call(message):
            result = other_call(message)
            if message["type"] == "hello":
                other_hello.set()
            return result

        gateway._cache_image = paused_cache
        other.call = signaled_call
        body = self.request()
        body["observation_generation"] = gateway._workers["run2"].observation_generation
        other_principal = principal(run="run2", company=2, identity="bob")

        def capture():
            try:
                captures.append(gateway.capture(self.p))
            except Exception as exc:
                errors.append(exc)

        def observe():
            try:
                observations.append(gateway.observe(other_principal, "world", body,
                                                    lambda _: ("authority-1", None)))
            except ServiceError as exc:
                observations.append(exc)
            except Exception as exc:
                errors.append(exc)
            finally:
                other_done.set()

        capture_thread = threading.Thread(target=capture)
        observe_thread = threading.Thread(target=observe)
        capture_thread.start()
        try:
            self.assertTrue(published.wait(1))
            observe_thread.start()
            self.assertTrue(other_hello.wait(1))
            self.assertFalse(other_done.wait(0.1))
        finally:
            finish_publication.set()
            capture_thread.join(2)
            if observe_thread.ident is not None:
                observe_thread.join(2)
        self.assertFalse(capture_thread.is_alive())
        self.assertFalse(observe_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(captures), 1)
        self.assertEqual(gateway.image(self.p, captures[0]["image_id"]), png())
        self.assertEqual(len(observations), 1)
        self.assertIsInstance(observations[0], ServiceError)
        self.assertEqual(observations[0].code, "renderer_unavailable")

    def test_changed_exact_body_conflicts_even_when_effective_defaults_match(self):
        body = self.request()
        self.observe(body)
        changed = copy.deepcopy(body)
        changed["labels"] = {"town_names": True}
        self.assert_code("observation_id_conflict", lambda: self.observe(changed))
        self.assertEqual(self.native.render_count, 1)

    def test_issued_expiry_and_nondecreasing_clock_prevent_redraw(self):
        body = self.request()
        self.observe(body)
        self.wall += 60
        self.assert_code("observation_expired", lambda: self.observe(body))
        self.wall -= 60
        self.assert_code("observation_expired", lambda: self.observe(body))
        self.assertEqual(self.native.render_count, 1)

    def test_future_clock_skew_is_bounded(self):
        body = self.request()
        body["observation_request_id"] = f"obs1:{int(self.wall * 1000) + 5001}:" + "a" * 32
        self.assert_code("invalid_request", lambda: self.observe(body))
        self.assertEqual(self.native.render_count, 0)

    def test_first_discovery_preserves_context_and_replacement_rotates_it(self):
        gateway = self.make_gateway()
        self.addCleanup(gateway.close)
        self.gateway = gateway
        self.generation = gateway._workers["run1"].observation_generation
        body = self.request()
        self.observe(body)
        self.assertEqual(gateway._workers["run1"].observation_generation, self.generation)
        self.native.instance = "renderer-2"
        self.assert_code("observation_expired", lambda: self.observe(body))
        self.assertNotEqual(gateway._workers["run1"].observation_generation, self.generation)
        self.assertEqual(self.native.render_count, 1)

    def test_gateway_restart_rejects_previous_generation(self):
        body = self.request()
        self.observe(body)
        gateway = self.make_gateway()
        self.addCleanup(gateway.close)
        self.assert_code("observation_expired", lambda: gateway.observe(
            self.p, "world", body, lambda _: ("authority-1", None)))
        self.assertEqual(self.native.render_count, 1)

    def test_capacity_is_temporary_and_never_evicts_request_identity(self):
        with patch("backend.renderer.MAX_REQUESTS", 2):
            first_body = self.request()
            self.observe(first_body)
            self.observe(self.request())
            result = self.observe(self.request())
            self.assertEqual(result["status"], "pending")
            self.assertEqual(self.native.render_count, 2)
            self.assertTrue(self.observe(first_body).reused)
            self.wall += 61
            self.now += 61
            self.assertIsInstance(self.observe(self.request()), ObservationImage)
            self.assertEqual(self.native.render_count, 3)

    def test_lost_native_reply_reuses_same_native_identity_and_pixels(self):
        self.native.lose_reply = True
        body = self.request()
        self.assert_code("renderer_unavailable", lambda: self.observe(body))
        result = self.observe(body)
        self.assertIsInstance(result, ObservationImage)
        self.assertEqual(self.native.render_count, 1)
        tokens = [call["parameters"]["observation_request_id"] for call in self.native.calls
                  if call["type"] == "observation.world"]
        self.assertEqual(tokens[0], tokens[1])

    def test_future_skew_cannot_extend_an_uncertain_native_token(self):
        self.native.lose_reply = True
        body = self.request()
        body["observation_request_id"] = f"obs1:{int(self.wall * 1000) + 5000}:" + "b" * 32
        self.assert_code("renderer_unavailable", lambda: self.observe(body))
        self.now += 60
        self.wall += 60
        # The public timestamp remains valid for five more seconds; native
        # deduplication does not, so the coordinator must refuse another call.
        self.native.observations.clear()
        self.assert_code("observation_expired", lambda: self.observe(body))
        self.assertEqual(self.native.render_count, 1)

    def test_after_barrier_wait_is_pending_then_satisfied(self):
        body = self.request(after_request_id="build-1")
        calls = []
        barrier = lambda request_id: (calls.append(request_id) or ("authority-1", 18))
        pending = self.observe(body, barrier=barrier)
        self.assertEqual(pending["reason"], "replica_pending")
        self.native.frame = 19
        result = self.observe(body, barrier=barrier)
        self.assertEqual(result.causal_state, "satisfied")
        self.assertEqual(calls, ["build-1"])
        self.assertEqual(self.native.render_count, 1)

    def test_action_pending_and_barrier_resolution_release_worker_lock(self):
        acquired = []

        def barrier(_):
            def probe():
                lock = self.gateway._workers["run1"].lock
                value = lock.acquire(blocking=False)
                acquired.append(value)
                if value:
                    lock.release()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            return None

        result = self.observe(self.request(after_request_id="queued"), barrier=barrier)
        self.assertEqual(result["reason"], "action_pending")
        self.assertEqual(acquired, [True])
        self.assertEqual(self.native.render_count, 0)

    def test_barrier_resolution_never_blocks_reacquiring_a_busy_worker(self):
        entered, release = threading.Event(), threading.Event()
        holders = []

        def barrier(_):
            def hold_worker():
                with self.gateway._workers["run1"].lock:
                    entered.set()
                    release.wait(2)
            holder = threading.Thread(target=hold_worker)
            holders.append(holder)
            holder.start()
            self.assertTrue(entered.wait(1))
            return "authority-1", None

        started = time.monotonic()
        try:
            pending = self.observe(self.request(), barrier=barrier)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(pending["reason"], "renderer_busy")
            self.assertEqual(self.native.render_count, 0)
        finally:
            release.set()
            for holder in holders:
                holder.join(2)
                self.assertFalse(holder.is_alive())

    def test_prior_world_point_resolves_owned_native_image_privately(self):
        old = self.observe(self.request())
        body = self.request(selection={"kind": "map_image_point", "observation_id": old.observation_id,
                                       "pixel": {"x": 30, "y": 20}, "zoom_level": "in2x"})
        self.observe(body)
        params = self.native.calls[-1]["parameters"]
        self.assertEqual(params["selection"]["image_id"], old.image_id)
        self.assertNotIn("observation_id", params["selection"])
        bad = self.request(selection={"kind": "map_image_point", "observation_id": old.observation_id,
                                      "pixel": {"x": 64, "y": 0}, "zoom_level": "normal"})
        self.assert_code("invalid_request", lambda: self.observe(bad))

    def test_native_info_images_are_not_world_point_sources(self):
        panel = self.observe(self.request("native_info"), "native_info")
        body = self.request(selection={"kind": "map_image_point", "observation_id": panel.observation_id,
                                       "pixel": {"x": 1, "y": 1}, "zoom_level": "normal"})
        self.assert_code("observation_expired", lambda: self.observe(body))
        self.assertEqual(self.native.render_count, 1)

    def test_private_provenance_and_requested_dimensions_are_validated(self):
        for override in ({"authority_instance_id": "foreign"}, {"world_epoch": "foreign"},
                         {"worker_instance_id": "foreign"}, {"width": 65},
                         {"source_frame": True}, {"renderer_lifecycle_revision": -1}):
            self.native.observation_override = override
            self.assert_code("renderer_unavailable", lambda: self.observe(self.request()))

    def test_observation_artifact_has_an_exact_distinct_filename(self):
        path = self.artifacts / f"frame-{1001:032x}.png"
        path.write_bytes(png(64, 64))
        path.chmod(0o600)
        self.native.observation_override = {"path": str(path)}
        self.assert_code("renderer_unavailable", lambda: self.observe(self.request()))

    def test_old_native_workers_keep_legacy_capture_but_cannot_observe(self):
        self.native.observation_version = 0
        self.assert_code("renderer_unavailable", lambda: self.gateway.observation_capabilities(self.p))
        self.assert_code("renderer_unavailable", lambda: self.observe(self.request()))
        self.assertEqual(self.gateway.capture(self.p)["width"], 2)


if __name__ == "__main__":
    unittest.main()
