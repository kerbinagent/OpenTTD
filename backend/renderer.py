"""Authenticated UI image and private native-intent gateway for renderer workers."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import threading
import time
import zlib

from .errors import ServiceError
from .json_tools import dumps
from .observations import (
    FUTURE_SKEW_MS, MAX_REQUESTS, RETENTION_MS, REQUEST_ID,
    ObservationImage, capability, validate_request,
)


_IMAGE_ID = re.compile(r"[0-9a-f]{32}\Z")
_INSTANCE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_MAX_PNG = 16 * 1024 * 1024
_MAX_PIXELS = 16 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_ACTOR_FIELDS = ("identity_id", "company_id", "run_id", "mode")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HEX = re.compile(r"[0-9a-fA-F]*\Z")
_PROOF_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NATIVE_KEY = re.compile(r"[0-9a-fA-F]{64}\Z")
_KEY_FRESHNESS = 30.0
_MAX_GESTURE_TEXT_BYTES = 4096
_MAX_LATEST_IMAGE_AGE = 40.0


@dataclass
class _Worker:
    run_id: str
    engine: object
    artifacts: Path
    directory_fd: int
    key_id: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)
    actor: tuple | None = None
    instance: str | None = None
    bound_instance: str | None = None
    bound_authority: tuple | None = None
    latest: tuple | None = None  # image ID, capture time, width, height, source frame
    last_capture: float = float("-inf")
    controls: deque = field(default_factory=deque)
    observation_generation: str = field(default_factory=lambda: secrets.token_hex(16))
    observations: OrderedDict = field(default_factory=OrderedDict)
    observation_revision: int | None = None
    observation_version: int = 0


@dataclass
class _Observation:
    fingerprint: str
    request: dict
    observation_id: str
    deadline_ms: int
    generation: str
    instance: str
    barrier: tuple | None = None
    parameters: dict | None = None
    image_id: str | None = None
    width: int = 0
    height: int = 0
    source_frame: int | None = None
    lifecycle_revision: int | None = None
    capture_started: float | None = None
    native_started: float | None = None
    digest: str | None = None
    error: tuple | None = None


@dataclass(frozen=True)
class _Image:
    data: bytes
    actor: tuple
    instance: str
    expires: float


def _unavailable():
    return ServiceError("renderer_unavailable", status=503)


def _image_unavailable():
    return ServiceError("image_unavailable", status=404)


def _positive_int(value):
    return type(value) is int and value > 0


def _bounded_int(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _png_dimensions(data):
    """Verify the narrow metadata-free RGB8 PNG emitted by the native bridge."""
    if not isinstance(data, bytes) or not data.startswith(_PNG) or len(data) > _MAX_PNG:
        raise ValueError("invalid PNG")
    offset = len(_PNG)
    dimensions = None
    idat = bytearray()
    phase = "ihdr"
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + length
        if length > _MAX_PNG or end > len(data):
            raise ValueError("invalid PNG chunk")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:end - 4]
        crc = int.from_bytes(data[end - 4:end], "big")
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ValueError("invalid PNG CRC")
        if phase == "ihdr":
            if kind != b"IHDR" or length != 13:
                raise ValueError("invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if (not width or not height or width * height > _MAX_PIXELS or
                    (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0)):
                raise ValueError("unsupported PNG format")
            dimensions = (width, height)
            phase = "idat"
        elif phase == "idat":
            if kind == b"IDAT":
                idat.extend(payload)
            elif kind == b"IEND" and length == 0 and idat and end == len(data):
                break
            else:
                raise ValueError("unexpected PNG chunk")
        offset = end
    else:
        raise ValueError("incomplete PNG")
    width, height = dimensions
    expected = height * (1 + 3 * width)
    if expected > 64 * 1024 * 1024:
        raise ValueError("PNG too large")
    inflater = zlib.decompressobj()
    decoded = inflater.decompress(idat, expected + 1)
    if len(decoded) != expected or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise ValueError("invalid PNG pixels")
    stride = 1 + 3 * width
    if any(decoded[row * stride] > 4 for row in range(height)):
        raise ValueError("invalid PNG filter")
    return dimensions


class RendererGateway:
    """One immutable actor and one private artifact directory per renderer socket."""

    def __init__(self, workers, world_id, world_epoch="initial", *,
                 slots=None,
                 max_cache_bytes=32 * 1024 * 1024, max_images=64, ttl=60,
                 capture_interval=1.0, max_managed_worker_keys=1024,
                 clock=time.monotonic, wall_clock=time.time):
        if not isinstance(world_id, str) or not world_id or not isinstance(world_epoch, str) or not world_epoch:
            raise ValueError("world and epoch are required")
        if (not isinstance(workers, dict) or (slots is not None and not isinstance(slots, dict))
                or not isinstance(max_cache_bytes, int) or max_cache_bytes < 1 or not isinstance(max_images, int) or max_images < 1 or ttl <= 0):
            raise ValueError("invalid renderer configuration")
        slots = {} if slots is None else slots
        if len(slots) > 15 or (slots and len(workers) + len(slots) > 15):
            raise ValueError("at most 15 combined renderer workers are supported")
        if type(max_managed_worker_keys) is not int or not 1 <= max_managed_worker_keys <= 1024:
            raise ValueError("max_managed_worker_keys must be between 1 and 1024")
        try:
            interval = float(capture_interval) if type(capture_interval) in (int, float) else float("nan")
        except (OverflowError, ValueError):
            interval = float("nan")
        if not math.isfinite(interval) or not 1 <= interval <= 60:
            raise ValueError("capture_interval must be a finite 1..60 seconds")
        self.world_id = world_id
        self.world_epoch = world_epoch
        self.max_cache_bytes = max_cache_bytes
        self.max_images = max_images
        self.ttl = ttl
        # A control invalidates the reusable image; then callers wait for the
        # configured interval before another native capture (minimum 1 second).
        self.capture_interval = interval
        self.max_managed_worker_keys = max_managed_worker_keys
        self.clock = clock
        self.wall_clock = wall_clock
        self._observation_time_lock = threading.Lock()
        self._observation_time_highwater = 0
        self._observation_slots = threading.BoundedSemaphore(4)
        self._workers = {}
        self._slots = {}
        self._registry = {}
        self._registry_lock = threading.Lock()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = threading.RLock()
        self._authority_lock = threading.RLock()
        self._authority = None
        # This lock is never held across IPC or image reads. Participant
        # projection can take a quick memory snapshot while a worker is busy.
        self._cache_key_lock = threading.Lock()
        self._native_keys = {}
        self._managed_native_keys = set()
        self._managed_keys_overflow = False
        sockets = set()
        directories = set()
        directory_inodes = set()
        try:
            entries = [(False, run_id, config) for run_id, config in workers.items()]
            entries += [(True, slot_id, config) for slot_id, config in slots.items()]
            for pooled, identifier, config in entries:
                if (not isinstance(identifier, str) or not identifier or len(identifier) > 128
                        or (pooled and not _INSTANCE_ID.fullmatch(identifier))):
                    raise ValueError("invalid renderer run or slot")
                if isinstance(config, dict):
                    engine, artifacts = config["engine"], config["artifacts"]
                else:
                    engine, artifacts = config
                socket_path = os.path.realpath(os.fspath(engine.socket_path))
                directory = Path(os.path.abspath(os.fspath(artifacts)))
                if socket_path in sockets or directory in directories:
                    raise ValueError("renderer worker reused by another run")
                sockets.add(socket_path)
                directories.add(directory)
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("renderer artifact directory must be owner-only")
                inode = (info.st_dev, info.st_ino)
                if inode in directory_inodes:
                    raise ValueError("renderer artifact directory reused by another run")
                directory_inodes.add(inode)
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                fd = os.open(directory, flags)
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    os.close(fd)
                    raise ValueError("renderer artifact directory changed")
                key_id = ("slot:" if pooled else "run:") + identifier
                worker = _Worker("" if pooled else identifier, engine, directory, fd, key_id=key_id)
                self._registry[key_id] = worker
                if pooled:
                    self._slots[identifier] = worker
                else:
                    self._workers[identifier] = worker
        except Exception:
            self.close()
            raise

    def close(self):
        with self._cache_key_lock:
            self._native_keys.clear()
            self._managed_native_keys.clear()
            self._managed_keys_overflow = False
        for worker in self._registry.values():
            if worker.directory_fd >= 0:
                os.close(worker.directory_fd)
                worker.directory_fd = -1

    def available(self, principal):
        return principal.world_id == self.world_id and principal.run_id in self._workers

    def worker_slots(self):
        """List trusted dynamic capacity; no public request selects a slot."""
        return tuple(sorted(self._slots))

    def assign_slot(self, slot_id, principal):
        """Restore a durable once-only slot assignment without issuing native IPC."""
        worker = self._slots.get(slot_id)
        if (worker is None or principal.world_id != self.world_id
                or principal.mode != "visual" or principal.kind != "agent"
                or not isinstance(principal.run_id, str) or not principal.run_id):
            raise ServiceError("forbidden", status=403)
        actor = tuple(getattr(principal, field) for field in _ACTOR_FIELDS)
        with self._registry_lock:
            previous = self._workers.get(principal.run_id)
            if ((previous is not None and previous is not worker)
                    or (worker.actor is not None and worker.actor != actor)
                    or (worker.run_id and worker.run_id != principal.run_id)):
                raise ServiceError("forbidden", status=403)
            worker.actor = actor
            worker.run_id = principal.run_id
            # Readers retain a stable dictionary snapshot while new joins attach.
            self._workers = {**self._workers, principal.run_id: worker}

    def worker_key(self, slot_id):
        """Read one trusted slot's connected native identity, never a roster label."""
        worker = self._slots.get(slot_id)
        if worker is None:
            raise _unavailable()
        with worker.lock:
            instance = self._refresh(worker)
            with self._cache_key_lock:
                entry = self._native_keys.get(worker.key_id)
                if entry is None or entry[0] != instance:
                    raise _unavailable()
                return entry[1]

    def check_ready(self, principal):
        """Verify and bind only this run's connected renderer for fresh admission."""
        worker = self._worker(principal)
        with worker.lock:
            instance = self._refresh(worker)
            with self._cache_key_lock:
                entry = self._native_keys.get(worker.key_id)
                if entry is None or entry[0] != instance:
                    raise _unavailable()
            actor, _ = self._actor(principal, worker)
            self._bind(worker, actor, instance)

    def known_native_worker_keys(self):
        """Return only recently seen renderer client keys; never issue IPC."""
        now = self.clock()
        with self._cache_key_lock:
            for key_id, (instance, _, seen) in list(self._native_keys.items()):
                worker = self._registry.get(key_id)
                if (worker is None or worker.instance != instance or
                        now - seen > _KEY_FRESHNESS):
                    del self._native_keys[key_id]
            return {key for _, key, _ in self._native_keys.values()}

    def managed_native_worker_keys(self):
        """Return retained managed identities without relying on worker health."""
        with self._cache_key_lock:
            return set(self._managed_native_keys)

    def verified_native_worker_keys(self):
        """Refresh every configured worker; require complete current coverage."""
        with self._cache_key_lock:
            if self._managed_keys_overflow:
                raise _unavailable()
        for key_id, worker in sorted(self._registry.items()):
            with worker.lock:
                instance = self._refresh(worker)
                with self._cache_key_lock:
                    entry = self._native_keys.get(key_id)
                    if (self._managed_keys_overflow or entry is None or
                            entry[0] != instance):
                        raise _unavailable()
        with self._cache_key_lock:
            if self._managed_keys_overflow:
                raise _unavailable()
            return set(self._managed_native_keys)

    def _clear_native_key(self, worker):
        with self._cache_key_lock:
            self._native_keys.pop(worker.key_id, None)

    def configure_authority(self, instance_id, max_lag_ticks):
        """Set the trusted world's renderer lease fence, never from a public body."""
        if (not isinstance(instance_id, str) or not _INSTANCE_ID.fullmatch(instance_id) or
                not _bounded_int(max_lag_ticks, 1, 65535)):
            raise ValueError("invalid renderer authority configuration")
        with self._authority_lock:
            self._authority = (instance_id, max_lag_ticks)

    def _worker(self, principal, *, image=False):
        if not self.available(principal) or principal.mode not in ("text", "visual"):
            raise _image_unavailable() if image else ServiceError("forbidden", status=403)
        return self._workers[principal.run_id]

    def _actor(self, principal, worker):
        actor = tuple(getattr(principal, name) for name in _ACTOR_FIELDS)
        if worker.actor is None:
            worker.actor = actor
        elif worker.actor != actor:
            raise ServiceError("forbidden", status=403)
        return dict(zip(_ACTOR_FIELDS, actor)), actor

    def _rpc(self, worker, rpc_type, actor=None, *, allow_unknown=False,
             observation=False, **fields):
        message = {"protocol_version": 1, "type": rpc_type,
                   "correlation_id": secrets.token_hex(16),
                   "world_id": self.world_id, "world_epoch": self.world_epoch}
        if actor is not None:
            message["actor"] = actor
        message.update(fields)
        try:
            reply = worker.engine.call(message)
        except Exception:
            self._clear_native_key(worker)
            raise _unavailable() from None
        if (not isinstance(reply, dict) or
                reply.get("world_id", self.world_id) != self.world_id or
                reply.get("world_epoch", self.world_epoch) != self.world_epoch):
            self._clear_native_key(worker)
            raise _unavailable()
        expected_instance = message.get("engine_instance_id")
        if (expected_instance is not None and
                reply.get("engine_instance_id", expected_instance) != expected_instance):
            self._clear_native_key(worker)
            raise _unavailable()
        if (allow_unknown and reply.get("status") == "rejected" and
                isinstance(reply.get("error"), dict) and
                reply["error"].get("code") == "unknown_request" and
                reply.get("result") == {}):
            return reply
        if observation and reply.get("status") == "rejected":
            error = reply.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code in ("replica_pending", "renderer_busy", "rate_limited", "observation_expired",
                        "observation_source_expired", "invalid_request"):
                return reply
        if reply.get("status") not in ("completed", "ok"):
            self._clear_native_key(worker)
            raise _unavailable()
        return reply

    def _refresh(self, worker):
        reply = self._rpc(worker, "hello")
        result = reply.get("result")
        if not isinstance(result, dict) or result.get("role") != "renderer":
            self._clear_native_key(worker)
            raise _unavailable()
        instance = result.get("engine_instance_id", reply.get("engine_instance_id"))
        if (not isinstance(instance, str) or not instance or
                reply.get("engine_instance_id", instance) != instance or
                result.get("world_id", self.world_id) != self.world_id or
                result.get("world_epoch", self.world_epoch) != self.world_epoch):
            self._clear_native_key(worker)
            raise _unavailable()
        with self._cache_lock:
            if worker.instance != instance:
                previous_instance = worker.instance
                self._clear_native_key(worker)
                self._drop_run_locked(worker)
                worker.instance = instance
                worker.bound_instance = None
                worker.bound_authority = None
                worker.latest = None
                if previous_instance is not None:
                    worker.observation_generation = secrets.token_hex(16)
                    worker.observations.clear()
                    worker.observation_revision = None
        worker.observation_version = result.get("observation_version", 0)
        native_key = result.get("native_public_key")
        if isinstance(native_key, str) and _NATIVE_KEY.fullmatch(native_key):
            with self._cache_key_lock:
                canonical_key = native_key.lower()
                if any(key_id != worker.key_id and entry[1] == canonical_key
                       for key_id, entry in self._native_keys.items()):
                    self._native_keys.pop(worker.key_id, None)
                    raise _unavailable()
                self._native_keys[worker.key_id] = (instance, canonical_key, self.clock())
                if canonical_key not in self._managed_native_keys:
                    if len(self._managed_native_keys) >= self.max_managed_worker_keys:
                        self._managed_keys_overflow = True
                    else:
                        self._managed_native_keys.add(canonical_key)
        else:
            # Empty is normal before a renderer joins the world. Malformed or
            # missing keys never become participant-filter identifiers.
            self._clear_native_key(worker)
        return instance

    def _bind(self, worker, actor, instance):
        with self._authority_lock:
            authority = self._authority
        if worker.bound_instance == instance:
            if worker.bound_authority == authority:
                return
            if worker.bound_authority is not None:
                # Native renderer authority bindings cannot change in place.
                raise _unavailable()
        fields = {"engine_instance_id": instance}
        if authority is not None:
            fields["authority_instance_id"] = authority[0]
            fields["authority_max_lag_ticks"] = authority[1]
        reply = self._rpc(worker, "session.bind", actor, **fields)
        if reply.get("engine_instance_id", instance) != instance:
            raise _unavailable()
        worker.bound_instance = instance
        worker.bound_authority = authority

    def _drop_run_locked(self, worker):
        for key in list(self._cache):
            if self._workers.get(key[0]) is worker:
                self._remove(key)

    def _remove(self, key):
        entry = self._cache.pop(key)
        self._cache_bytes -= len(entry.data)

    def _prune(self, now):
        for key, entry in list(self._cache.items()):
            if entry.expires <= now:
                self._remove(key)

    def _cache_image(self, run_id, image_id, image, actor, instance, now, *, protect_input=False):
        if len(image) > self.max_cache_bytes:
            raise _unavailable()
        per_run_images = max(1, min(16, self.max_images))
        per_run_bytes = min(self.max_cache_bytes, _MAX_PNG * 2)
        with self._cache_lock:
            self._prune(now)
            protected = ({(worker.run_id, worker.latest[0])
                          for worker in self._workers.values()
                          if worker.latest is not None and
                          now - worker.latest[1] <= _MAX_LATEST_IMAGE_AGE}
                         if protect_input else set())
            key = (run_id, image_id)
            if key in self._cache:
                raise _unavailable()
            while self._cache and (sum(k[0] == run_id for k in self._cache) >= per_run_images or
                   sum(len(v.data) for k, v in self._cache.items() if k[0] == run_id) + len(image) > per_run_bytes or
                   len(self._cache) >= self.max_images or self._cache_bytes + len(image) > self.max_cache_bytes):
                run_keys = [k for k in self._cache if k[0] == run_id and k not in protected]
                candidates = [k for k in self._cache if k not in protected]
                if not candidates:
                    raise _unavailable()
                run_overflow = (sum(k[0] == run_id for k in self._cache) >= per_run_images or
                                sum(len(v.data) for k, v in self._cache.items() if k[0] == run_id) +
                                len(image) > per_run_bytes)
                if run_overflow and not run_keys:
                    raise _unavailable()
                victim = run_keys[0] if run_overflow else candidates[0]
                self._remove(victim)
            self._cache[key] = _Image(image, actor, instance, now + self.ttl)
            self._cache_bytes += len(image)

    def _read_artifact(self, worker, path, image_id, *, prefix="frame"):
        if prefix not in ("frame", "observation"):
            raise _unavailable()
        expected = worker.artifacts / (prefix + "-" + image_id + ".png")
        if not isinstance(path, str) or Path(path) != expected:
            raise _unavailable()
        try:
            current = worker.artifacts.lstat()
            pinned = os.fstat(worker.directory_fd)
            if (not stat.S_ISDIR(current.st_mode) or
                    (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)):
                raise _unavailable()
            fd = os.open(expected.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=worker.directory_fd)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        info.st_nlink != 1 or info.st_mode & 0o077 or
                        info.st_size < 1 or info.st_size > _MAX_PNG):
                    raise _unavailable()
                data = source.read(_MAX_PNG + 1)
                if len(data) != info.st_size:
                    raise _unavailable()
                return data
        except (OSError, ValueError):
            raise _unavailable() from None

    def capture(self, principal):
        worker = self._worker(principal)
        with worker.lock:
            actor_dict, actor_key = self._actor(principal, worker)
            instance = self._refresh(worker)
            now = self.clock()
            self._bind(worker, actor_dict, instance)
            if now - worker.last_capture < self.capture_interval:
                latest = worker.latest
                if latest is not None:
                    with self._cache_lock:
                        self._prune(now)
                        entry = self._cache.get((principal.run_id, latest[0]))
                        if (entry is not None and entry.actor == actor_key and
                                entry.instance == instance and entry.expires > now):
                            return {"image_id": latest[0], "width": latest[2], "height": latest[3]}
                raise ServiceError("rate_limited", status=429)
            reply = self._rpc(worker, "image.request", actor_dict,
                              engine_instance_id=instance, parameters={})
            result = reply.get("result")
            if not isinstance(result, dict):
                raise _unavailable()
            image_id = result.get("image_id")
            width, height = result.get("width"), result.get("height")
            source_frame = result.get("source_frame")
            if (not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id) or
                    result.get("screenshot_id") != image_id or
                    result.get("content_type") != "image/png" or
                    not _positive_int(width) or not _positive_int(height) or
                    not _bounded_int(result.get("tick"), 0, 2**63 - 1) or
                    source_frame is not None and not _bounded_int(source_frame, 0, 2**32 - 1) or
                    reply.get("engine_instance_id", instance) != instance):
                raise _unavailable()
            data = self._read_artifact(worker, result.get("path"), image_id)
            try:
                dimensions = _png_dimensions(data)
            except (ValueError, zlib.error):
                raise _unavailable() from None
            if dimensions != (width, height):
                raise _unavailable()
            with self._cache_lock:
                self._prune(now)
                existing = self._cache.get((principal.run_id, image_id))
                if existing is not None:
                    if (worker.latest is None or worker.latest[0] != image_id or
                            existing.actor != actor_key or existing.instance != instance or
                            existing.data != data or (worker.latest[2], worker.latest[3]) != (width, height) or
                            worker.latest[4] != source_frame):
                        raise _unavailable()
                    # A parked native renderer may repeat its own image ID.
                    # Its original age and cache expiry remain authoritative.
                    worker.last_capture = now
                    return {"image_id": image_id, "width": width, "height": height}
                if worker.latest is not None and worker.latest[0] == image_id:
                    # The original image aged out or was evicted. A repeated
                    # native ID must never acquire a fresh gesture lifetime.
                    raise _unavailable()
            # Publish the input image and its protection marker together.
            # Another worker's observation may evict cache entries under this
            # lock, so it must never see a new input image without `latest`.
            with self._cache_lock:
                self._cache_image(principal.run_id, image_id, data, actor_key, instance, now)
                worker.latest = (image_id, now, width, height, source_frame)
            worker.last_capture = now
            return {"image_id": image_id, "width": width, "height": height}

    def image(self, principal, image_id):
        worker = self._worker(principal, image=True)
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise _image_unavailable()
        actor = tuple(getattr(principal, name) for name in _ACTOR_FIELDS)
        with self._cache_lock:
            self._prune(self.clock())
            entry = self._cache.get((principal.run_id, image_id))
            if entry is None or entry.actor != actor or entry.instance != worker.instance:
                raise _image_unavailable()
            self._cache.move_to_end((principal.run_id, image_id))
            return entry.data

    def observation_capabilities(self, principal):
        worker = self._worker(principal)
        if not worker.lock.acquire(blocking=False):
            raise _unavailable()
        try:
            self._actor(principal, worker)
            self._refresh(worker)
            if type(worker.observation_version) is not int or worker.observation_version != 1:
                raise _unavailable()
            return capability(worker.observation_generation)
        finally:
            worker.lock.release()

    def _observation_wall_ms(self):
        # A clock correction cannot revive an expired identity in this gateway.
        with self._observation_time_lock:
            self._observation_time_highwater = max(
                self._observation_time_highwater, int(self.wall_clock() * 1000))
            return self._observation_time_highwater

    @staticmethod
    def _pending_observation(request_id, reason):
        return {"status": "pending", "observation_request_id": request_id,
                "reason": reason, "retry_after_ms": 100}

    def _observation_record(self, principal, worker, request, fingerprint):
        actor, actor_key = self._actor(principal, worker)
        instance = self._refresh(worker)
        if type(worker.observation_version) is not int or worker.observation_version != 1:
            raise _unavailable()
        now_ms = self._observation_wall_ms()
        issued_ms = int(REQUEST_ID.fullmatch(request["observation_request_id"]).group(1))
        if (request["observation_generation"] != worker.observation_generation or
                now_ms >= issued_ms + RETENTION_MS):
            raise ServiceError("observation_expired", status=410)
        if issued_ms > now_ms + FUTURE_SKEW_MS:
            raise ServiceError("invalid_request", "Observation clock is ahead of the server")
        for key, entry in list(worker.observations.items()):
            if entry.deadline_ms <= now_ms:
                del worker.observations[key]
        request_id = request["observation_request_id"]
        record = worker.observations.get(request_id)
        if record is None:
            if len(worker.observations) >= MAX_REQUESTS:
                return actor, actor_key, None
            record = _Observation(fingerprint, request, secrets.token_hex(16),
                                  issued_ms + RETENTION_MS,
                                  worker.observation_generation, instance)
            worker.observations[request_id] = record
        elif record.fingerprint != fingerprint:
            raise ServiceError("observation_id_conflict", status=409)
        return actor, actor_key, record

    def _observation_result(self, principal, worker, record, actor_key, kind, *, reused):
        with self._cache_lock:
            self._prune(self.clock())
            entry = self._cache.get((principal.run_id, record.image_id))
            if (entry is None or entry.actor != actor_key or
                    entry.instance != record.instance):
                # Keep the request fingerprint until its timestamp expires.
                # Eviction must never turn an exact retry into another render.
                raise ServiceError("observation_expired", status=410)
            data = entry.data
        return ObservationImage(
            data=data, observation_id=record.observation_id,
            image_id=record.image_id, generation=record.generation,
            kind=kind, width=record.width, height=record.height,
            causal_state="satisfied" if "after_request_id" in record.request else "none",
            reused=reused, age_ms=max(0, int((self.clock() - record.capture_started) * 1000)),
        )

    def _observation_parameters(self, worker, record, kind):
        request = record.request
        if kind == "world":
            selection = dict(request["selection"])
            if selection["kind"] == "map_image_point":
                source_id = selection.pop("observation_id")
                source = next((value for value in worker.observations.values()
                               if value.observation_id == source_id), None)
                if (source is None or source.image_id is None or source.error is not None or
                        "selection" not in source.request or
                        source.generation != record.generation or
                        source.instance != record.instance or
                        source.lifecycle_revision != worker.observation_revision or
                        source.deadline_ms <= self._observation_wall_ms()):
                    raise ServiceError("observation_expired", status=410)
                pixel = selection["pixel"]
                if pixel["x"] >= source.width or pixel["y"] >= source.height:
                    raise ServiceError("invalid_request")
                with self._cache_lock:
                    self._prune(self.clock())
                    if (worker.run_id, source.image_id) not in self._cache:
                        raise ServiceError("observation_expired", status=410)
                selection["image_id"] = source.image_id
            params = {"selection": selection, "output": request["output"],
                      "labels": request["labels"]}
        else:
            params = {"panel": request["panel"], "presentation": request["presentation"],
                      "region": request["region"]}
        params["observation_request_id"] = record.observation_id
        params["authority_instance_id"] = record.barrier[0]
        if record.barrier[1] is not None:
            params["minimum_source_frame"] = record.barrier[1]
        return params

    def _render_observation(self, principal, worker, record, actor, actor_key, kind):
        if (record.native_started is not None and
                self.clock() - record.native_started >= RETENTION_MS / 1000):
            # Future clock skew must not extend an uncertain native token past
            # its own deduplication lifetime and accidentally draw it again.
            record.error = ("observation_expired", 410)
            raise ServiceError("observation_expired", status=410)
        if record.parameters is None:
            record.parameters = self._observation_parameters(worker, record, kind)
        self._bind(worker, actor, record.instance)
        if record.capture_started is None:
            record.capture_started = self.clock()
        if record.native_started is None:
            record.native_started = self.clock()
        reply = self._rpc(worker, "observation." + kind, actor,
                          observation=True, engine_instance_id=record.instance,
                          parameters=record.parameters)
        if reply.get("status") == "rejected":
            code = reply["error"]["code"]
            if code in ("replica_pending", "renderer_busy", "rate_limited"):
                record.capture_started = None
                return code
            code, status = (("invalid_request", 400) if code == "invalid_request"
                            else ("observation_expired", 410))
            record.error = (code, status)
            raise ServiceError(code, status=status)
        result = reply.get("result")
        required = {"image_id", "path", "width", "height", "content_type", "source_frame",
                    "authority_instance_id", "world_epoch", "worker_instance_id",
                    "renderer_lifecycle_revision", "capture_kind"}
        if (not isinstance(result, dict) or not required <= set(result) or
                not isinstance(result["image_id"], str) or not _IMAGE_ID.fullmatch(result["image_id"]) or
                result["content_type"] != "image/png" or result["capture_kind"] != kind or
                result["world_epoch"] != self.world_epoch or
                result["worker_instance_id"] != record.instance or
                result["authority_instance_id"] != record.barrier[0] or
                not _bounded_int(result["source_frame"], 0, 2**32 - 1) or
                record.barrier[1] is not None and result["source_frame"] < record.barrier[1] or
                not _bounded_int(result["renderer_lifecycle_revision"], 0, 2**64 - 1) or
                not _bounded_int(result["width"], 1, 1280) or
                not _bounded_int(result["height"], 1, 1024) or
                result["width"] * result["height"] > 1048576):
            raise _unavailable()
        if kind == "world" and (result["width"], result["height"]) != (
                record.request["output"]["width"], record.request["output"]["height"]):
            raise _unavailable()
        data = self._read_artifact(worker, result["path"], result["image_id"], prefix="observation")
        try:
            dimensions = _png_dimensions(data)
        except (ValueError, zlib.error):
            raise _unavailable() from None
        if dimensions != (result["width"], result["height"]):
            raise _unavailable()
        self._cache_image(principal.run_id, result["image_id"], data, actor_key,
                          record.instance, self.clock(), protect_input=True)
        record.image_id = result["image_id"]
        record.width, record.height = dimensions
        record.source_frame = result["source_frame"]
        record.lifecycle_revision = result["renderer_lifecycle_revision"]
        record.digest = hashlib.sha256(data).hexdigest()
        worker.observation_revision = record.lifecycle_revision
        return self._observation_result(principal, worker, record, actor_key, kind, reused=False)

    def observe(self, principal, kind, body, resolve_barrier):
        """Return immutable observation pixels without acquiring an input lease.

        Retry admission is bounded by the timestamp in the caller's identity.
        Waiting happens between IPC attempts, with the worker lock released so
        native replay and independently admitted actions can make progress.
        """
        request = validate_request(kind, body)
        worker = self._worker(principal)
        fingerprint = hashlib.sha256((kind + "\n" + dumps(body)).encode("utf-8")).hexdigest()
        request_id = request["observation_request_id"]
        if not self._observation_slots.acquire(blocking=False):
            return self._pending_observation(request_id, "renderer_busy")
        deadline = time.monotonic() + request["wait_ms"] / 1000
        resolved_barrier = None
        try:
            while True:
                reason, needs_barrier, record = "renderer_busy", False, None
                if worker.lock.acquire(blocking=False):
                    try:
                        actor, actor_key, record = self._observation_record(
                            principal, worker, request, fingerprint)
                        if record is not None:
                            if record.error is not None:
                                raise ServiceError(record.error[0], status=record.error[1])
                            if record.image_id is not None:
                                return self._observation_result(
                                    principal, worker, record, actor_key, kind, reused=True)
                            if record.barrier is None:
                                if resolved_barrier is None:
                                    needs_barrier = True
                                else:
                                    record.barrier = resolved_barrier
                            if record.barrier is not None:
                                value = self._render_observation(
                                    principal, worker, record, actor, actor_key, kind)
                                if isinstance(value, ObservationImage):
                                    return value
                                reason = value
                    finally:
                        worker.lock.release()
                if needs_barrier:
                    barrier = resolve_barrier(request.get("after_request_id"))
                    if barrier is not None:
                        resolved_barrier = barrier
                        # The first render attempt is allowed even with wait_ms=0.
                        continue
                    reason = "action_pending"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._pending_observation(request_id, reason)
                time.sleep(min(0.1, remaining))
        finally:
            self._observation_slots.release()

    def control(self, principal, body):
        worker = self._worker(principal)
        with worker.lock:
            actor_dict, _ = self._actor(principal, worker)
            instance = self._refresh(worker)
            now = self.clock()
            while worker.controls and worker.controls[0] <= now - 1:
                worker.controls.popleft()
            if len(worker.controls) >= 16:
                raise ServiceError("rate_limited", status=429)
            worker.controls.append(now)
            if not isinstance(body, dict) or not isinstance(body.get("image_id"), str):
                raise ServiceError("invalid_request")
            image_id = body["image_id"]
            latest = worker.latest
            if (not _IMAGE_ID.fullmatch(image_id) or latest is None or
                    latest[0] != image_id or now - latest[1] > _MAX_LATEST_IMAGE_AGE):
                raise _image_unavailable()
            with self._cache_lock:
                self._prune(now)
                if (principal.run_id, image_id) not in self._cache:
                    raise _image_unavailable()
            width, height = latest[2:4]
            action = body.get("action")
            if action == "pan":
                allowed = {"image_id", "action", "dx", "dy"}
                if set(body) != allowed or not _bounded_int(body["dx"], -width, width) or not _bounded_int(body["dy"], -height, height):
                    raise ServiceError("invalid_request")
                rpc_type, params = "view.control", {"action": "pan", "dx": body["dx"], "dy": body["dy"]}
            elif action == "zoom":
                if set(body) != {"image_id", "action", "steps"} or not _bounded_int(body["steps"], -3, 3):
                    raise ServiceError("invalid_request")
                rpc_type, params = "view.control", {"action": "zoom", "steps": body["steps"]}
            elif action == "inspect":
                if (set(body) != {"image_id", "action", "x", "y"} or
                        not _bounded_int(body["x"], 0, width - 1) or
                        not _bounded_int(body["y"], 0, height - 1)):
                    raise ServiceError("invalid_request")
                rpc_type, params = "view.control", {"action": "inspect", "x": body["x"], "y": body["y"], "screenshot_id": image_id}
            elif action == "open_window":
                window = body.get("window")
                if window not in ("company", "finances", "stations", "vehicles",
                                  "road_construction", "rail_construction", "water_construction",
                                  "airport_construction", "landscaping", "trees"):
                    raise ServiceError("invalid_request")
                allowed = {"image_id", "action", "window"}
                params = {"window": window}
                if window == "vehicles":
                    allowed.add("vehicle_type")
                    if body.get("vehicle_type") not in ("rail", "road", "ship", "air"):
                        raise ServiceError("invalid_request")
                    params["vehicle_type"] = body["vehicle_type"]
                if set(body) != allowed:
                    raise ServiceError("invalid_request")
                rpc_type = "window.open"
            elif action == "close_windows":
                if set(body) != {"image_id", "action"}:
                    raise ServiceError("invalid_request")
                rpc_type, params = "view.control", {"action": "close_windows"}
            else:
                raise ServiceError("invalid_request")
            self._bind(worker, actor_dict, instance)
            reply = self._rpc(worker, rpc_type, actor_dict,
                              engine_instance_id=instance, parameters=params)
            if reply.get("engine_instance_id", instance) != instance:
                raise _unavailable()
            worker.latest = None
            return {"status": "completed"}

    def _validate_gesture_locked(self, principal, worker, params):
        if not isinstance(params, dict):
            raise ServiceError("invalid_request")
        gesture = params.get("gesture")
        base = {"image_id", "gesture", "x", "y"}
        if gesture == "drag":
            required = base | {"end_x", "end_y"}
            allowed = required | {"button", "ctrl"}
        elif gesture == "click":
            required = base
            allowed = base | {"button", "ctrl"}
        elif gesture == "move":
            required = allowed = base
        elif gesture == "text":
            required = allowed = base | {"text"}
        elif gesture == "control":
            control = params.get("control")
            if control == "orders.go_to":
                required = allowed = base | {"control"}
            elif control == "road_stop.orientation":
                required = allowed = base | {"control", "orientation"}
                if params.get("orientation") not in (
                        "terminal_ne", "terminal_se", "terminal_sw",
                        "terminal_nw", "drive_through_ne_sw",
                        "drive_through_se_nw"):
                    raise ServiceError("invalid_request")
            else:
                raise ServiceError("invalid_request")
        else:
            raise ServiceError("invalid_request")
        if not required <= set(params) or set(params) - allowed:
            raise ServiceError("invalid_request")
        if gesture in ("click", "drag"):
            button = params.get("button", "left")
            if (button not in ("left", "right") or
                    gesture == "drag" and button != "left" or
                    "ctrl" in params and type(params["ctrl"]) is not bool):
                raise ServiceError("invalid_request")
        if gesture == "text":
            value = params["text"]
            if type(value) is not str:
                raise ServiceError("invalid_request")
            try:
                encoded = value.encode("utf-8")
            except UnicodeError:
                raise ServiceError("invalid_request") from None
            if len(encoded) > _MAX_GESTURE_TEXT_BYTES or any(
                    ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or
                    0xE000 <= ord(char) <= 0xE2FF or char in "\u2028\u2029"
                    for char in value):
                raise ServiceError("invalid_request")
        image_id = params["image_id"]
        latest = worker.latest
        now = self.clock()
        if (not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id) or
                latest is None or latest[0] != image_id or now - latest[1] > _MAX_LATEST_IMAGE_AGE):
            raise _image_unavailable()
        width, height = latest[2:4]
        if (not _bounded_int(params["x"], 0, width - 1) or
                not _bounded_int(params["y"], 0, height - 1) or
                gesture == "control" and (params["x"] > 4095 or params["y"] > 4095) or
                gesture == "drag" and (not _bounded_int(params["end_x"], 0, width - 1) or
                                       not _bounded_int(params["end_y"], 0, height - 1))):
            raise ServiceError("invalid_request")
        actor = tuple(getattr(principal, name) for name in _ACTOR_FIELDS)
        with self._cache_lock:
            self._prune(now)
            entry = self._cache.get((principal.run_id, image_id))
            if entry is None or entry.actor != actor or entry.instance != worker.instance:
                raise _image_unavailable()

    def validate_gesture(self, principal, params):
        """Check an owned latest image and pixel bounds locally, without IPC."""
        worker = self._worker(principal, image=True)
        if principal.mode != "visual":
            raise ServiceError("forbidden", status=403)
        with worker.lock:
            self._actor(principal, worker)
            self._validate_gesture_locked(principal, worker, params)

    def _authority_for_intent(self):
        with self._authority_lock:
            authority = self._authority
        if authority is None:
            raise _unavailable()
        return authority

    @staticmethod
    def _expect_worker(instance, expected_worker_instance_id):
        if expected_worker_instance_id is not None and (
                not isinstance(expected_worker_instance_id, str) or
                not _INSTANCE_ID.fullmatch(expected_worker_instance_id) or
                instance != expected_worker_instance_id):
            raise _unavailable()

    def prepare_intent(self, principal, authority_instance_id, max_lag_ticks):
        """Fence the worker before the broker persists its capture marker."""
        worker = self._worker(principal)
        if principal.mode != "visual":
            raise ServiceError("forbidden", status=403)
        with worker.lock:
            actor_dict, _ = self._actor(principal, worker)
            self.configure_authority(authority_instance_id, max_lag_ticks)
            instance = self._refresh(worker)
            self._bind(worker, actor_dict, instance)
            return instance

    def _validate_bundle(self, bundle, principal, request_id, image_id, instance, authority,
                         source_frame=None):
        required = {"version", "request_id", "image_id", "source_frame", "authority_instance_id",
                    "world_epoch", "worker_instance_id", "owner", "commands", "tile_proof"}
        if not isinstance(bundle, dict) or not required <= set(bundle) or set(bundle) - (required | {"state"}):
            raise _unavailable()
        try:
            size = len(json.dumps(bundle, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            raise _unavailable() from None
        if (size > 48000 or type(bundle["version"]) is not int or bundle["version"] != 2 or
                bundle["request_id"] != request_id or bundle["image_id"] != image_id or
                not _bounded_int(bundle["source_frame"], 0, 2**32 - 1) or
                source_frame is not None and bundle["source_frame"] != source_frame or
                bundle["authority_instance_id"] != authority[0] or
                bundle["world_epoch"] != self.world_epoch or
                bundle["worker_instance_id"] != instance):
            raise _unavailable()
        owner = bundle["owner"]
        if (not isinstance(owner, dict) or set(owner) != {"client_id", "public_key"} or
                not _bounded_int(owner["client_id"], 2, 2**32 - 1) or
                not isinstance(owner["public_key"], str) or
                not 1 <= len(owner["public_key"]) <= 128):
            raise _unavailable()
        commands, proof = bundle["commands"], bundle["tile_proof"]
        if not isinstance(commands, list) or len(commands) > 1:
            raise _unavailable()
        if commands:
            command = commands[0]
            if (not isinstance(command, dict) or
                    set(command) != {"command", "data", "callback", "error", "company_id"} or
                    not _bounded_int(command["command"], 0, 65535) or
                    not _bounded_int(command["callback"], 0, 255) or
                    not _bounded_int(command["error"], 0, 65535) or
                    not _bounded_int(command["company_id"], 0, 254) or
                    command["company_id"] != principal.company_id or
                    not isinstance(command["data"], str) or len(command["data"]) > 16384 or
                    len(command["data"]) % 2 or not _HEX.fullmatch(command["data"]) or
                    not isinstance(bundle.get("state"), dict)):
                raise _unavailable()
            if (not isinstance(proof, dict) or set(proof) != {"count", "digest"} or
                    not _bounded_int(proof["count"], 0, 1 << 24) or
                    not isinstance(proof["digest"], str) or
                    not _PROOF_DIGEST.fullmatch(proof["digest"])):
                raise _unavailable()
        elif "state" in bundle or proof is not None:
            raise _unavailable()
        if "state" in bundle:
            try:
                state_size = len(json.dumps(bundle["state"], separators=(",", ":"),
                                            ensure_ascii=False, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, RecursionError):
                raise _unavailable() from None
            if state_size > 16384:
                raise _unavailable()
        return bundle

    def capture_intent(self, principal, request_id, params, authority_instance_id, max_lag_ticks,
                       *, expected_worker_instance_id=None):
        """Send one native UI capture after the broker has persisted its marker."""
        worker = self._worker(principal)
        if principal.mode != "visual" or not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ServiceError("invalid_request")
        with worker.lock:
            actor_dict, _ = self._actor(principal, worker)
            self.configure_authority(authority_instance_id, max_lag_ticks)
            instance = self._refresh(worker)
            self._expect_worker(instance, expected_worker_instance_id)
            self._bind(worker, actor_dict, instance)
            self._validate_gesture_locked(principal, worker, params)
            source_frame = worker.latest[4]
            payload = dict(params)
            payload.update({"request_id": request_id, "authority_instance_id": authority_instance_id,
                            "world_epoch": self.world_epoch})
            # A lost reply is uncertain: neither this method nor its caller may reclick.
            try:
                reply = self._rpc(worker, "ui.capture", actor_dict,
                                  engine_instance_id=instance, parameters=payload)
            finally:
                worker.latest = None
            if reply.get("engine_instance_id", instance) != instance:
                raise _unavailable()
            bundle = self._validate_bundle(reply.get("result"), principal, request_id,
                                           params["image_id"], instance,
                                           self._authority_for_intent(), source_frame)
            if params["gesture"] == "control" and bundle["commands"]:
                raise _unavailable()
            return bundle

    def intent_status(self, principal, request_id, *, expected_worker_instance_id=None):
        worker = self._worker(principal)
        if principal.mode != "visual" or not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ServiceError("invalid_request")
        with worker.lock:
            actor_dict, _ = self._actor(principal, worker)
            instance = self._refresh(worker)
            self._expect_worker(instance, expected_worker_instance_id)
            self._bind(worker, actor_dict, instance)
            authority = self._authority_for_intent()
            reply = self._rpc(worker, "ui.status", actor_dict, engine_instance_id=instance,
                              parameters={"request_id": request_id}, allow_unknown=True)
            if reply.get("engine_instance_id", instance) != instance:
                raise _unavailable()
            if reply["status"] == "rejected":
                return {"phase": "unknown"}
            result = reply.get("result")
            if (reply.get("engine_instance_id", instance) != instance or not isinstance(result, dict) or
                    set(result) != {"bundle", "pending", "phase", "callback_rejected", "children"} or
                    type(result["pending"]) is not bool or type(result["callback_rejected"]) is not bool or
                    result["phase"] not in ("captured", "committed", "completed", "rejected") or
                    not isinstance(result["children"], list) or len(result["children"]) > 16):
                raise _unavailable()
            if result["phase"] == "rejected" and isinstance(result["bundle"], dict) and "error" in result["bundle"]:
                bundle = result["bundle"]
                error = bundle.get("error")
                if (set(bundle) != {"error", "request_id"} or bundle["request_id"] != request_id or
                        not isinstance(error, dict) or set(error) != {"code", "message"} or
                        error["code"] != "gesture_rejected" or not isinstance(error["message"], str) or
                        len(error["message"]) > 256 or result["pending"] or
                        result["callback_rejected"] or result["children"]):
                    raise _unavailable()
                return result
            image_id = result["bundle"].get("image_id") if isinstance(result["bundle"], dict) else None
            if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
                raise _unavailable()
            self._validate_bundle(result["bundle"], principal, request_id, image_id, instance, authority)
            for child in result["children"]:
                child_id = child.get("request_id") if isinstance(child, dict) else child
                if not isinstance(child_id, str) or not _REQUEST_ID.fullmatch(child_id):
                    raise _unavailable()
            return result

    def release_intent(self, principal, request_id, phase="commit", authority_rejected=False,
                       *, expected_worker_instance_id=None):
        worker = self._worker(principal)
        if (principal.mode != "visual" or not isinstance(request_id, str) or
                not _REQUEST_ID.fullmatch(request_id) or phase not in ("commit", "reject", "ack") or
                type(authority_rejected) is not bool or authority_rejected and phase != "reject"):
            raise ServiceError("invalid_request")
        with worker.lock:
            actor_dict, _ = self._actor(principal, worker)
            instance = self._refresh(worker)
            self._expect_worker(instance, expected_worker_instance_id)
            self._bind(worker, actor_dict, instance)
            self._authority_for_intent()
            params = {"request_id": request_id, "phase": phase}
            if authority_rejected:
                params["authority_rejected"] = True
            reply = self._rpc(worker, "ui.release", actor_dict, engine_instance_id=instance,
                              parameters=params)
            if reply.get("engine_instance_id", instance) != instance or not isinstance(reply.get("result"), dict) or reply["result"].get("status") != "ok":
                raise _unavailable()

    def clear_children(self, principal, status, *, expected_worker_instance_id=None):
        """Reject and acknowledge at most 16 unexpected native callback children."""
        children = status.get("children") if isinstance(status, dict) else None
        if not isinstance(children, list) or len(children) > 16:
            raise _unavailable()
        for child in children:
            request_id = child.get("request_id") if isinstance(child, dict) else child
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                raise _unavailable()
            self.release_intent(principal, request_id, phase="reject", authority_rejected=True,
                                expected_worker_instance_id=expected_worker_instance_id)
            self.release_intent(principal, request_id, phase="ack",
                                expected_worker_instance_id=expected_worker_instance_id)
