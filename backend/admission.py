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
from types import SimpleNamespace

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
                service._bind(slot.principal, hello)
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


class DynamicAgentAdmission:
    """Actual native allocation, with durable setup and no bearer until ready.

    HTTP only persists a creation intent. The existing broker advances its
    journaled mutations and verified checkpoint receipts. Retrying a claim can
    never allocate a replacement company.
    """

    def __init__(self, store, world_id, renderer, legacy=None):
        self.store, self.world_id, self.renderer, self.legacy = store, world_id, renderer, legacy
        self.world_epoch = store.world_epoch(world_id)
        self._lock = threading.Lock()
        for row in store.registrations(world_id):
            if row["worker_slot"] is not None and row["company_id"] is not None:
                self._assign(row)

    def _assign(self, row):
        principal = self.store.get_principal(row["run_id"])
        if self.renderer is None or principal is None:
            raise ServiceError("renderer_unavailable", status=503)
        self.renderer.assign_slot(row["worker_slot"], principal)
        return principal

    def unclaimed_runs(self):
        pending = {row["run_id"] for row in self.store.registrations(self.world_id) if not row["ready"]}
        return pending | (set(self.legacy.unclaimed_runs()) if self.legacy else set())

    @staticmethod
    def _owner(principal):
        return {key: getattr(principal, key) for key in ("identity_id", "company_id", "run_id", "mode")}

    @staticmethod
    def _assert_owner(principal, service, hello, *, require_registration=True):
        if require_registration and hello.get("company_registration") != {"version": 1}:
            raise ServiceError("join_not_ready", status=503)
        if hello.get("recovery_required") is not False:
            raise ServiceError("world_recovering", status=503)
        result = service._rpc("session.bind", principal, owner_bound=True,
                              display_name=principal.display_name, kind=principal.kind,
                              engine_instance_id=hello["engine_instance_id"])
        if result.get("status") not in ("completed", "ok") or result.get("result", {}).get("bound") is not True:
            code = (result.get("error") or {}).get("code")
            if code in {"checkpoint_busy", "world_unavailable", "ledger_unavailable", "authority_unavailable"}:
                raise ServiceError("join_not_ready", status=503)
            if code in {"world_recovering", "recovery_required", "world_replaced", "engine_fence"}:
                raise ServiceError("world_recovering", status=503)
            raise ServiceError("company_unavailable", status=409)

    def verify_resume(self, principal, service):
        row = self.store.registration_for_run(principal.run_id)
        return self.verify_owner(principal, service, row)

    @staticmethod
    def verify_owner(principal, service, row):
        """Ownership remains mandatory even when new admission is disabled."""
        if row is None:
            return {}
        epoch = service.store.world_epoch(principal.world_id)
        if not row["ready"] or row["world_epoch"] != epoch:
            raise ServiceError("join_not_ready", status=503)
        DynamicAgentAdmission._assert_owner(principal, service, service._hello(principal, refresh=True))
        return {"world_epoch": epoch, "registration_id": row["run_id"]}

    def _reply(self, secret, row, service):
        if not row["ready"]:
            record = self.store.get_internal(self.world_id, row["allocation_request_id"], "company.register")
            if record["status"] in ("uncertain", "resolved_rolled_back"):
                raise ServiceError("join_uncertain", status=409)
            if record["status"] == "rejected":
                raise ServiceError("join_rejected", status=409)
            if row["worker_request_id"] and row["company_id"] is not None:
                grant = self.store.get_request(self.store.get_principal(row["run_id"]), row["worker_request_id"])
                if grant["status"] in ("uncertain", "resolved_rolled_back"):
                    raise ServiceError("join_uncertain", status=409)
                if (grant["status"] == "rejected" and (grant.get("error") or {}).get("code")
                        not in {"invalid_targets", "stale_target", "invalid_parameters"}):
                    raise ServiceError("join_rejected", status=409)
            raise ServiceError("join_not_ready", status=503)
        principal = self.store.authenticate(secret)
        metadata = self.verify_resume(principal, service)
        return {"api_version": "v1", "world_id": self.world_id, **self._owner(principal),
                **metadata, "token": secret}

    def join(self, secret, body, service):
        if (not isinstance(body, dict) or set(body) not in ({"mode"}, {"mode", "intent"})
                or body.get("mode") not in ("text", "visual")
                or ("intent" in body and body["intent"] != "new")
                or not isinstance(secret, str) or not _CLAIM.fullmatch(secret)):
            raise ServiceError("invalid_request")
        raw = base64.urlsafe_b64decode(secret + "=")
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != secret:
            raise ServiceError("invalid_request")
        digest = hashlib.sha256(secret.encode()).hexdigest()
        if self.legacy and self.store.agent_join_claim(self.world_id, digest) is not None:
            # Existing legacy capabilities retain their original token semantics.
            if self.store.registration(self.world_id, digest) is None:
                if "intent" in body:
                    raise ServiceError("join_conflict", status=409)
                return self.legacy.join(secret, body, service)
        with self._lock:
            row = self.store.registration(self.world_id, digest)
            if row is None:
                hello = service._hello(SimpleNamespace(world_id=self.world_id), refresh=True)
                if hello.get("company_registration") != {"version": 1}:
                    raise ServiceError("join_not_ready", status=503)
                if hello.get("recovery_required") is not False:
                    raise ServiceError("world_recovering", status=503)
                slots = self.renderer.worker_slots() if self.renderer else ()
                row = self.store.prepare_registration(self.world_id, self.world_epoch, digest, body["mode"], slots)
            elif row["mode"] != body["mode"]:
                raise ServiceError("join_conflict", status=409)
        service._wake.set()
        return self._reply(secret, row, service)

    def _worker_participant(self, service, hello, key):
        instance = hello["engine_instance_id"]
        reply = service._rpc("chat.roster", engine_instance_id=instance)
        if reply.get("status") not in ("completed", "ok") or reply.get("engine_instance_id") != instance:
            raise ServiceError("join_not_ready", status=503)
        result = reply.get("result")
        clients = result.get("clients") if isinstance(result, dict) else result
        if not isinstance(clients, list) or len(clients) > 256:
            raise ServiceError("join_not_ready", status=503)
        matches = [client for client in clients if isinstance(client, dict)
                   and isinstance(client.get("public_key"), str) and client["public_key"].lower() == key]
        if len(matches) != 1:
            raise ServiceError("join_not_ready", status=503)
        handle = matches[0].get("native_handle")
        if service._handle_identity(handle)[2] != instance:
            raise ServiceError("join_not_ready", status=503)
        return handle

    def advance(self, service, hello):
        """One bounded setup pass, exclusively on the broker thread."""
        for row in self.store.registrations(self.world_id):
            if row["ready"]:
                continue
            try:
                self._advance_one(row, service, hello)
            except ServiceError:
                # Persisted journal state remains the sole retry authority.
                continue

    @staticmethod
    def repair_worker(row, service, hello):
        """Restore a saved key grant's attachment; never allocate or regrant."""
        store, renderer = service.store, service.renderer
        principal = store.get_principal(row["run_id"])
        if principal is None:
            raise ServiceError("company_unavailable", status=409)
        store.validate_principal(principal)
        DynamicAgentAdmission._assert_owner(principal, service, hello)
        grant_id = row["worker_request_id"]
        if not grant_id or not row["worker_public_key"]:
            raise ServiceError("join_not_ready", status=503)
        grant = store.get_request(principal, grant_id)
        if grant["status"] != "completed" or grant["checkpoint_state"] != "checkpointed":
            raise ServiceError("join_not_ready", status=503)
        if renderer is None:
            raise ServiceError("renderer_unavailable", status=503)
        renderer.assign_slot(row["worker_slot"], principal)
        key = renderer.worker_key(row["worker_slot"])
        if key != row["worker_public_key"]:
            raise ServiceError("join_worker_changed", status=409)
        lag = hello.get("native_max_lag_ticks")
        if type(lag) is not int or not 1 <= lag <= 65535:
            raise ServiceError("join_not_ready", status=503)
        renderer.configure_authority(hello["engine_instance_id"], lag)
        attached = service._rpc("renderer.attach", owner=DynamicAgentAdmission._owner(principal), native_public_key=key,
                                engine_instance_id=hello["engine_instance_id"])
        if attached.get("status") not in ("completed", "ok"):
            raise ServiceError("join_not_ready", status=503)
        renderer.check_ready(principal)

    def _advance_one(self, row, service, hello):
        record = self.store.get_internal(self.world_id, row["allocation_request_id"], "company.register")
        if record["status"] != "completed":
            return
        result = record.get("result") or {}
        company = result.get("company_id")
        owner = {"identity_id": row["identity_id"], "run_id": row["run_id"], "mode": row["mode"],
                 "company_id": company}
        if (type(company) is not int or not 0 <= company < 15 or result.get("owner") != owner
                or result.get("owner_bound") is not True):
            raise ServiceError("registration_receipt_unverified", status=503)
        self.store.provisional_registration(self.world_id, row["claim_hash"], company)
        principal = self.store.get_principal(row["run_id"])
        self._assert_owner(principal, service, hello)
        if row["mode"] == "visual":
            self._assign(dict(row, company_id=company))
            lag = hello.get("native_max_lag_ticks")
            if type(lag) is not int or not 1 <= lag <= 65535:
                raise ServiceError("join_not_ready", status=503)
            self.renderer.configure_authority(hello["engine_instance_id"], lag)
            key = self.renderer.worker_key(row["worker_slot"])
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ServiceError("join_not_ready", status=503)
            self.store.bind_registration_worker_key(self.world_id, row["claim_hash"], key)
            current = self.store.registration(self.world_id, row["claim_hash"])
            grant = (self.store.get_request(principal, current["worker_request_id"])
                     if current["worker_request_id"] else None)
            if grant is None or grant["status"] == "rejected":
                handle = self._worker_participant(service, hello, key)
                request_id = self.store.registration_worker_request_id(principal, handle)
                payload = {"action": {"name": "company.participant.authorize", "parameters": {
                    "participant": handle, "allowed": True}}, "preconditions": {}}
                grant, _ = self.store.accept_registration_setup(principal, request_id, payload)
            if grant["status"] != "completed":
                return
            if grant["checkpoint_state"] != "checkpointed":
                service.checkpoints.request()
                return
            self.repair_worker(self.store.registration(self.world_id, row["claim_hash"]), service, hello)
        if record["checkpoint_state"] != "checkpointed":
            service.checkpoints.request()
            return
        # Last authority check is deliberately uncached, including for text.
        self._assert_owner(principal, service, hello)
        self.store.finalize_registration(self.world_id, row["claim_hash"])
