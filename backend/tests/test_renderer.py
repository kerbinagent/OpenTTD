import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import zlib

from backend.errors import ServiceError
from backend.renderer import RendererGateway
from backend.store import Principal


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


def png(width=2, height=2, extra=b""):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\0" + b"\xff\x00\x00" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + extra + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class FakeRenderer:
    def __init__(self, socket_path, artifacts):
        self.socket_path = str(socket_path)
        self.artifacts = Path(artifacts)
        self.instance = "renderer-1"
        self.calls = []
        self.serial = 0
        self.hold_image = False
        self.image_bytes = png()
        self.path_override = None
        self.result_override = {}
        self.role = "renderer"
        self.status = "completed"
        self.bundle = None
        self.bundle_override = None
        self.phase = "captured"
        self.children = []
        self.unknown_status = False
        self.native_key = None

    def call(self, message):
        self.calls.append(message)
        result = {}
        status = self.status
        error = None
        if message["type"] == "hello":
            result = {"role": self.role, "engine_instance_id": self.instance,
                      "world_epoch": "initial"}
            if self.native_key is not None:
                result["native_public_key"] = self.native_key
        elif message["type"] == "image.request":
            if not self.hold_image or self.serial == 0:
                self.serial += 1
            image_id = f"{self.serial:032x}"
            path = self.artifacts / f"frame-{image_id}.png"
            path.write_bytes(self.image_bytes)
            path.chmod(0o600)
            result = {"image_id": image_id, "screenshot_id": image_id,
                      "path": str(path), "width": 2, "height": 2,
                      "tick": 0, "source_frame": 17, "content_type": "image/png"}
            result.update(self.result_override)
            if self.path_override is not None:
                result["path"] = self.path_override(path)
        elif message["type"] == "ui.capture":
            params = message["parameters"]
            self.bundle = {"version": 2, "request_id": params["request_id"],
                           "image_id": params["image_id"], "source_frame": 17,
                           "authority_instance_id": params["authority_instance_id"],
                           "world_epoch": "initial", "worker_instance_id": self.instance,
                           "owner": {"client_id": 5, "public_key": "key"},
                           "commands": [], "tile_proof": None}
            if self.bundle_override is not None:
                self.bundle_override(self.bundle)
            result = self.bundle
        elif message["type"] == "ui.status":
            if self.unknown_status:
                status = "rejected"
                error = {"code": "unknown_request", "message": "Integration request unavailable"}
            else:
                result = {"bundle": self.bundle, "pending": self.phase in ("captured", "committed"),
                          "phase": self.phase, "callback_rejected": False,
                          "children": self.children}
        elif message["type"] == "ui.release":
            phase = message["parameters"]["phase"]
            if phase == "commit":
                self.phase = "completed"
            elif phase == "reject":
                self.phase = "rejected"
            result = {"status": "ok"}
        reply = {"protocol_version": 1, "correlation_id": message["correlation_id"],
                "world_id": "world", "world_epoch": "initial",
                "engine_instance_id": self.instance, "status": status,
                "result": result}
        if error is not None:
            reply["error"] = error
        return reply


def principal(run="run1", company=1, identity="alice", mode="visual"):
    return Principal(identity, "world", company, run, mode, "agent", identity)


class RendererGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dir1 = self.root / "a"
        self.dir1.mkdir(mode=0o700)
        self.fake1 = FakeRenderer(self.root / "a.sock", self.dir1)
        self.now = [100.0]
        self.gateway = self.make_gateway()
        self.addCleanup(self.gateway.close)

    def make_gateway(self, other=None, **options):
        workers = {"run1": {"engine": self.fake1, "artifacts": self.dir1}}
        if other is not None:
            workers["run2"] = {"engine": other, "artifacts": other.artifacts}
        return RendererGateway(workers, "world", clock=lambda: self.now[0], **options)

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def advance(self, seconds=1):
        self.now[0] += seconds

    def test_capture_and_bound_readonly_controls(self):
        p = principal()
        self.assertTrue(self.gateway.available(p))
        image = self.gateway.capture(p)
        self.assertEqual(set(image), {"image_id", "width", "height"})
        self.assertEqual(self.gateway.image(p, image["image_id"]), png())

        bind = [call for call in self.fake1.calls if call["type"] == "session.bind"]
        self.assertEqual(len(bind), 1)
        self.assertEqual(bind[0]["actor"], {"identity_id": "alice", "company_id": 1,
                                           "run_id": "run1", "mode": "visual"})
        self.assertEqual(self.gateway.control(p, {"image_id": image["image_id"],
                                                  "action": "inspect", "x": 1, "y": 0}),
                         {"status": "completed"})
        request = self.fake1.calls[-1]
        self.assertEqual(request["type"], "view.control")
        self.assertEqual(request["parameters"], {"action": "inspect", "x": 1, "y": 0,
                                                  "screenshot_id": image["image_id"]})
        self.assert_code("image_unavailable", lambda: self.gateway.control(p, {
            "image_id": image["image_id"], "action": "zoom", "steps": 1}))
        self.assertEqual(self.gateway.image(p, image["image_id"]), png())

    def test_dynamic_slots_start_idle_and_keep_immutable_actor_and_image_scope(self):
        dir2 = self.root / "b"
        dir2.mkdir(mode=0o700)
        fake2 = FakeRenderer(self.root / "b.sock", dir2)
        self.fake1.native_key, fake2.native_key = "AB" * 32, "cd" * 32
        gateway = RendererGateway({}, "world", slots={
            "capture-0": (self.fake1, self.dir1), "capture-1": (fake2, dir2),
        })
        self.addCleanup(gateway.close)
        p, other = principal(), principal("run2", 2, "bob")
        self.assertEqual(gateway.worker_slots(), ("capture-0", "capture-1"))
        self.assertFalse(gateway.available(p))
        self.assertEqual(gateway.verified_native_worker_keys(), {"ab" * 32, "cd" * 32})
        self.assertEqual(gateway.worker_key("capture-0"), "ab" * 32)
        self.assertFalse(any(call["type"] == "session.bind" for call in self.fake1.calls))
        gateway.assign_slot("capture-0", p)
        gateway.assign_slot("capture-0", p)
        gateway.assign_slot("capture-1", other)
        gateway.check_ready(p)
        image = gateway.capture(p)
        self.assertEqual(gateway.image(p, image["image_id"]), png())
        self.assert_code("image_unavailable", lambda: gateway.image(other, image["image_id"]))
        for slot, changed in (("capture-0", other), ("capture-1", p),
                              ("capture-0", principal(company=2)),
                              ("capture-0", principal(identity="mallory")),
                              ("capture-0", principal(mode="text"))):
            self.assert_code("forbidden", lambda slot=slot, changed=changed:
                             gateway.assign_slot(slot, changed))

    def test_dynamic_slot_assignment_race_cannot_share_one_worker(self):
        gateway = RendererGateway({}, "world", slots={"capture-0": (self.fake1, self.dir1)})
        self.addCleanup(gateway.close)
        barrier = threading.Barrier(2)

        def assign(p):
            barrier.wait(timeout=5)
            try:
                gateway.assign_slot("capture-0", p)
                return True
            except ServiceError as exc:
                self.assertEqual(exc.code, "forbidden")
                return False

        players = [principal(), principal("run2", 2, "bob")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(assign, players))
        self.assertEqual(sum(result), 1)
        self.assertEqual([gateway.available(p) for p in players], result)

    def test_dynamic_idle_key_failure_keeps_managed_provenance(self):
        self.fake1.native_key = "ab" * 32
        gateway = RendererGateway({}, "world", slots={"capture-0": (self.fake1, self.dir1)})
        self.addCleanup(gateway.close)
        self.assertEqual(gateway.worker_key("capture-0"), "ab" * 32)
        self.fake1.native_key = "malformed"
        self.assert_code("renderer_unavailable", gateway.verified_native_worker_keys)
        self.assertEqual(gateway.known_native_worker_keys(), set())
        self.assertEqual(gateway.managed_native_worker_keys(), {"ab" * 32})
        self.assert_code("renderer_unavailable", lambda: gateway.worker_key("unknown"))

    def test_legacy_and_dynamic_slots_cannot_share_native_key(self):
        other_dir = self.root / "pooled"
        other_dir.mkdir(mode=0o700)
        pooled = FakeRenderer(self.root / "pooled.sock", other_dir)
        self.fake1.native_key = pooled.native_key = "ab" * 32
        gateway = RendererGateway({"run1": (self.fake1, self.dir1)}, "world",
                                  slots={"capture-0": (pooled, other_dir)})
        self.addCleanup(gateway.close)
        self.assert_code("renderer_unavailable", gateway.verified_native_worker_keys)

    def test_text_run_captures_and_cross_run_or_company_cannot_read(self):
        dir2 = self.root / "b"
        dir2.mkdir(mode=0o700)
        fake2 = FakeRenderer(self.root / "b.sock", dir2)
        other = self.make_gateway(fake2)
        self.addCleanup(other.close)
        p = principal(mode="text")
        image = other.capture(p)
        self.assertEqual(other.image(p, image["image_id"]), png())
        self.assert_code("image_unavailable", lambda: other.image(principal("run2", 2, "bob"), image["image_id"]))
        self.assert_code("image_unavailable", lambda: other.image(principal(company=2), image["image_id"]))
        self.assert_code("forbidden", lambda: other.capture(principal(company=2)))
        self.assert_code("image_unavailable", lambda: other.image(principal("unknown"), image["image_id"]))

    def test_one_run_per_socket_and_owner_only_artifact_directory(self):
        dir2 = self.root / "b"
        dir2.mkdir(mode=0o700)
        fake2 = FakeRenderer(self.fake1.socket_path, dir2)
        with self.assertRaises(ValueError):
            self.make_gateway(fake2)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        fake2.socket_path = str(self.root / "b.sock")
        fake2.artifacts = alias / "a"
        with self.assertRaises(ValueError):
            self.make_gateway(fake2)
        dir2.chmod(0o755)
        with self.assertRaises(ValueError):
            RendererGateway({"run2": {"engine": fake2, "artifacts": dir2}}, "world")

    def test_artifact_traversal_symlink_and_private_mode(self):
        p = principal()
        self.fake1.path_override = lambda path: str(path.parent / ".." / path.name)
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture(p))

        target = self.root / "target"
        target.write_bytes(png())
        target.chmod(0o600)

        def symlink(path):
            path.unlink()
            path.symlink_to(target)
            return str(path)

        self.fake1.path_override = symlink
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture(p))

        def shared_file(path):
            path.chmod(0o644)
            return str(path)

        self.fake1.path_override = shared_file
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture(p))

    def test_png_metadata_crc_dimensions_and_size_are_rejected(self):
        p = principal()
        header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
        broken_deflate = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"bad") + chunk(b"IEND", b"")
        for data in (png(extra=chunk(b"tEXt", b"hidden\x00metadata")),
                     png()[:-1] + b"x", png(width=3), broken_deflate,
                     b"x" * (16 * 1024 * 1024 + 1)):
            self.fake1.image_bytes = data
            self.assert_code("renderer_unavailable", lambda: self.gateway.capture(p))

    def test_private_authority_lease_fields_rebind_once_and_fence_changes(self):
        p = principal()
        self.gateway.capture(p)
        self.assertEqual(len([c for c in self.fake1.calls if c["type"] == "session.bind"]), 1)
        for args in (("", 300), ("world-instance", 0), ("world-instance", 65536),
                     ("world-instance", True)):
            with self.assertRaises(ValueError):
                self.gateway.configure_authority(*args)
        self.gateway.configure_authority("world-instance", 300)
        self.gateway.configure_authority("world-instance", 300)
        self.advance()
        self.gateway.capture(p)
        binds = [c for c in self.fake1.calls if c["type"] == "session.bind"]
        self.assertEqual(len(binds), 2)
        self.assertEqual(binds[-1]["authority_instance_id"], "world-instance")
        self.assertEqual(binds[-1]["authority_max_lag_ticks"], 300)
        self.advance()
        self.gateway.capture(p)
        self.assertEqual(len([c for c in self.fake1.calls if c["type"] == "session.bind"]), 2)
        self.gateway.configure_authority("new-world-instance", 300)
        self.advance()
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture(p))
        self.fake1.instance = "renderer-2"
        self.gateway.capture(p)
        binds = [c for c in self.fake1.calls if c["type"] == "session.bind"]
        self.assertEqual(len(binds), 3)
        self.assertEqual(binds[-1]["authority_instance_id"], "new-world-instance")

    def test_restart_expiry_control_invalidation_and_rate(self):
        p = principal()
        image = self.gateway.capture(p)
        self.assertEqual(self.gateway.capture(p), image)
        self.advance(1)
        next_image = self.gateway.capture(p)
        self.assert_code("image_unavailable", lambda: self.gateway.control(p, {
            "image_id": image["image_id"], "action": "pan", "dx": 1, "dy": 0}))
        self.advance(41)
        self.assert_code("image_unavailable", lambda: self.gateway.control(p, {
            "image_id": next_image["image_id"], "action": "zoom", "steps": 1}))
        self.advance(45)
        self.assert_code("image_unavailable", lambda: self.gateway.image(p, image["image_id"]))
        third = self.gateway.capture(p)
        self.fake1.instance = "renderer-2"
        self.assertEqual(self.gateway.image(p, third["image_id"]), png())
        self.assert_code("rate_limited", lambda: self.gateway.capture(p))
        self.assert_code("image_unavailable", lambda: self.gateway.image(p, third["image_id"]))
        self.assert_code("image_unavailable", lambda: self.gateway.control(p, {
            "image_id": third["image_id"], "action": "close_windows"}))

    def test_parallel_capture_reuses_one_frame_until_control_invalidates(self):
        p = principal()
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda _: self.gateway.capture(p), range(50)))
        self.assertEqual(results, [results[0]] * 50)
        self.assertEqual(len([call for call in self.fake1.calls if call["type"] == "image.request"]), 1)
        self.assertEqual(self.gateway.control(p, {"image_id": results[0]["image_id"],
                                                  "action": "close_windows"}),
                         {"status": "completed"})
        self.assert_code("rate_limited", lambda: self.gateway.capture(p))
        self.advance()
        self.assertNotEqual(self.gateway.capture(p)["image_id"], results[0]["image_id"])

    def test_capture_reuse_does_not_extend_ttl_or_cross_worker_restart(self):
        gateway = self.make_gateway(ttl=0.25)
        self.addCleanup(gateway.close)
        p = principal()
        first = gateway.capture(p)
        self.advance(0.2)
        self.assertEqual(gateway.capture(p), first)
        self.advance(0.1)
        self.assert_code("rate_limited", lambda: gateway.capture(p))
        self.fake1.instance = "renderer-2"
        self.assert_code("rate_limited", lambda: gateway.capture(p))
        self.advance(0.7)
        self.assertNotEqual(gateway.capture(p)["image_id"], first["image_id"])

    def test_configured_capture_interval_coalesces_and_respects_boundary(self):
        gateway = self.make_gateway(capture_interval=5)
        self.addCleanup(gateway.close)
        p = principal()
        first = gateway.capture(p)
        self.advance(4.999)
        self.assertEqual(gateway.capture(p), first)
        self.assertEqual(len([c for c in self.fake1.calls if c["type"] == "image.request"]), 1)
        self.advance(0.001)
        self.assertNotEqual(gateway.capture(p)["image_id"], first["image_id"])
        self.assertEqual(len([c for c in self.fake1.calls if c["type"] == "image.request"]), 2)

    def test_configured_interval_control_eviction_and_validation(self):
        for invalid in (True, False, 0.9, 60.1, float("nan"), float("inf"),
                        -float("inf"), 10**1000, "5"):
            with self.assertRaises(ValueError):
                self.make_gateway(capture_interval=invalid)
        dir2 = self.root / "b"
        dir2.mkdir(mode=0o700)
        fake2 = FakeRenderer(self.root / "b.sock", dir2)
        gateway = self.make_gateway(fake2, max_images=1, capture_interval=5)
        self.addCleanup(gateway.close)
        p = principal()
        first = gateway.capture(p)
        self.assertEqual(gateway.control(p, {"image_id": first["image_id"],
                                             "action": "close_windows"}),
                         {"status": "completed"})
        self.assert_code("rate_limited", lambda: gateway.capture(p))
        self.advance(5)
        second = gateway.capture(p)
        self.advance(1)
        gateway.capture(principal("run2", 2, "bob"))
        self.assert_code("rate_limited", lambda: gateway.capture(p))
        self.advance(4)
        self.assertNotEqual(gateway.capture(p)["image_id"], second["image_id"])

    def test_native_worker_key_shape_case_restart_and_freshness(self):
        p = principal()
        uppercase = "A1" * 32
        lowercase = "b2" * 32
        self.fake1.native_key = uppercase
        image = self.gateway.capture(p)
        calls = len(self.fake1.calls)
        self.assertEqual(self.gateway.known_native_worker_keys(), {uppercase.lower()})
        self.assertEqual(len(self.fake1.calls), calls)
        self.advance(30)
        self.assertEqual(self.gateway.known_native_worker_keys(), {uppercase.lower()})
        self.advance(0.01)
        self.assertEqual(self.gateway.known_native_worker_keys(), set())
        self.fake1.instance = "renderer-2"
        self.fake1.native_key = lowercase
        self.assertEqual(self.gateway.image(p, image["image_id"]), png())
        self.gateway.capture(p)
        self.assert_code("image_unavailable", lambda: self.gateway.image(p, image["image_id"]))
        self.assertEqual(self.gateway.known_native_worker_keys(), {lowercase})

    def test_managed_worker_provenance_outlives_health_and_replacement(self):
        p = principal()
        first_key = "A1" * 32
        second_key = "b2" * 32
        self.fake1.native_key = first_key
        self.gateway.capture(p)
        self.assertEqual(self.gateway.managed_native_worker_keys(), {first_key.lower()})
        self.advance(31)
        self.assertEqual(self.gateway.known_native_worker_keys(), set())
        self.assertEqual(self.gateway.managed_native_worker_keys(), {first_key.lower()})
        self.fake1.status = "rejected"
        self.assert_code("renderer_unavailable", self.gateway.verified_native_worker_keys)
        self.assertEqual(self.gateway.managed_native_worker_keys(), {first_key.lower()})
        self.fake1.status = "completed"
        self.fake1.instance = "renderer-2"
        self.fake1.native_key = second_key
        self.assertEqual(self.gateway.verified_native_worker_keys(),
                         {first_key.lower(), second_key})
        self.assertEqual(self.gateway.managed_native_worker_keys(),
                         {first_key.lower(), second_key})

    def test_verified_worker_keys_require_all_configured_hellos(self):
        dir2 = self.root / "b"
        dir2.mkdir(mode=0o700)
        second = FakeRenderer(self.root / "b.sock", dir2)
        gateway = self.make_gateway(second)
        self.addCleanup(gateway.close)
        self.fake1.native_key = "aa" * 32
        second.native_key = ""
        self.assert_code("renderer_unavailable", gateway.verified_native_worker_keys)
        self.assertEqual(gateway.managed_native_worker_keys(), {"aa" * 32})
        second.native_key = "Bb" * 32
        self.assertEqual(gateway.verified_native_worker_keys(), {"aa" * 32, "bb" * 32})
        second.native_key = "invalid"
        self.assert_code("renderer_unavailable", gateway.verified_native_worker_keys)
        self.assertEqual(gateway.managed_native_worker_keys(), {"aa" * 32, "bb" * 32})

    def test_managed_worker_provenance_overflow_fails_closed(self):
        for invalid in (0, -1, True, 1025):
            with self.assertRaises(ValueError):
                self.make_gateway(max_managed_worker_keys=invalid)
        gateway = self.make_gateway(max_managed_worker_keys=1)
        self.addCleanup(gateway.close)
        p = principal()
        self.fake1.native_key = "aa" * 32
        gateway.capture(p)
        self.fake1.instance = "renderer-2"
        self.fake1.native_key = "bb" * 32
        self.assert_code("rate_limited", lambda: gateway.capture(p))
        self.assertEqual(gateway.managed_native_worker_keys(), {"aa" * 32})
        self.assert_code("renderer_unavailable", gateway.verified_native_worker_keys)

    def test_native_worker_key_invalid_empty_and_rpc_failure_clear(self):
        p = principal()
        self.fake1.native_key = "ab" * 32
        self.gateway.capture(p)
        self.assertEqual(self.gateway.known_native_worker_keys(), {"ab" * 32})
        self.fake1.native_key = ""
        self.gateway.capture(p)
        self.assertEqual(self.gateway.known_native_worker_keys(), set())
        for invalid in ("G" * 64, "a" * 63, "a" * 65, 123, None):
            self.fake1.native_key = invalid
            self.gateway.capture(p)
            self.assertEqual(self.gateway.known_native_worker_keys(), set())
            self.advance()
        self.fake1.native_key = "cd" * 32
        self.gateway.capture(p)
        self.assertEqual(self.gateway.known_native_worker_keys(), {"cd" * 32})
        self.fake1.status = "rejected"
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture(p))
        self.assertEqual(self.gateway.known_native_worker_keys(), set())
        self.fake1.status = "completed"
        self.fake1.native_key = "de" * 32
        original_call = self.fake1.call

        def fail_image(message):
            if message["type"] == "image.request":
                raise OSError("socket lost")
            return original_call(message)

        self.fake1.call = fail_image
        self.advance()
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture(p))
        self.assertEqual(self.gateway.known_native_worker_keys(), set())

    def test_native_worker_key_snapshot_does_not_wait_for_worker_rpc(self):
        p = principal()
        self.fake1.native_key = "ef" * 32
        self.gateway.capture(p)
        original_call = self.fake1.call
        entered = threading.Event()
        release = threading.Event()

        def blocked_hello(message):
            if message["type"] == "hello":
                entered.set()
                release.wait(timeout=2)
            return original_call(message)

        self.fake1.call = blocked_hello
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.gateway.capture, p)
            self.assertTrue(entered.wait(timeout=1))
            started = time.monotonic()
            self.assertEqual(self.gateway.known_native_worker_keys(), {"ef" * 32})
            self.assertEqual(self.gateway.managed_native_worker_keys(), {"ef" * 32})
            self.assertLess(time.monotonic() - started, 0.1)
            release.set()
            self.assertEqual(future.result(timeout=2)["width"], 2)

    def test_parallel_image_gets_use_cache_during_blocked_worker_rpc(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        original_call = self.fake1.call
        entered = threading.Event()
        release = threading.Event()

        def blocked_hello(message):
            if message["type"] == "hello":
                entered.set()
                release.wait(timeout=2)
            return original_call(message)

        self.fake1.call = blocked_hello
        with ThreadPoolExecutor(max_workers=51) as pool:
            capture_future = pool.submit(self.gateway.capture, p)
            self.assertTrue(entered.wait(timeout=1))
            count = len(self.fake1.calls)
            image_futures = [pool.submit(self.gateway.image, p, image_id) for _ in range(50)]
            try:
                self.assertEqual([future.result(timeout=1) for future in image_futures], [png()] * 50)
                self.assertEqual(len(self.fake1.calls), count)
                self.assert_code("image_unavailable", lambda: self.gateway.image(principal(company=2), image_id))
            finally:
                release.set()
            self.assertEqual(capture_future.result(timeout=2)["image_id"], image_id)

    def test_noticed_worker_replacement_purges_cache_before_publication(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        original_drop = self.gateway._drop_run_locked
        entered = threading.Event()
        release = threading.Event()

        def delayed_drop(worker):
            entered.set()
            release.wait(timeout=2)
            original_drop(worker)

        self.gateway._drop_run_locked = delayed_drop
        self.fake1.instance = "renderer-2"
        with ThreadPoolExecutor(max_workers=2) as pool:
            refresh_future = pool.submit(self.gateway.capture, p)
            self.assertTrue(entered.wait(timeout=1))
            read_future = pool.submit(self.gateway.image, p, image_id)
            try:
                self.assertFalse(read_future.done())
            finally:
                release.set()
            self.assert_code("image_unavailable", lambda: read_future.result(timeout=2))
            self.assert_code("rate_limited", lambda: refresh_future.result(timeout=2))
        self.assert_code("image_unavailable", lambda: self.gateway.image(p, image_id))

    def test_parked_native_repeat_keeps_original_cache_expiry(self):
        gateway = self.make_gateway(ttl=2)
        self.addCleanup(gateway.close)
        self.fake1.hold_image = True
        p = principal()
        first = gateway.capture(p)
        original_expiry = gateway._cache[(p.run_id, first["image_id"])].expires
        self.advance(1.1)
        self.assertEqual(gateway.capture(p), first)
        self.advance(0.1)
        self.assertEqual(gateway.capture(p), first)
        self.assertEqual(gateway._cache[(p.run_id, first["image_id"])].expires,
                         original_expiry)
        self.assertEqual(len([c for c in self.fake1.calls if c["type"] == "image.request"]), 2)

    def test_parked_repeat_cannot_restore_evicted_image_or_change_frame(self):
        dir2 = self.root / "b"
        dir2.mkdir(mode=0o700)
        fake2 = FakeRenderer(self.root / "b.sock", dir2)
        gateway = self.make_gateway(fake2, max_images=1)
        self.addCleanup(gateway.close)
        self.fake1.hold_image = True
        p = principal()
        gateway.capture(p)
        self.advance()
        gateway.capture(principal("run2", 2, "bob"))
        self.assert_code("renderer_unavailable", lambda: gateway.capture(p))

        # A second gateway exercises the retained-cache source-frame fence.
        self.advance()
        retained = self.make_gateway()
        self.addCleanup(retained.close)
        retained.capture(p)
        self.fake1.result_override = {"source_frame": 18}
        self.advance()
        self.assert_code("renderer_unavailable", lambda: retained.capture(p))

    def test_strict_control_fields_and_private_reply_projection(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        base = {"image_id": image_id}
        for body in ({**base, "action": "pan", "dx": 3, "dy": 0},
                     {**base, "action": "zoom", "steps": True},
                     {**base, "action": "inspect", "x": 2, "y": 0},
                     {**base, "action": "open_window", "window": "vehicles"},
                     {**base, "action": "open_window", "window": "company", "company_id": 2},
                     {**base, "action": "close_windows", "map_x": 7}):
            self.assert_code("invalid_request", lambda body=body: self.gateway.control(p, body))
        self.assertEqual(self.gateway.control(p, {**base, "action": "open_window", "window": "vehicles", "vehicle_type": "rail"}),
                         {"status": "completed"})
        self.assertEqual(self.fake1.calls[-1]["type"], "window.open")
        self.assertEqual(self.fake1.calls[-1]["parameters"], {"window": "vehicles", "vehicle_type": "rail"})

    def test_native_construction_window_allowlist_uses_no_public_ids(self):
        p = principal()
        for window in ("road_construction", "rail_construction", "water_construction",
                       "airport_construction", "landscaping", "trees"):
            image_id = self.gateway.capture(p)["image_id"]
            self.assertEqual(self.gateway.control(p, {"image_id": image_id,
                                                        "action": "open_window", "window": window}),
                             {"status": "completed"})
            self.assertEqual(self.fake1.calls[-1]["type"], "window.open")
            self.assertEqual(self.fake1.calls[-1]["parameters"], {"window": window})
            self.advance()

    def test_latest_image_allows_under_40_seconds_but_native_deadline_can_reject(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        gesture = {"image_id": image_id, "gesture": "click", "x": 1, "y": 0}
        self.advance(30)
        self.gateway.validate_gesture(p, gesture)
        original_call = self.fake1.call

        def expired_native_lease(message):
            if message["type"] == "ui.capture":
                return {"protocol_version": 1, "correlation_id": message["correlation_id"],
                        "world_id": "world", "world_epoch": "initial",
                        "engine_instance_id": self.fake1.instance, "status": "rejected",
                        "result": {}, "error": {"code": "stale_view", "message": "Unavailable"}}
            return original_call(message)

        self.fake1.call = expired_native_lease
        expected = self.gateway.prepare_intent(p, "world-instance", 300)
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture_intent(
            p, "late-gesture", gesture, "world-instance", 300,
            expected_worker_instance_id=expected))
        self.assert_code("image_unavailable", lambda: self.gateway.validate_gesture(p, gesture))

    def test_parked_recapture_and_image_get_do_not_renew_40_second_age(self):
        p = principal()
        self.fake1.hold_image = True
        first = self.gateway.capture(p)
        gesture = {"image_id": first["image_id"], "gesture": "move", "x": 0, "y": 0}
        self.advance(20)
        self.assertEqual(self.gateway.capture(p), first)
        self.assertEqual(self.gateway.image(p, first["image_id"]), png())
        self.advance(19)
        self.gateway.validate_gesture(p, gesture)
        self.advance(1.01)
        self.assert_code("image_unavailable", lambda: self.gateway.validate_gesture(p, gesture))
        self.assert_code("image_unavailable", lambda: self.gateway.control(p, {
            "image_id": first["image_id"], "action": "zoom", "steps": 1}))

    def test_global_cache_budget_and_control_rate(self):
        gateway = self.make_gateway(max_images=2, max_cache_bytes=len(png()) * 2)
        self.addCleanup(gateway.close)
        p = principal()
        first = gateway.capture(p)["image_id"]
        self.advance()
        second = gateway.capture(p)["image_id"]
        self.advance()
        third = gateway.capture(p)["image_id"]
        self.assert_code("image_unavailable", lambda: gateway.image(p, first))
        self.assertEqual(gateway.image(p, second), png())
        self.assertEqual(gateway.image(p, third), png())
        for _ in range(16):
            self.assert_code("image_unavailable", lambda: gateway.control(p, {"image_id": first, "action": "close_windows"}))
        self.assert_code("rate_limited", lambda: gateway.control(p, {"image_id": third, "action": "close_windows"}))

    def test_gesture_validation_is_local_owned_and_strict(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        click = {"image_id": image_id, "gesture": "click", "x": 1, "y": 0}
        calls = len(self.fake1.calls)
        self.gateway.validate_gesture(p, click)
        self.gateway.validate_gesture(p, {**click, "button": "right"})
        self.gateway.validate_gesture(p, {**click, "ctrl": True})
        self.gateway.validate_gesture(p, {**click, "ctrl": False})
        self.gateway.validate_gesture(p, {**click, "gesture": "move"})
        self.gateway.validate_gesture(p, {**click, "gesture": "drag", "end_x": 0,
                                          "end_y": 1, "button": "left", "ctrl": True})
        self.assertEqual(len(self.fake1.calls), calls)
        for params in ({**click, "x": 2}, {**click, "x": True},
                       {**click, "end_x": 1}, {**click, "tile": 3},
                       {**click, "gesture": "move", "button": "left"},
                       {**click, "gesture": "move", "end_x": 1},
                       {**click, "gesture": "move", "ctrl": False},
                       {**click, "ctrl": 1}, {**click, "ctrl": "true"},
                       {**click, "gesture": "drag", "end_x": 1, "end_y": 1,
                        "button": "right"}):
            self.assert_code("invalid_request", lambda params=params: self.gateway.validate_gesture(p, params))
        self.assert_code("forbidden", lambda: self.gateway.validate_gesture(principal(mode="text"), click))
        self.assert_code("image_unavailable", lambda: self.gateway.validate_gesture(principal("other"), click))
        self.advance(41)
        self.assert_code("image_unavailable", lambda: self.gateway.validate_gesture(p, click))
        self.assertEqual(len(self.fake1.calls), calls)

    def test_text_gesture_utf8_boundary_controls_and_exact_fields(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        base = {"image_id": image_id, "gesture": "text", "x": 1, "y": 0}
        calls = len(self.fake1.calls)
        for value in ("", "  À  ", "é" * 2048, "😀" * 1024):
            self.gateway.validate_gesture(p, {**base, "text": value})
        self.assertEqual(len(self.fake1.calls), calls)
        invalid = ["é" * 2049, "😀" * 1025, "\ud800", "line\nfeed", "\r", "\t",
                   "\0", "\x1b", "\x7f", "\x85", "\ue000", "\ue1ff",
                   "\ue200", "\ue2ff", "\u2028", "\u2029"]
        for value in invalid:
            self.assert_code("invalid_request", lambda value=value:
                             self.gateway.validate_gesture(p, {**base, "text": value}))
        for body in ({**base}, {**base, "text": 4}, {**base, "text": "okay", "button": "left"},
                     {**base, "text": "okay", "ctrl": False},
                     {**base, "text": "okay", "end_x": 1},
                     {**base, "text": "okay", "end_y": 1},
                     {**base, "text": "okay", "native_command": 7},
                     {**base, "text": "okay", "x": True},
                     {**base, "text": "okay", "y": 2}):
            self.assert_code("invalid_request", lambda body=body:
                             self.gateway.validate_gesture(p, body))
        self.advance(41)
        self.assert_code("image_unavailable", lambda:
                         self.gateway.validate_gesture(p, {**base, "text": "okay"}))
        self.assertEqual(len(self.fake1.calls), calls)

    def test_text_intent_preserves_exact_utf8_and_empty_value(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        params = {"image_id": image_id, "gesture": "text", "x": 1, "y": 1,
                  "text": "  Café 😀  "}
        expected = self.gateway.prepare_intent(p, "world-instance", 300)
        bundle = self.gateway.capture_intent(p, "text-1", params,
                                             "world-instance", 300,
                                             expected_worker_instance_id=expected)
        self.assertEqual(bundle["commands"], [])
        sent = [c for c in self.fake1.calls if c["type"] == "ui.capture"][-1]["parameters"]
        self.assertEqual(sent, {**params, "request_id": "text-1",
                                "authority_instance_id": "world-instance",
                                "world_epoch": "initial"})
        self.gateway.release_intent(p, "text-1")
        self.advance()
        second = self.gateway.capture(p)
        empty = {"image_id": second["image_id"], "gesture": "text", "x": 1,
                 "y": 1, "text": ""}
        self.gateway.capture_intent(p, "text-2", empty, "world-instance", 300,
                                    expected_worker_instance_id=expected)
        sent_empty = [c for c in self.fake1.calls if c["type"] == "ui.capture"][-1]["parameters"]
        self.assertEqual(sent_empty["text"], "")

    def test_intent_capture_bundle_and_release_private_wire(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        params = {"image_id": image_id, "gesture": "click", "x": 1, "y": 0,
                  "button": "right", "ctrl": True}
        bundle = self.gateway.capture_intent(p, "request-1", params, "world-instance", 300)
        self.assertEqual(bundle["owner"], {"client_id": 5, "public_key": "key"})
        self.assertEqual(bundle["version"], 2)
        self.assertEqual(bundle["commands"], [])
        self.assertIsNone(bundle["tile_proof"])
        self.assertNotIn("state", bundle)
        captured = [c for c in self.fake1.calls if c["type"] == "ui.capture"]
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["parameters"], {**params, "request_id": "request-1",
                                                      "authority_instance_id": "world-instance",
                                                      "world_epoch": "initial"})
        self.assertEqual(self.gateway.intent_status(p, "request-1")["phase"], "captured")
        self.gateway.release_intent(p, "request-1")
        self.gateway.release_intent(p, "request-1")
        self.assertEqual(self.gateway.intent_status(p, "request-1")["phase"], "completed")
        self.assert_code("image_unavailable", lambda: self.gateway.validate_gesture(p, params))
        self.assertEqual(len([c for c in self.fake1.calls if c["type"] == "ui.capture"]), 1)

    def test_ui_controls_forward_exact_choice_under_owned_image(self):
        p = principal()
        choices = [("orders.go_to", None)] + [
            ("road_stop.orientation", orientation) for orientation in (
                "terminal_ne", "terminal_se", "terminal_sw", "terminal_nw",
                "drive_through_ne_sw", "drive_through_se_nw")]
        for index, (control, orientation) in enumerate(choices):
            with self.subTest(control=control, orientation=orientation):
                image_id = self.gateway.capture(p)["image_id"]
                params = {"image_id": image_id, "gesture": "control",
                          "control": control, "x": 1, "y": 0}
                if orientation is not None:
                    params["orientation"] = orientation
                self.gateway.validate_gesture(p, params)
                bundle = self.gateway.capture_intent(p, f"control-{index}",
                    params, "world-instance", 300)
                self.assertEqual(bundle["commands"], [])
                self.assertIsNone(bundle["tile_proof"])
                self.assertNotIn("state", bundle)
                sent = [call for call in self.fake1.calls
                        if call["type"] == "ui.capture"][-1]["parameters"]
                self.assertEqual(sent, {**params, "request_id": f"control-{index}",
                                        "authority_instance_id": "world-instance",
                                        "world_epoch": "initial"})
                self.advance()

    def test_ui_control_shape_rejects_mixed_keys_and_malformed_choices(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        base = {"image_id": image_id, "gesture": "control",
                "control": "orders.go_to", "x": 1, "y": 0}
        self.gateway.validate_gesture(p, base)
        invalid = [
            {**base, "control": "other"},
            {**base, "control": None},
            {**base, "orientation": "terminal_ne"},
            {**base, "button": "left"},
            {**base, "ctrl": False},
            {**base, "text": "x"},
            {**base, "end_x": 1, "end_y": 0},
            {**base, "x": True},
            {**base, "y": 1.5},
            {**base, "x": 2},
            {**base, "control": "road_stop.orientation"},
            {**base, "control": "road_stop.orientation",
             "orientation": "terminal_NE"},
            {**base, "control": "road_stop.orientation",
             "orientation": 1},
            {**base, "control": "road_stop.orientation",
             "orientation": "terminal_ne", "extra": 1},
        ]
        for index, params in enumerate(invalid):
            with self.subTest(index=index):
                self.assert_code("invalid_request", lambda: self.gateway.validate_gesture(p, params))
        self.assert_code("forbidden", lambda: self.gateway.validate_gesture(
            principal(company=2), base))

    def test_ui_control_rejects_command_bearing_capture_and_consumes_image(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        params = {"image_id": image_id, "gesture": "control",
                  "control": "orders.go_to", "x": 1, "y": 0}
        def command_bearing(bundle):
            bundle["commands"] = [{"command": 22, "data": "ab",
                                   "callback": 22, "error": 4,
                                   "company_id": 1}]
            bundle["state"] = {}
            bundle["tile_proof"] = {"count": 0, "digest": "a" * 64}
        self.fake1.bundle_override = command_bearing
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture_intent(
            p, "control-command", params, "world-instance", 300))
        self.assert_code("image_unavailable", lambda: self.gateway.validate_gesture(p, params))

    def test_move_intent_sends_only_cursor_coordinates(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        params = {"image_id": image_id, "gesture": "move", "x": 1, "y": 1}
        expected = self.gateway.prepare_intent(p, "world-instance", 300)
        bundle = self.gateway.capture_intent(p, "move-1", params,
                                             "world-instance", 300,
                                             expected_worker_instance_id=expected)
        self.assertEqual(bundle["commands"], [])
        sent = [c for c in self.fake1.calls if c["type"] == "ui.capture"][-1]["parameters"]
        self.assertEqual(sent, {**params, "request_id": "move-1",
                                "authority_instance_id": "world-instance",
                                "world_epoch": "initial"})

    def test_intent_bundle_binding_and_size_fail_closed(self):
        p = principal()
        cases = [lambda b: b.update({"authority_instance_id": "wrong"}),
                 lambda b: b.update({"worker_instance_id": "wrong"}),
                 lambda b: b.update({"source_frame": 18}),
                 lambda b: b.update({"owner": {"client_id": 5, "key": "secret"}}),
                 lambda b: b.update({"version": 1}),
                 lambda b: b.update({"tiles": []}),
                 lambda b: b.update({"state": {}}),
                 lambda b: b.update({"tile_proof": {"count": 0, "digest": "a" * 64}}),
                 lambda b: b.update({"commands": [{}, {}]}),
                 lambda b: b.update({"state": {"secret": "x" * 48000}})]
        for index, change in enumerate(cases):
            image_id = self.gateway.capture(p)["image_id"]
            self.fake1.bundle_override = change
            params = {"image_id": image_id, "gesture": "click", "x": 0, "y": 0}
            self.assert_code("renderer_unavailable", lambda index=index, params=params:
                             self.gateway.capture_intent(p, f"request-{index}", params,
                                                         "world-instance", 300))
            self.assert_code("image_unavailable", lambda params=params: self.gateway.validate_gesture(p, params))
            self.advance()

    def test_v2_gameplay_proof_shape_and_command_byte_boundary(self):
        p = principal()
        base = {"version": 2, "request_id": "request-1", "image_id": "a" * 32,
                "source_frame": 17, "authority_instance_id": "world-instance",
                "world_epoch": "initial", "worker_instance_id": "renderer-1",
                "owner": {"client_id": 5, "public_key": "key"},
                "commands": [{"command": 22, "data": "ab" * 8192,
                              "callback": 22, "error": 4, "company_id": 1}],
                "state": {}, "tile_proof": {"count": 1 << 24,
                                               "digest": "a" * 64}}
        def validate(bundle):
            return self.gateway._validate_bundle(bundle, p, "request-1", "a" * 32,
                                                 "renderer-1", ("world-instance", 300), 17)
        self.assertEqual(validate(base), base)
        zero = copy.deepcopy(base)
        zero["tile_proof"]["count"] = 0
        self.assertEqual(validate(zero), zero)
        # The gateway checks owner shape. Native CurrentOwner checks live owner identity.
        different_owner = copy.deepcopy(base)
        different_owner["owner"]["public_key"] = "other-valid-key"
        self.assertEqual(validate(different_owner), different_owner)
        changes = [
            lambda b: b.update(version=True),
            lambda b: b.pop("tile_proof"),
            lambda b: b.update(tile_proof=None),
            lambda b: b.update(tile_proof={"count": 1, "digest": "a" * 64,
                                           "algorithm": "blake2b"}),
            lambda b: b.update(tile_proof={"count": True, "digest": "a" * 64}),
            lambda b: b.update(tile_proof={"count": -1, "digest": "a" * 64}),
            lambda b: b.update(tile_proof={"count": (1 << 24) + 1,
                                           "digest": "a" * 64}),
            lambda b: b.update(tile_proof={"count": "1", "digest": "a" * 64}),
            lambda b: b.update(tile_proof={"count": 1, "digest": "A" * 64}),
            lambda b: b.update(tile_proof={"count": 1, "digest": "g" * 64}),
            lambda b: b.update(tile_proof={"count": 1, "digest": "a" * 63}),
            lambda b: b.update(tile_proof={"count": 1, "digest": "a" * 65}),
            lambda b: b.pop("state"),
            lambda b: b.update(state=None),
            lambda b: b["commands"][0].update(data="ab" * 8193),
            lambda b: b["commands"][0].update(data="abc"),
            lambda b: b["commands"][0].update(data="0G"),
            lambda b: b["commands"][0].update(company_id=2),
            lambda b: b.update(state={"text": "x" * 16384}),
            lambda b: b.update(tiles=[]),
        ]
        for index, change in enumerate(changes):
            candidate = copy.deepcopy(base)
            change(candidate)
            with self.subTest(index=index):
                self.assert_code("renderer_unavailable", lambda: validate(candidate))

    def test_v2_successful_status_rejects_legacy_bundle(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        self.gateway.capture_intent(p, "request-1", {"image_id": image_id,
                                     "gesture": "click", "x": 0, "y": 0},
                                    "world-instance", 300)
        self.fake1.bundle["version"] = 1
        self.assert_code("renderer_unavailable", lambda: self.gateway.intent_status(
            p, "request-1", expected_worker_instance_id="renderer-1"))

    def test_single_native_command_bundle_stays_private(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        native_data = "ABcd" * 8 + "12"  # 34 hex digits, as in the native road capture.
        proof = {"count": 1, "digest": "a" * 64}

        def add_command(bundle):
            bundle["commands"] = [{"command": 22, "data": native_data, "callback": 22,
                                   "error": 4, "company_id": 1}]
            bundle["state"] = {}
            bundle["tile_proof"] = proof

        self.fake1.bundle_override = add_command
        bundle = self.gateway.capture_intent(p, "request-1", {"image_id": image_id,
                                             "gesture": "drag", "x": 0, "y": 0,
                                             "end_x": 1, "end_y": 1}, "world-instance", 300)
        self.assertEqual(bundle["commands"][0]["data"], native_data)
        self.assertEqual(bundle["tile_proof"], proof)
        status = self.gateway.intent_status(p, "request-1")
        self.assertEqual(status["bundle"], bundle)
        self.assertEqual(status["bundle"]["commands"][0]["data"], native_data)

    def test_native_command_bundle_rejects_nonhex_without_rewriting(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]

        def malformed(bundle):
            bundle["commands"] = [{"command": 22, "data": "ABcd" * 8 + "1G",
                                   "callback": 22, "error": 4, "company_id": 1}]
            bundle["state"] = {}
            bundle["tile_proof"] = {"count": 1, "digest": "a" * 64}

        self.fake1.bundle_override = malformed
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture_intent(
            p, "request-1", {"image_id": image_id, "gesture": "click", "x": 0, "y": 0},
            "world-instance", 300))

    def test_intent_status_and_child_cleanup_are_bounded(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        self.gateway.capture_intent(p, "request-1", {"image_id": image_id,
                                    "gesture": "click", "x": 0, "y": 0}, "world-instance", 300)
        self.fake1.children = ["child-1"]
        status = self.gateway.intent_status(p, "request-1")
        self.gateway.clear_children(p, status)
        releases = [c["parameters"] for c in self.fake1.calls if c["type"] == "ui.release"]
        self.assertEqual(releases, [{"request_id": "child-1", "phase": "reject",
                                     "authority_rejected": True},
                                    {"request_id": "child-1", "phase": "ack"}])
        self.fake1.children = [f"child-{i}" for i in range(17)]
        self.assert_code("renderer_unavailable", lambda: self.gateway.intent_status(p, "request-1"))

    def test_prepare_intent_fences_worker_before_capture_status_and_release(self):
        p = principal()
        image_id = self.gateway.capture(p)["image_id"]
        expected = self.gateway.prepare_intent(p, "world-instance", 300)
        self.assertEqual(expected, "renderer-1")
        params = {"image_id": image_id, "gesture": "click", "x": 0, "y": 0}
        self.fake1.instance = "renderer-2"
        self.assert_code("renderer_unavailable", lambda: self.gateway.capture_intent(
            p, "request-1", params, "world-instance", 300,
            expected_worker_instance_id=expected))
        self.assert_code("renderer_unavailable", lambda: self.gateway.intent_status(
            p, "request-1", expected_worker_instance_id=expected))
        self.assert_code("renderer_unavailable", lambda: self.gateway.release_intent(
            p, "request-1", expected_worker_instance_id=expected))
        self.assertFalse(any(c["type"] in ("ui.capture", "ui.status", "ui.release")
                             for c in self.fake1.calls))

    def test_intent_status_recovers_unknown_and_typed_rejected_bundle(self):
        p = principal()
        expected = self.gateway.prepare_intent(p, "world-instance", 300)
        self.fake1.unknown_status = True
        self.assertEqual(self.gateway.intent_status(p, "request-1",
                                                    expected_worker_instance_id=expected),
                         {"phase": "unknown"})
        self.fake1.unknown_status = False
        self.fake1.phase = "rejected"
        self.fake1.bundle = {"error": {"code": "gesture_rejected",
                                       "message": "Rendering request unavailable"},
                             "request_id": "request-1"}
        status = self.gateway.intent_status(p, "request-1",
                                            expected_worker_instance_id=expected)
        self.assertEqual(status["phase"], "rejected")
        self.assertEqual(status["bundle"]["error"]["code"], "gesture_rejected")
        self.gateway.release_intent(p, "request-1", phase="ack",
                                    expected_worker_instance_id=expected)


if __name__ == "__main__":
    unittest.main()
