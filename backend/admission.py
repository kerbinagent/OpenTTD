"""Opt-in, finite admission to existing independently provisioned agent companies.

The private pool file is an operator input. Inactive current credentials remain
valid configuration but cannot be admitted. Rotated token files no longer bound
to their declared run are invalid configuration and must be updated by the
operator before restart. A running service instead reports ``join_revoked`` for
such a previously claimed slot; neither case silently reallocates its company.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
import threading

from .errors import ServiceError
from .json_tools import loads


_CLAIM = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_RUN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _private_file(path, maximum):
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("Agent pool paths must be canonical absolute paths")
    try:
        if Path(path).resolve(strict=True) != Path(path):
            raise ValueError("Agent pool paths must be canonical absolute paths")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > maximum):
                raise ValueError("Agent pool inputs must be bounded owner-only regular files")
            raw = file.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("Agent pool input exceeds its size limit")
        return raw
    except OSError:
        raise ValueError("Agent pool input cannot be read") from None


@dataclass(frozen=True)
class _Slot:
    principal: object
    token_hash: str
    token: str = field(repr=False)


class AgentAdmission:
    """One immutable pool with serialized preparation and durable exact claims."""

    def __init__(self, store, world_id, world_epoch, slots, renderer):
        self.store = store
        self.world_id = world_id
        self.world_epoch = world_epoch
        self.slots = tuple(slots)
        self._by_run = {slot.principal.run_id: slot for slot in self.slots}
        self.renderer = renderer
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path, store, world_id, renderer=None):
        """Load a strict trusted map; never discover token paths from public input."""
        try:
            value = loads(_private_file(os.fspath(path), 32768).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise ValueError("Invalid private agent join pool") from None
        if (not isinstance(value, dict) or set(value) != {"version", "world_id", "world_epoch", "runs"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["world_id"] != world_id or value["world_epoch"] != store.world_epoch(world_id)
                or not isinstance(value["runs"], dict) or not 1 <= len(value["runs"]) <= 15):
            raise ValueError("Invalid agent pool schema, world or epoch")
        slots, companies, token_paths = [], set(), set()
        for run_id, settings in value["runs"].items():
            if (not isinstance(run_id, str) or not _RUN.fullmatch(run_id)
                    or not isinstance(settings, dict) or set(settings) != {"token_file"}):
                raise ValueError("Invalid agent pool run settings")
            principal = store.get_principal(run_id)
            if (principal is None or principal.world_id != world_id or principal.kind != "agent"
                    or principal.mode not in ("text", "visual") or principal.company_id in companies):
                raise ValueError("Agent pool requires separate eligible agent companies")
            path = settings["token_file"]
            raw = _private_file(path, 512)
            try:
                token = raw.decode("ascii").rstrip("\n")
            except UnicodeError:
                raise ValueError("Invalid agent pool credential") from None
            if (not token or len(token) > 256 or any(char.isspace() for char in token)
                    or path in token_paths):
                raise ValueError("Invalid or reused agent pool credential file")
            digest = hashlib.sha256(token.encode()).hexdigest()
            if not store.credential_matches(principal, digest, active=False):
                raise ValueError("Agent pool credential does not match its declared run")
            if principal.mode == "visual" and (renderer is None or not renderer.available(principal)):
                raise ValueError("Capture agent pool run requires its configured renderer")
            companies.add(principal.company_id)
            token_paths.add(path)
            slots.append(_Slot(principal, digest, token))
        return cls(store, world_id, value["world_epoch"], slots, renderer)

    def unclaimed_runs(self):
        """Unused configured slots, for public presentation only."""
        return self._by_run.keys() - self.store.agent_join_claimed_runs(self.world_id)

    def _reply(self, claim, mode):
        if claim["mode"] != mode:
            raise ServiceError("join_conflict", status=409)
        slot = self._by_run.get(claim["run_id"])
        if (slot is None or slot.principal.mode != mode
                or not self.store.credential_matches(slot.principal, slot.token_hash)):
            raise ServiceError("join_revoked", status=401)
        principal = slot.principal
        return {"api_version": "v1", "world_id": self.world_id, "company_id": principal.company_id,
                "identity_id": principal.identity_id, "run_id": principal.run_id,
                "mode": principal.mode, "token": slot.token}

    def _world_ready(self):
        with self.store.lock:
            world = self.store.connection.execute(
                "SELECT epoch,recovery_state FROM worlds WHERE id=?", (self.world_id,),
            ).fetchone()
        if world is None or world["epoch"] != self.world_epoch or world["recovery_state"] != "ready":
            raise ServiceError("world_recovering", status=503)

    def join(self, secret, body, service):
        if (not isinstance(body, dict) or set(body) != {"mode"} or body["mode"] not in ("text", "visual")
                or not isinstance(secret, str) or not _CLAIM.fullmatch(secret)):
            raise ServiceError("invalid_request")
        # Reject alternative encodings of the same 32 bytes as distinct claims.
        raw = base64.urlsafe_b64decode(secret + "=")
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != secret:
            raise ServiceError("invalid_request")
        mode, digest = body["mode"], hashlib.sha256(secret.encode()).hexdigest()
        claim = self.store.agent_join_claim(self.world_id, digest)
        if claim is not None:
            return self._reply(claim, mode)  # Recover an existing credential even during native outage.
        with self._lock:
            claim = self.store.agent_join_claim(self.world_id, digest)
            if claim is not None:
                return self._reply(claim, mode)
            claimed = self.store.agent_join_claimed_runs(self.world_id)
            slot = next((slot for slot in self.slots
                         if slot.principal.mode == mode and slot.principal.run_id not in claimed
                         and self.store.credential_matches(slot.principal, slot.token_hash)), None)
            if slot is None:
                raise ServiceError("join_pool_full", status=409)
            self._world_ready()
            try:
                hello = service._hello(slot.principal, refresh=True)
                if hello.get("recovery_required") is not False:
                    raise ServiceError("world_recovering", status=503)
                reply = service._rpc("company.ensure", company_id=slot.principal.company_id,
                                     create=False, engine_instance_id=hello["engine_instance_id"])
                if reply.get("status") not in ("completed", "ok"):
                    raise ServiceError("join_not_ready", status=503)
                if mode == "visual":
                    lag = hello.get("native_max_lag_ticks")
                    if type(lag) is not int or not 1 <= lag <= 65535:
                        raise ServiceError("join_not_ready", status=503)
                    self.renderer.configure_authority(hello["engine_instance_id"], lag)
                    self.renderer.check_ready(slot.principal)
            except ServiceError as exc:
                if exc.code == "world_recovering":
                    raise
                raise ServiceError("join_not_ready", status=503) from None
            claim = self.store.claim_agent_join(
                self.world_id, self.world_epoch, digest, mode, slot.principal,
                slot.token_hash, hello["engine_instance_id"])
            return self._reply(claim, mode)
