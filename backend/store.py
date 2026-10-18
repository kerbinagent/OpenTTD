"""Durable, scoped state for the OpenTTD bridge."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Iterator

from .errors import ServiceError


@dataclass(frozen=True)
class Principal:
    identity_id: str
    world_id: str
    company_id: int
    run_id: str
    mode: str
    kind: str
    display_name: str


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid_payload", "Payload must be JSON serializable") from exc


def _decode(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Store:
    def __init__(
        self,
        path: str | os.PathLike[str],
        max_pending_company: int = 64,
        max_pending_world: int = 1024,
        event_retention: int = 1000,
        world_id: str | None = None,
        max_unretired: int = 8000,
    ) -> None:
        if (min(max_pending_company, max_pending_world, event_retention) < 1
                or type(max_unretired) is not int or not 1 <= max_unretired < 8192):
            raise ValueError("Store limits must be positive")
        self.max_pending_company = max_pending_company
        self.max_pending_world = max_pending_world
        self.event_retention = event_retention
        self.max_unretired = max_unretired
        self.single_world_id = world_id
        db_path = os.fspath(path)
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.lock = threading.RLock()
        self.event_condition = threading.Condition(self.lock)
        self.event_revision = 0
        self.connection = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worlds (
                    id TEXT PRIMARY KEY, epoch TEXT NOT NULL,
                    next_request_sequence INTEGER NOT NULL DEFAULT 1,
                    recovery_state TEXT NOT NULL DEFAULT 'ready',
                    recovery_instance_id TEXT,
                    engine_instance_id TEXT,
                    retired_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS identities (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    world_id TEXT NOT NULL REFERENCES worlds(id),
                    identity_id TEXT NOT NULL REFERENCES identities(id),
                    company_id INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (world_id, identity_id, company_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    world_id TEXT NOT NULL,
                    identity_id TEXT NOT NULL,
                    company_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    cursor_secret TEXT NOT NULL,
                    next_event_seq INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (world_id, identity_id, company_id)
                        REFERENCES memberships(world_id, identity_id, company_id)
                );
                CREATE INDEX IF NOT EXISTS runs_world ON runs(world_id, active);
                CREATE TABLE IF NOT EXISTS credentials (
                    token_hash TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS action_requests (
                    world_id TEXT NOT NULL,
                    identity_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    company_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    engine_sequence INTEGER,
                    request_sequence INTEGER,
                    engine_dispatched INTEGER NOT NULL DEFAULT 0,
                    intent_state TEXT NOT NULL DEFAULT 'none',
                    visual_intent TEXT,
                    visual_intent_sha256 TEXT,
                    expected_worker_instance_id TEXT,
                    ui_state TEXT NOT NULL DEFAULT 'pending',
                    tick INTEGER,
                    checkpoint_state TEXT NOT NULL DEFAULT 'uncheckpointed',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (world_id, identity_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS requests_pending_world
                    ON action_requests(world_id, status, company_id);
                CREATE TABLE IF NOT EXISTS projected_events (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    world_id TEXT NOT NULL REFERENCES worlds(id),
                    world_epoch TEXT NOT NULL,
                    save_id TEXT NOT NULL,
                    watermark INTEGER NOT NULL,
                    retired_through INTEGER,
                    tick INTEGER NOT NULL,
                    save_sha256 TEXT,
                    save_checksum TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(world_id, world_epoch, save_id)
                );
                CREATE INDEX IF NOT EXISTS checkpoints_latest ON checkpoints(world_id, id DESC);
                CREATE TABLE IF NOT EXISTS checkpoint_receipts (
                    world_id TEXT NOT NULL,
                    save_id TEXT NOT NULL,
                    request_sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    receipt TEXT NOT NULL,
                    PRIMARY KEY(world_id, save_id, request_sequence)
                );
                """
            )
            self._migrate_schema()
            if world_id is not None:
                self.enforce_single_world(world_id)

    def _migrate_schema(self) -> None:
        """Upgrade an existing draft database without renumbering allocated work."""
        columns = lambda table: {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in (
            ("next_request_sequence", "INTEGER NOT NULL DEFAULT 1"),
            ("recovery_state", "TEXT NOT NULL DEFAULT 'ready'"),
            ("recovery_instance_id", "TEXT"),
            ("engine_instance_id", "TEXT"),
            ("retired_through", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns("worlds"):
                self.connection.execute(f"ALTER TABLE worlds ADD COLUMN {name} {definition}")
        if "request_sequence" not in columns("action_requests"):
            self.connection.execute("ALTER TABLE action_requests ADD COLUMN request_sequence INTEGER")
        if "engine_dispatched" not in columns("action_requests"):
            self.connection.execute("ALTER TABLE action_requests ADD COLUMN engine_dispatched INTEGER NOT NULL DEFAULT 0")
            # Old rows lack the pre-dispatch marker; assume every non-accepted
            # row reached the engine so retirement stays conservative.
            self.connection.execute("UPDATE action_requests SET engine_dispatched=1 WHERE status!='accepted'")
        for name, definition in (
            ("intent_state", "TEXT NOT NULL DEFAULT 'none'"),
            ("visual_intent", "TEXT"),
            ("visual_intent_sha256", "TEXT"),
            ("expected_worker_instance_id", "TEXT"),
            ("ui_state", "TEXT NOT NULL DEFAULT 'pending'"),
        ):
            if name not in columns("action_requests"):
                self.connection.execute(f"ALTER TABLE action_requests ADD COLUMN {name} {definition}")
        if "retired_through" not in columns("checkpoints"):
            self.connection.execute("ALTER TABLE checkpoints ADD COLUMN retired_through INTEGER")
        if "save_checksum" not in columns("checkpoints"):
            self.connection.execute("ALTER TABLE checkpoints ADD COLUMN save_checksum TEXT")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for world in self.connection.execute("SELECT id,next_request_sequence FROM worlds").fetchall():
                used = self.connection.execute(
                    "SELECT COALESCE(MAX(request_sequence),0) FROM action_requests WHERE world_id=?",
                    (world["id"],),
                ).fetchone()[0]
                missing = self.connection.execute(
                    "SELECT world_id,identity_id,request_id FROM action_requests "
                    "WHERE world_id=? AND request_sequence IS NULL "
                    "ORDER BY created_at,identity_id,request_id", (world["id"],),
                ).fetchall()
                sequence = max(used + 1, world["next_request_sequence"])
                for row in missing:
                    self.connection.execute(
                        "UPDATE action_requests SET request_sequence=? "
                        "WHERE world_id=? AND identity_id=? AND request_id=?",
                        (sequence, row["world_id"], row["identity_id"], row["request_id"]),
                    )
                    sequence += 1
                self.connection.execute(
                    "UPDATE worlds SET next_request_sequence=? WHERE id=?",
                    (sequence, world["id"]),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS requests_sequence ON action_requests(world_id,request_sequence)"
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def enforce_single_world(self, world_id: str | None = None) -> str:
        """Bind this database connection to exactly one world for a service."""
        with self.lock:
            ids = [row["id"] for row in self.connection.execute("SELECT id FROM worlds ORDER BY id")]
            chosen = world_id or self.single_world_id or (ids[0] if len(ids) == 1 else None)
            if not chosen or (ids and (len(ids) != 1 or ids[0] != chosen)):
                raise ServiceError("world_scope", "Database must contain one matching world", 409)
            self.single_world_id = chosen
            return chosen

    def _ensure_world(self, world_id: str) -> None:
        if self.single_world_id is not None and world_id != self.single_world_id:
            raise ServiceError("world_scope", "Wrong world for this database", 409)

    @staticmethod
    def _principal(row: sqlite3.Row) -> Principal:
        return Principal(
            identity_id=row["identity_id"], world_id=row["world_id"],
            company_id=row["company_id"], run_id=row["id"], mode=row["mode"],
            kind=row["kind"], display_name=row["display_name"],
        )

    @staticmethod
    def _request(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "request_id": row["request_id"], "identity_id": row["identity_id"],
            "world_id": row["world_id"], "run_id": row["run_id"],
            "request_sequence": row["request_sequence"],
            "engine_dispatched": bool(row["engine_dispatched"]),
            "intent_state": row["intent_state"],
            "visual_intent": _decode(row["visual_intent"]),
            "visual_intent_sha256": row["visual_intent_sha256"],
            "expected_worker_instance_id": row["expected_worker_instance_id"],
            "ui_state": row["ui_state"],
            "company_id": row["company_id"], "mode": row["mode"],
            "status": row["status"], "payload": _decode(row["payload"]),
            "result": _decode(row["result"]), "error": _decode(row["error"]),
            "engine_sequence": row["engine_sequence"], "tick": row["tick"],
            "checkpoint_state": row["checkpoint_state"],
        }

    def provision(
        self, world_id: str, identity_id: str, company_id: int,
        mode: str = "text", kind: str = "agent", display_name: str | None = None,
        world_epoch: str = "initial",
    ) -> str:
        self._ensure_world(world_id)
        if identity_id == "__backend_chat__":
            raise ServiceError("invalid_identity", "Reserved identity")
        if not all(isinstance(x, str) and x for x in (world_id, identity_id, mode, kind, world_epoch)):
            raise ServiceError("invalid_identity", "Identity fields must be nonempty strings")
        if not isinstance(company_id, int) or isinstance(company_id, bool) or not 0 <= company_id <= 14:
            raise ServiceError("invalid_company", "Company ID must be between 0 and 14")
        if mode not in ("text", "visual"):
            raise ServiceError("invalid_mode", "Mode must be text or visual")
        if kind not in ("agent", "human"):
            raise ServiceError("invalid_kind", "Kind must be agent or human")
        if display_name is None:
            display_name = identity_id
        if not isinstance(display_name, str) or not display_name:
            raise ServiceError("invalid_identity", "Display name must be a nonempty string")
        run_id = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO worlds(id,epoch) VALUES (?,?)", (world_id, world_epoch))
            world = db.execute("SELECT epoch FROM worlds WHERE id=?", (world_id,)).fetchone()
            if world["epoch"] != world_epoch:
                raise ServiceError("epoch_conflict", "World epoch differs", 409)
            db.execute("INSERT OR IGNORE INTO identities(id) VALUES (?)", (identity_id,))
            db.execute(
                "INSERT INTO memberships(world_id,identity_id,company_id,active) VALUES (?,?,?,1) "
                "ON CONFLICT(world_id,identity_id,company_id) DO UPDATE SET active=1",
                (world_id, identity_id, company_id),
            )
            db.execute(
                "INSERT INTO runs(id,world_id,identity_id,company_id,mode,kind,display_name,cursor_secret,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, world_id, identity_id, company_id, mode, kind, display_name,
                 secrets.token_hex(32), time.time()),
            )
            db.execute("INSERT INTO credentials(token_hash,run_id) VALUES (?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), run_id))
        return token

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not token:
            raise ServiceError("unauthorized", "Invalid credential", 401)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.connection.execute(
                "SELECT r.* FROM credentials c JOIN runs r ON r.id=c.run_id "
                "JOIN memberships m ON (m.world_id=r.world_id AND m.identity_id=r.identity_id "
                "AND m.company_id=r.company_id) WHERE c.token_hash=? AND r.active=1 AND m.active=1",
                (digest,),
            ).fetchone()
        if row is None:
            raise ServiceError("unauthorized", "Invalid credential", 401)
        return self._principal(row)

    def rotate_credential(self, principal: Principal) -> str:
        """Issue a new bearer credential for the same immutable run."""
        token = secrets.token_urlsafe(32)
        with self._transaction() as db:
            self.validate_principal(principal)
            db.execute(
                "UPDATE credentials SET token_hash=? WHERE run_id=?",
                (hashlib.sha256(token.encode()).hexdigest(), principal.run_id),
            )
        return token

    def get_principal(self, run_id: str) -> Principal | None:
        """Read a run's immutable identity, including after membership revocation."""
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,),
            ).fetchone()
        return self._principal(row) if row else None

    def validate_principal(self, principal: Principal) -> Principal:
        with self.lock:
            row = self.connection.execute(
                "SELECT r.* FROM runs r JOIN memberships m ON (m.world_id=r.world_id "
                "AND m.identity_id=r.identity_id AND m.company_id=r.company_id) "
                "WHERE r.id=? AND r.active=1 AND m.active=1", (principal.run_id,),
            ).fetchone()
        if row is None or self._principal(row) != principal:
            raise ServiceError("unauthorized", "Credential is no longer active", 401)
        return principal

    def active_principals_for_world(self, world_id: str) -> list[Principal]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT r.* FROM runs r JOIN memberships m ON (m.world_id=r.world_id "
                "AND m.identity_id=r.identity_id AND m.company_id=r.company_id) "
                "WHERE r.world_id=? AND r.active=1 AND m.active=1 AND r.mode!='internal' "
                "ORDER BY r.created_at,r.id",
                (world_id,),
            ).fetchall()
        return [self._principal(row) for row in rows]

    principals_for_world = active_principals_for_world

    def company_exists(self, world_id: str, company_id: int) -> bool:
        """Whether a company has ever been provisioned in this world."""
        with self.lock:
            return self.connection.execute(
                "SELECT 1 FROM memberships m JOIN runs r ON (r.world_id=m.world_id AND "
                "r.identity_id=m.identity_id AND r.company_id=m.company_id) "
                "WHERE m.world_id=? AND m.company_id=? AND r.mode IN ('text','visual') LIMIT 1",
                (world_id, company_id),
            ).fetchone() is not None

    def _validate_run_identity(self, principal: Principal) -> None:
        """Check an immutable run binding, including one subsequently revoked."""
        with self.lock:
            row = self.connection.execute("SELECT * FROM runs WHERE id=?", (principal.run_id,)).fetchone()
        if row is None or self._principal(row) != principal:
            raise ServiceError("unauthorized", "Invalid run", 401)

    def revoke_membership(self, world_id: str, identity_id: str, company_id: int | None = None) -> None:
        with self._transaction() as db:
            if company_id is None:
                db.execute("UPDATE memberships SET active=0 WHERE world_id=? AND identity_id=?",
                           (world_id, identity_id))
                db.execute("UPDATE runs SET active=0 WHERE world_id=? AND identity_id=?",
                           (world_id, identity_id))
            else:
                db.execute("UPDATE memberships SET active=0 WHERE world_id=? AND identity_id=? AND company_id=?",
                           (world_id, identity_id, company_id))
                db.execute("UPDATE runs SET active=0 WHERE world_id=? AND identity_id=? AND company_id=?",
                           (world_id, identity_id, company_id))

    def accept_request(self, principal: Principal, request_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self._ensure_world(principal.world_id)
        if not isinstance(request_id, str) or not request_id:
            raise ServiceError("invalid_request_id", "Request ID must be a nonempty string")
        if not isinstance(payload, dict):
            raise ServiceError("invalid_payload", "Payload must be an object")
        canonical = _json(payload)
        with self._transaction() as db:
            self.validate_principal(principal)
            credential = db.execute("SELECT 1 FROM credentials WHERE run_id=?", (principal.run_id,)).fetchone()
            if principal.mode not in ("text", "visual") or not 0 <= principal.company_id <= 14 or credential is None:
                raise ServiceError("forbidden", "Action admission is unavailable", 403)
            row = db.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=?",
                (principal.world_id, principal.identity_id, request_id),
            ).fetchone()
            if row is not None:
                if (row["payload"] != canonical or row["run_id"] != principal.run_id
                        or row["company_id"] != principal.company_id or row["mode"] != principal.mode):
                    raise ServiceError("idempotency_conflict", "Request ID already has different content", 409)
                return self._request(row), False
            world = db.execute(
                "SELECT recovery_state,next_request_sequence,retired_through FROM worlds WHERE id=?",
                (principal.world_id,),
            ).fetchone()
            if world["recovery_state"] != "ready":
                raise ServiceError("world_recovering", "World is recovering", 503)
            if world["next_request_sequence"] - 1 - world["retired_through"] >= self.max_unretired:
                raise ServiceError("unretired_capacity", "World ledger capacity reached", 429)
            world_count = db.execute(
                "SELECT COUNT(*) FROM action_requests WHERE world_id=? AND status IN ('accepted','queued','uncertain')",
                (principal.world_id,),
            ).fetchone()[0]
            if world_count >= self.max_pending_world:
                raise ServiceError("world_capacity", "World request capacity reached", 429)
            company_count = db.execute(
                "SELECT COUNT(*) FROM action_requests WHERE world_id=? AND company_id=? "
                "AND status IN ('accepted','queued','uncertain')",
                (principal.world_id, principal.company_id),
            ).fetchone()[0]
            if company_count >= self.max_pending_company:
                raise ServiceError("company_capacity", "Company request capacity reached", 429)
            now = time.time()
            sequence = world["next_request_sequence"]
            db.execute("UPDATE worlds SET next_request_sequence=? WHERE id=?",
                       (sequence + 1, principal.world_id))
            db.execute(
                "INSERT INTO action_requests(world_id,identity_id,request_id,run_id,company_id,mode,status,payload,request_sequence,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'accepted',?,?,?,?)",
                (principal.world_id, principal.identity_id, request_id, principal.run_id,
                 principal.company_id, principal.mode, canonical, sequence, now, now),
            )
            row = db.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=?",
                (principal.world_id, principal.identity_id, request_id),
            ).fetchone()
            return self._request(row), True

    def reserve_internal(self, world_id: str, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._transaction() as db:
            return self.reserve_internal_in_transaction(db, world_id, request_id, payload)

    def reserve_internal_in_transaction(
        self, db: sqlite3.Connection, world_id: str, request_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Reserve a backend chat mutation in the same ordered action ledger."""
        self._ensure_world(world_id)
        if db is not self.connection or not db.in_transaction:
            raise ValueError("An active Store transaction is required")
        if not isinstance(request_id, str) or not request_id:
            raise ServiceError("invalid_request_id", "Request ID must be a nonempty string")
        if not isinstance(payload, dict) or payload.get("rpc_type") != "chat.publish":
            raise ServiceError("invalid_payload", "Invalid internal request payload")
        canonical = _json(payload)
        identity_id = "__backend_chat__"
        run_id = "__backend_chat_run__:" + world_id
        existing = db.execute(
            "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=?",
            (world_id, identity_id, request_id),
        ).fetchone()
        if existing is not None:
            if existing["payload"] != canonical or existing["run_id"] != run_id:
                raise ServiceError("idempotency_conflict", "Request ID already has different content", 409)
            return self._request(existing)
        world = db.execute(
            "SELECT recovery_state,next_request_sequence,retired_through FROM worlds WHERE id=?", (world_id,),
        ).fetchone()
        if world is None:
            raise ServiceError("not_found", "World not found", 404)
        if world["recovery_state"] != "ready":
            raise ServiceError("world_recovering", "World is recovering", 503)
        if world["next_request_sequence"] - 1 - world["retired_through"] >= self.max_unretired:
            raise ServiceError("unretired_capacity", "World ledger capacity reached", 429)
        world_pending = db.execute(
            "SELECT COUNT(*) FROM action_requests WHERE world_id=? AND status IN ('accepted','queued','uncertain')",
            (world_id,),
        ).fetchone()[0]
        if world_pending >= self.max_pending_world:
            raise ServiceError("world_capacity", "World request capacity reached", 429)
        company_pending = db.execute(
            "SELECT COUNT(*) FROM action_requests WHERE world_id=? AND company_id=255 "
            "AND status IN ('accepted','queued','uncertain')", (world_id,),
        ).fetchone()[0]
        if company_pending >= self.max_pending_company:
            raise ServiceError("company_capacity", "Internal request capacity reached", 429)
        db.execute("INSERT OR IGNORE INTO identities(id) VALUES (?)", (identity_id,))
        db.execute(
            "INSERT OR IGNORE INTO memberships(world_id,identity_id,company_id,active) VALUES (?,?,255,1)",
            (world_id, identity_id),
        )
        db.execute(
            "INSERT OR IGNORE INTO runs(id,world_id,identity_id,company_id,mode,kind,display_name,cursor_secret,created_at) "
            "VALUES (?,?,?,255,'internal','agent','Backend chat',?,?)",
            (run_id, world_id, identity_id, secrets.token_hex(32), time.time()),
        )
        sequence = world["next_request_sequence"]
        db.execute("UPDATE worlds SET next_request_sequence=? WHERE id=?", (sequence + 1, world_id))
        now = time.time()
        db.execute(
            "INSERT INTO action_requests(world_id,identity_id,request_id,run_id,company_id,mode,status,payload,request_sequence,created_at,updated_at) "
            "VALUES (?,?,?, ?,255,'internal','accepted',?,?,?,?)",
            (world_id, identity_id, request_id, run_id, canonical, sequence, now, now),
        )
        row = db.execute(
            "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=?",
            (world_id, identity_id, request_id),
        ).fetchone()
        return self._request(row)

    def get_internal(self, world_id: str, request_id: str) -> dict[str, Any] | None:
        self._ensure_world(world_id)
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id='__backend_chat__' AND request_id=?",
                (world_id, request_id),
            ).fetchone()
        return self._request(row) if row else None

    @staticmethod
    def _visual_request_row(db: sqlite3.Connection, principal: Principal, request_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=? AND run_id=?",
            (principal.world_id, principal.identity_id, request_id, principal.run_id),
        ).fetchone()
        if row is None:
            raise ServiceError("not_found", "Request not found", 404)
        payload = _decode(row["payload"])
        if (principal.mode != "visual" or row["mode"] != "visual"
                or payload.get("action", {}).get("name") != "ui.apply"):
            raise ServiceError("invalid_intent", "Request is not a visual action", 400)
        return row

    def begin_intent_capture(
        self, principal: Principal, request_id: str, expected_worker_instance_id: str,
    ) -> dict[str, Any]:
        """Commit the capture marker before making any renderer call."""
        if not isinstance(expected_worker_instance_id, str) or not expected_worker_instance_id:
            raise ServiceError("invalid_intent", "Worker instance is required")
        with self._transaction() as db:
            self.validate_principal(principal)
            row = self._visual_request_row(db, principal, request_id)
            if row["status"] != "accepted" or row["intent_state"] != "none":
                raise ServiceError("invalid_transition", "Visual capture cannot be repeated", 409)
            db.execute(
                "UPDATE action_requests SET intent_state='capturing',expected_worker_instance_id=?,updated_at=? "
                "WHERE world_id=? AND identity_id=? AND request_id=?",
                (expected_worker_instance_id, time.time(), principal.world_id, principal.identity_id, request_id),
            )
            return self._request(self._visual_request_row(db, principal, request_id))

    def abort_visual_capture(
        self, principal: Principal, request_id: str, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reject a capture that never stored a bundle or reached dispatch."""
        with self._transaction() as db:
            self._validate_run_identity(principal)
            row = self._visual_request_row(db, principal, request_id)
            if row["intent_state"] == "aborted" and row["status"] == "rejected":
                return self._request(row)
            if (row["status"] != "accepted" or row["intent_state"] != "capturing"
                    or row["visual_intent"] is not None or row["engine_dispatched"]):
                raise ServiceError("invalid_transition", "Visual capture cannot be aborted", 409)
            db.execute(
                "UPDATE action_requests SET intent_state='aborted',status='rejected',"
                "error=?,result=NULL,engine_sequence=NULL,tick=NULL,updated_at=? "
                "WHERE world_id=? AND identity_id=? AND request_id=?",
                (_json({"code": "visual_capture_aborted"}), time.time(),
                 principal.world_id, principal.identity_id, request_id),
            )
            updated = self._visual_request_row(db, principal, request_id)
            if event is not None:
                try:
                    self.validate_principal(principal)
                except ServiceError as exc:
                    if exc.code != "unauthorized":
                        raise
                else:
                    self.append_event_in_transaction(db, principal, "action.status", event)
            return self._request(updated)

    def persist_visual_intent(
        self, principal: Principal, request_id: str, bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(bundle, dict):
            raise ServiceError("invalid_intent", "Visual intent must be an object")
        canonical = _json(bundle)
        if len(canonical.encode("utf-8")) > 48000:
            raise ServiceError("visual_intent_limit", "Visual intent exceeds size limit", 413)
        with self._transaction() as db:
            self.validate_principal(principal)
            row = self._visual_request_row(db, principal, request_id)
            public_payload = _decode(row["payload"])
            image_id = public_payload["action"].get("parameters", {}).get("image_id")
            world = db.execute("SELECT epoch FROM worlds WHERE id=?", (principal.world_id,)).fetchone()
            if (not isinstance(image_id, str) or not image_id
                    or bundle.get("request_id") != request_id or bundle.get("image_id") != image_id
                    or bundle.get("world_epoch") != world["epoch"]):
                raise ServiceError("invalid_intent", "Visual intent binding differs", 400)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if row["intent_state"] == "persisted":
                if row["visual_intent"] != canonical or row["visual_intent_sha256"] != digest:
                    raise ServiceError("visual_intent_conflict", "Visual intent already differs", 409)
                return self._request(row)
            if row["intent_state"] != "capturing" or row["status"] != "accepted":
                raise ServiceError("invalid_transition", "Visual intent cannot be persisted", 409)
            db.execute(
                "UPDATE action_requests SET intent_state='persisted',visual_intent=?,visual_intent_sha256=?,updated_at=? "
                "WHERE world_id=? AND identity_id=? AND request_id=?",
                (canonical, digest, time.time(), principal.world_id, principal.identity_id, request_id),
            )
            return self._request(self._visual_request_row(db, principal, request_id))

    def mark_intent_uncertain(
        self, principal: Principal, request_id: str, event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """End a broken capture or dispatch without inventing a visual result."""
        with self._transaction() as db:
            self._validate_run_identity(principal)
            row = self._visual_request_row(db, principal, request_id)
            if row["intent_state"] not in ("capturing", "persisted", "uncertain") or row["status"] not in (
                "accepted", "queued", "uncertain"
            ):
                raise ServiceError("invalid_transition", "Visual intent cannot become uncertain", 409)
            if row["intent_state"] != "uncertain" or row["status"] != "uncertain":
                db.execute(
                    "UPDATE action_requests SET intent_state='uncertain',status='uncertain',"
                    "checkpoint_state='uncertain',result=NULL,error=NULL,engine_sequence=NULL,tick=NULL,updated_at=? "
                    "WHERE world_id=? AND identity_id=? AND request_id=?",
                    (time.time(), principal.world_id, principal.identity_id, request_id),
                )
            updated = self._visual_request_row(db, principal, request_id)
            if event is not None:
                self.append_event_in_transaction(db, principal, "action.status", event)
            return self._request(updated)

    def mark_ui_state(self, principal: Principal, request_id: str, state: str) -> dict[str, Any]:
        if state not in ("pending", "released", "acked"):
            raise ServiceError("invalid_ui_state", "Invalid UI state")
        with self._transaction() as db:
            self._validate_run_identity(principal)
            row = self._visual_request_row(db, principal, request_id)
            order = {"pending": 0, "released": 1, "acked": 2}
            if (row["intent_state"] not in ("persisted", "aborted") or row["status"] == "uncertain"
                    or order[state] > order[row["ui_state"]] + 1
                    or order[state] < order[row["ui_state"]]):
                raise ServiceError("invalid_transition", "UI state cannot be changed", 409)
            if state != row["ui_state"]:
                db.execute(
                    "UPDATE action_requests SET ui_state=?,updated_at=? "
                    "WHERE world_id=? AND identity_id=? AND request_id=?",
                    (state, time.time(), principal.world_id, principal.identity_id, request_id),
                )
            return self._request(self._visual_request_row(db, principal, request_id))

    def unsynced_visual_requests(
        self, world_id: str, limit: int = 16, *, after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_world(world_id)
        if type(limit) is not int or not 1 <= limit <= 128 or type(after_sequence) is not int or after_sequence < 0:
            raise ServiceError("invalid_limit", "Invalid visual request limit")
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND mode='visual' "
                "AND intent_state IN ('persisted','aborted') AND ui_state!='acked' "
                "AND status IN ('completed','rejected','resolved_rolled_back') "
                "AND request_sequence>? ORDER BY request_sequence LIMIT ?",
                (world_id, after_sequence, limit),
            ).fetchall()
        return [self._request(row) for row in rows]

    def get_request(self, principal: Principal, request_id: str) -> dict[str, Any]:
        self.validate_principal(principal)
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=? AND run_id=?",
                (principal.world_id, principal.identity_id, request_id, principal.run_id),
            ).fetchone()
        if row is None:
            raise ServiceError("not_found", "Request not found", 404)
        return self._request(row)

    def pending_requests(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM action_requests WHERE status IN ('accepted','queued') "
                "ORDER BY created_at,world_id,identity_id,request_id"
            ).fetchall()
        return [self._request(row) for row in rows]

    def accepted_requests(self, world_id: str, after_sequence: int = 0, limit: int = 128) -> list[dict[str, Any]]:
        """Read the complete immutable request ledger in allocation order."""
        self._ensure_world(world_id)
        if (not isinstance(after_sequence, int) or after_sequence < 0
                or not isinstance(limit, int) or not 1 <= limit <= 10000):
            raise ServiceError("invalid_request", "Invalid ledger range")
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND request_sequence>? "
                "ORDER BY request_sequence LIMIT ?", (world_id, after_sequence, limit),
            ).fetchall()
        return [self._request(row) for row in rows]

    def max_request_sequence(self, world_id: str) -> int:
        self._ensure_world(world_id)
        with self.lock:
            row = self.connection.execute(
                "SELECT next_request_sequence FROM worlds WHERE id=?", (world_id,),
            ).fetchone()
        return row["next_request_sequence"] - 1 if row else 0

    def has_dispatched_requests(self, world_id: str) -> bool:
        self._ensure_world(world_id)
        with self.lock:
            return self.connection.execute(
                "SELECT 1 FROM action_requests WHERE world_id=? AND engine_dispatched=1 LIMIT 1",
                (world_id,),
            ).fetchone() is not None

    def get_engine_state(self, world_id: str) -> dict[str, Any]:
        self._ensure_world(world_id)
        with self.lock:
            row = self.connection.execute(
                "SELECT engine_instance_id,retired_through FROM worlds WHERE id=?", (world_id,),
            ).fetchone()
        if row is None:
            raise ServiceError("not_found", "World not found", 404)
        return {"engine_instance_id": row["engine_instance_id"],
                "retired_through": row["retired_through"]}

    def observe_engine(self, world_id: str, instance_id: str, retired_through: int) -> dict[str, Any]:
        """Persist a verified engine observation without granting recovery authority."""
        self._ensure_world(world_id)
        if not isinstance(instance_id, str) or not instance_id or type(retired_through) is not int or retired_through < 0:
            raise ServiceError("invalid_engine_state", "Invalid engine state")
        with self._transaction() as db:
            row = db.execute(
                "SELECT next_request_sequence,retired_through FROM worlds WHERE id=?", (world_id,),
            ).fetchone()
            if row is None:
                raise ServiceError("not_found", "World not found", 404)
            if retired_through < row["retired_through"]:
                raise ServiceError("restore_behind", "Engine retired prefix regressed", 409)
            if retired_through >= row["next_request_sequence"]:
                raise ServiceError("invalid_engine_state", "Engine retired beyond allocated requests", 409)
            db.execute(
                "UPDATE worlds SET engine_instance_id=?,retired_through=? WHERE id=?",
                (instance_id, retired_through, world_id),
            )
            return {"engine_instance_id": instance_id, "retired_through": retired_through}

    def terminal_ack_prefix(self, world_id: str) -> int:
        self._ensure_world(world_id)
        with self.lock:
            rows = self.connection.execute(
                "SELECT request_sequence,status FROM action_requests WHERE world_id=? ORDER BY request_sequence",
                (world_id,),
            ).fetchall()
        prefix = 0
        for row in rows:
            if row["request_sequence"] != prefix + 1 or row["status"] not in (
                "completed", "rejected", "resolved_rolled_back"
            ):
                break
            prefix += 1
        return prefix

    def checkpoint_ack_prefix(self, world_id: str) -> int:
        """Highest contiguous request prefix safe to retire from the engine."""
        self._ensure_world(world_id)
        with self.lock:
            rows = self.connection.execute(
                "SELECT request_sequence,status,checkpoint_state,error,engine_dispatched "
                "FROM action_requests WHERE world_id=? ORDER BY request_sequence", (world_id,),
            ).fetchall()
        prefix = 0
        for row in rows:
            if row["request_sequence"] != prefix + 1:
                break
            terminal = row["status"] in ("completed", "rejected", "resolved_rolled_back")
            local_rejection = (
                row["status"] == "rejected" and not row["engine_dispatched"]
                and isinstance(_decode(row["error"]), dict)
                and _decode(row["error"]).get("code") in (
                    "unsupported_action", "authorization_revoked", "visual_capture_aborted"
                )
            )
            if not terminal or (row["checkpoint_state"] != "checkpointed" and not local_rejection):
                break
            prefix += 1
        return prefix

    def begin_recovery(
        self, world_id: str, engine_instance_id: str, *, verified_checkpoint: bool = False,
    ) -> None:
        self._ensure_world(world_id)
        if not isinstance(engine_instance_id, str) or not engine_instance_id:
            raise ServiceError("invalid_recovery", "Invalid engine instance")
        with self._transaction() as db:
            row = db.execute("SELECT recovery_state,recovery_instance_id FROM worlds WHERE id=?",
                             (world_id,)).fetchone()
            if row is None:
                raise ServiceError("not_found", "World not found", 404)
            # Service must verify a matching loaded checkpoint or a direct
            # successor before replacing an interrupted recovery target.
            if (row["recovery_state"] == "recovering"
                    and row["recovery_instance_id"] != engine_instance_id
                    and verified_checkpoint is not True):
                raise ServiceError("recovery_conflict", "Recovery instance differs", 409)
            db.execute("UPDATE worlds SET recovery_state='recovering',recovery_instance_id=? WHERE id=?",
                       (engine_instance_id, world_id))

    def finish_recovery(
        self, world_id: str, scan_completed: bool = False, *,
        engine_instance_id: str | None = None, retired_through: int | None = None,
    ) -> None:
        self._ensure_world(world_id)
        if scan_completed is not True:
            raise ServiceError("recovery_incomplete", "Recovery scan is incomplete", 409)
        if (engine_instance_id is None) != (retired_through is None):
            raise ServiceError("invalid_recovery", "Engine state must be supplied together")
        with self._transaction() as db:
            row = db.execute(
                "SELECT recovery_state,recovery_instance_id,retired_through,next_request_sequence "
                "FROM worlds WHERE id=?", (world_id,),
            ).fetchone()
            if row is None or row["recovery_state"] != "recovering":
                raise ServiceError("recovery_conflict", "World is not recovering", 409)
            if engine_instance_id is None:
                db.execute("UPDATE worlds SET recovery_state='ready' WHERE id=?", (world_id,))
                return
            if (not isinstance(engine_instance_id, str) or not engine_instance_id
                    or type(retired_through) is not int or retired_through < 0):
                raise ServiceError("invalid_recovery", "Invalid engine state")
            if engine_instance_id != row["recovery_instance_id"]:
                raise ServiceError("recovery_conflict", "Recovery target differs", 409)
            if retired_through < row["retired_through"]:
                raise ServiceError("restore_behind", "Engine retired prefix regressed", 409)
            if retired_through >= row["next_request_sequence"]:
                raise ServiceError("invalid_engine_state", "Engine retired beyond allocated requests", 409)
            db.execute(
                "UPDATE worlds SET recovery_state='ready',engine_instance_id=?,retired_through=? WHERE id=?",
                (engine_instance_id, retired_through, world_id),
            )

    def resolve_recovery_request(
        self, principal: Principal, request_id: str, status: str,
        result: Any = None, error: Any = None, tick: int | None = None,
        checkpointed: bool = False,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply a recovery-only correction with an explicit durable basis."""
        if status not in ("completed", "rejected", "uncertain", "resolved_rolled_back"):
            raise ServiceError("invalid_status", "Invalid recovery status")
        with self._transaction() as db:
            self._validate_run_identity(principal)
            world = db.execute("SELECT recovery_state FROM worlds WHERE id=?",
                               (principal.world_id,)).fetchone()
            if world is None or world["recovery_state"] != "recovering":
                raise ServiceError("recovery_conflict", "World is not recovering", 409)
            row = db.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=? AND run_id=?",
                (principal.world_id, principal.identity_id, request_id, principal.run_id),
            ).fetchone()
            if row is None:
                raise ServiceError("not_found", "Request not found", 404)
            if row["status"] == "rejected" and status != "rejected":
                raise ServiceError("invalid_transition", "Rejected request cannot be corrected", 409)
            if tick is not None and (type(tick) is not int or tick < 0):
                raise ServiceError("invalid_tick", "Tick must be nonnegative")
            db.execute(
                "UPDATE action_requests SET status=?,result=?,error=?,tick=?,checkpoint_state=?,updated_at=? "
                "WHERE world_id=? AND identity_id=? AND request_id=?",
                (status, _json(result) if result is not None else
                 (row["result"] if status == row["status"] else None),
                 _json(error) if error is not None else
                 (row["error"] if status == row["status"] else None),
                 tick if tick is not None else row["tick"],
                 "checkpointed" if checkpointed else ("uncertain" if status == "uncertain" else "uncheckpointed"),
                 time.time(), principal.world_id, principal.identity_id, request_id),
            )
            updated = db.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=?",
                (principal.world_id, principal.identity_id, request_id),
            ).fetchone()
            if event is not None:
                self.append_event_in_transaction(db, principal, "action.status", event)
            return self._request(updated)

    def update_request(
        self, principal: Principal, request_id: str, status: str,
        result: Any = None, error: Any = None,
        engine_sequence: int | None = None, tick: int | None = None,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "accepted": {"accepted", "queued", "completed", "rejected", "uncertain"},
            "queued": {"queued", "completed", "rejected", "uncertain"},
            "uncertain": {"uncertain", "completed", "rejected"},
            "completed": {"completed"}, "rejected": {"rejected"},
        }
        if status not in allowed:
            raise ServiceError("invalid_status", "Invalid request status")
        with self._transaction() as db:
            self._validate_run_identity(principal)
            row = db.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=? AND run_id=?",
                (principal.world_id, principal.identity_id, request_id, principal.run_id),
            ).fetchone()
            if row is None:
                raise ServiceError("not_found", "Request not found", 404)
            if status not in allowed[row["status"]]:
                raise ServiceError("invalid_transition", "Request status cannot be changed", 409)
            new_result = _json(result) if result is not None else row["result"]
            new_error = _json(error) if error is not None else row["error"]
            new_sequence = engine_sequence if engine_sequence is not None else row["engine_sequence"]
            new_tick = tick if tick is not None else row["tick"]
            if row["status"] in ("completed", "rejected") and (
                new_result != row["result"] or new_error != row["error"]
                or new_sequence != row["engine_sequence"] or new_tick != row["tick"]
            ):
                raise ServiceError("invalid_transition", "Terminal request cannot be changed", 409)
            if new_sequence is not None and (not isinstance(new_sequence, int) or new_sequence < 0):
                raise ServiceError("invalid_sequence", "Engine sequence must be nonnegative")
            if new_tick is not None and (not isinstance(new_tick, int) or new_tick < 0):
                raise ServiceError("invalid_tick", "Tick must be nonnegative")
            checkpoint_state = row["checkpoint_state"]
            db.execute(
                "UPDATE action_requests SET status=?,result=?,error=?,engine_sequence=?,tick=?,checkpoint_state=?,engine_dispatched=?,updated_at=? "
                "WHERE world_id=? AND identity_id=? AND request_id=?",
                (status, new_result, new_error, new_sequence, new_tick, checkpoint_state,
                 1 if status == "queued" or row["engine_dispatched"] else 0, time.time(),
                 principal.world_id, principal.identity_id, request_id),
            )
            updated = db.execute(
                "SELECT * FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=?",
                (principal.world_id, principal.identity_id, request_id),
            ).fetchone()
            if event is not None:
                self.append_event_in_transaction(db, principal, "action.status", event)
            return self._request(updated)

    @staticmethod
    def _cursor(run_id: str, secret: str, seq: int) -> str:
        body = f"v1:{seq}"
        mac = hmac.new(bytes.fromhex(secret), f"{run_id}:{body}".encode(), hashlib.sha256).digest()[:16]
        raw = f"{body}:{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @staticmethod
    def _cursor_seq(run_id: str, secret: str, cursor: str) -> int:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
            version, number, _ = raw.split(":", 2)
            seq = int(number)
            if version != "v1" or seq < 0 or not hmac.compare_digest(
                cursor, Store._cursor(run_id, secret, seq)
            ):
                raise ValueError
            return seq
        except (ValueError, UnicodeError, base64.binascii.Error, TypeError) as exc:
            raise ServiceError("invalid_cursor", "Invalid event cursor", 400) from exc

    def append_event(self, principal: Principal, kind: str, payload: Any) -> str:
        with self._transaction() as db:
            return self.append_event_in_transaction(db, principal, kind, payload)

    def append_event_in_transaction(
        self, db: sqlite3.Connection, principal: Principal, kind: str, payload: Any,
    ) -> str:
        """Append within the caller's write transaction for atomic projections."""
        if db is not self.connection or not db.in_transaction:
            raise ValueError("An active Store transaction is required")
        if not isinstance(kind, str) or not kind:
            raise ServiceError("invalid_event", "Event type must be a nonempty string")
        data = _json(payload)
        self.validate_principal(principal)
        row = db.execute("SELECT cursor_secret,next_event_seq FROM runs WHERE id=?",
                         (principal.run_id,)).fetchone()
        seq = row["next_event_seq"]
        db.execute("UPDATE runs SET next_event_seq=? WHERE id=?", (seq + 1, principal.run_id))
        db.execute("INSERT INTO projected_events(run_id,seq,type,data) VALUES (?,?,?,?)",
                   (principal.run_id, seq, kind, data))
        db.execute("DELETE FROM projected_events WHERE run_id=? AND seq<=?",
                   (principal.run_id, seq - self.event_retention))
        with self.event_condition:
            self.event_revision += 1
            self.event_condition.notify_all()
        return self._cursor(principal.run_id, row["cursor_secret"], seq)

    def wait_for_events(self, principal: Principal, after: str | None = None, timeout: float = 1) -> bool:
        """Wait for a recipient event after a public cursor, returning on timeout."""
        if not isinstance(timeout, (int, float)) or timeout < 0:
            raise ServiceError("invalid_timeout", "Invalid wait timeout")
        deadline = time.monotonic() + timeout
        with self.event_condition:
            while True:
                if self.events(principal, after=after, limit=1)["events"]:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.event_condition.wait(remaining)

    def current_event_cursor(self, principal: Principal) -> str:
        """Read the run's latest cursor before a fresh authority snapshot.

        The lock is held only for this SQLite read, never across native RPC.
        A recovering world cannot provide an authoritative current status.
        """
        with self.lock:
            self.validate_principal(principal)
            row = self.connection.execute(
                "SELECT r.cursor_secret,r.next_event_seq,w.recovery_state "
                "FROM runs r JOIN worlds w ON w.id=r.world_id WHERE r.id=?",
                (principal.run_id,),
            ).fetchone()
            if row is None or row["recovery_state"] != "ready":
                raise ServiceError("world_recovering", status=503)
            return self._cursor(principal.run_id, row["cursor_secret"], row["next_event_seq"] - 1)

    def events(self, principal: Principal, after: str | None = None, limit: int = 100) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ServiceError("invalid_limit", "Event limit must be between 1 and 500")
        self.validate_principal(principal)
        with self.lock:
            run = self.connection.execute("SELECT cursor_secret,next_event_seq FROM runs WHERE id=?",
                                          (principal.run_id,)).fetchone()
            seq = self._cursor_seq(principal.run_id, run["cursor_secret"], after) if after is not None else 0
            latest = run["next_event_seq"] - 1
            if seq > latest:
                raise ServiceError("invalid_cursor", "Invalid event cursor")
            oldest = self.connection.execute(
                "SELECT MIN(seq) FROM projected_events WHERE run_id=?", (principal.run_id,)
            ).fetchone()[0]
            if after is None and oldest is not None:
                seq = oldest - 1
            elif oldest is not None and seq < oldest - 1:
                raise ServiceError("resync_required", "Event cursor has expired", 409)
            rows = self.connection.execute(
                "SELECT seq,type,data FROM projected_events WHERE run_id=? AND seq>? "
                "ORDER BY seq LIMIT ?", (principal.run_id, seq, limit),
            ).fetchall()
        events = [
            {"id": self._cursor(principal.run_id, run["cursor_secret"], row["seq"]),
             "type": row["type"], "data": _decode(row["data"])}
            for row in rows
        ]
        final_seq = rows[-1]["seq"] if rows else seq
        return {"events": events, "cursor": self._cursor(principal.run_id, run["cursor_secret"], final_seq)}

    def world_epoch(self, world_id: str) -> str:
        with self.lock:
            row = self.connection.execute("SELECT epoch FROM worlds WHERE id=?", (world_id,)).fetchone()
        if row is None:
            raise ServiceError("not_found", "World not found", 404)
        return row["epoch"]

    def list_worlds(self) -> list[str]:
        with self.lock:
            rows = self.connection.execute("SELECT id FROM worlds ORDER BY id").fetchall()
        return [row["id"] for row in rows]

    def checkpoint(
        self, world_id: str, world_epoch: str, save_id: str, watermark: int,
        tick: int, save_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Legacy manifest wrapper; no request becomes durable without a receipt."""
        return self.apply_checkpoint(world_id, {
            "world_epoch": world_epoch, "save_id": save_id,
            "retired_through": watermark, "tick": tick, "save_sha256": save_sha256,
        }, [])

    @staticmethod
    def _normalize_receipts(receipts: list[dict[str, Any]]) -> dict[int, tuple[str, str]]:
        if not isinstance(receipts, list):
            raise ServiceError("invalid_checkpoint", "Invalid checkpoint receipts")
        normalized: dict[int, tuple[str, str]] = {}
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise ServiceError("invalid_checkpoint", "Invalid checkpoint receipt")
            sequence = receipt.get("request_sequence", receipt.get("sequence"))
            raw_status = receipt.get("status", receipt.get("state"))
            status = {"executed": "completed", "rolled_back": "resolved_rolled_back"}.get(
                raw_status, raw_status
            )
            if (type(sequence) is not int or sequence < 1
                    or status not in ("completed", "rejected", "resolved_rolled_back",
                                      "accepted", "reserved", "queued", "uncertain")
                    or sequence in normalized):
                raise ServiceError("invalid_checkpoint", "Invalid checkpoint receipt")
            normalized[sequence] = (status, _json(receipt))
        return normalized

    def apply_checkpoint(
        self, world_id: str, checkpoint: dict[str, Any], receipts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._ensure_world(world_id)
        if not isinstance(checkpoint, dict) or not isinstance(receipts, list):
            raise ServiceError("invalid_checkpoint", "Invalid checkpoint manifest")
        world_epoch = checkpoint.get("world_epoch")
        save_id = checkpoint.get("save_id")
        retired_through = checkpoint.get("retired_through")
        tick = checkpoint.get("tick")
        save_sha256 = checkpoint.get("save_sha256")
        save_checksum = checkpoint.get("save_checksum")
        if not all(isinstance(v, str) and v for v in (world_id, world_epoch, save_id)):
            raise ServiceError("invalid_checkpoint", "Checkpoint identifiers must be nonempty")
        if any(type(v) is not int or v < 0 for v in (retired_through, tick)):
            raise ServiceError("invalid_checkpoint", "Checkpoint counters must be nonnegative")
        if save_sha256 is not None and (
            not isinstance(save_sha256, str) or len(save_sha256) != 64
            or any(ch not in "0123456789abcdefABCDEF" for ch in save_sha256)
        ):
            raise ServiceError("invalid_checkpoint", "Invalid save digest")
        if save_checksum is not None and (
            not isinstance(save_checksum, str) or not save_checksum or len(save_checksum) > 128
        ):
            raise ServiceError("invalid_checkpoint", "Invalid save checksum")
        normalized = self._normalize_receipts(receipts)
        with self._transaction() as db:
            world = db.execute("SELECT epoch FROM worlds WHERE id=?", (world_id,)).fetchone()
            if world is None:
                db.execute("INSERT INTO worlds(id,epoch) VALUES (?,?)", (world_id, world_epoch))
            elif world["epoch"] != world_epoch:
                raise ServiceError("epoch_conflict", "World epoch differs", 409)
            existing = db.execute(
                "SELECT * FROM checkpoints WHERE world_id=? AND world_epoch=? AND save_id=?",
                (world_id, world_epoch, save_id),
            ).fetchone()
            if existing is not None:
                previous = db.execute(
                    "SELECT request_sequence,status,receipt FROM checkpoint_receipts "
                    "WHERE world_id=? AND save_id=?", (world_id, save_id),
                ).fetchall()
                old_receipts = {row["request_sequence"]: (row["status"], row["receipt"]) for row in previous}
                if (existing["watermark"] != retired_through or existing["tick"] != tick
                        or old_receipts != normalized
                        or existing["save_sha256"] != save_sha256
                        or existing["save_checksum"] != save_checksum):
                    raise ServiceError("checkpoint_conflict", "Save ID already has a different manifest", 409)
                return self._checkpoint(existing)
            latest = db.execute(
                "SELECT * FROM checkpoints WHERE world_id=? AND world_epoch=? ORDER BY id DESC LIMIT 1",
                (world_id, world_epoch),
            ).fetchone()
            if latest and retired_through < latest["watermark"]:
                raise ServiceError("watermark_regression", "Checkpoint watermark regressed", 409)
            for sequence, (status, _) in normalized.items():
                request = db.execute(
                    "SELECT status FROM action_requests WHERE world_id=? AND request_sequence=?",
                    (world_id, sequence),
                ).fetchone()
                if request is None or (status in ("completed", "rejected", "resolved_rolled_back")
                                       and request["status"] != status):
                    raise ServiceError("receipt_conflict", "Receipt does not match a terminal request", 409)
            cursor = db.execute(
                "INSERT INTO checkpoints(world_id,world_epoch,save_id,watermark,retired_through,tick,save_sha256,save_checksum,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (world_id, world_epoch, save_id, retired_through, retired_through, tick,
                 save_sha256, save_checksum, time.time()),
            )
            checkpoint_id = cursor.lastrowid
            for sequence, (status, receipt_json) in normalized.items():
                db.execute(
                    "INSERT INTO checkpoint_receipts(world_id,save_id,request_sequence,status,receipt) "
                    "VALUES (?,?,?,?,?)", (world_id, save_id, sequence, status, receipt_json),
                )
                if status in ("completed", "rejected", "resolved_rolled_back"):
                    db.execute(
                        "UPDATE action_requests SET checkpoint_state='checkpointed' "
                        "WHERE world_id=? AND request_sequence=?", (world_id, sequence),
                    )
            return self._checkpoint(db.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone())

    def merge_checkpoint_receipts(
        self, world_id: str, save_id: str, receipts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Attach later verified terminal receipts to an immutable manifest."""
        self._ensure_world(world_id)
        if not isinstance(save_id, str) or not save_id:
            raise ServiceError("invalid_checkpoint", "Invalid checkpoint ID")
        normalized = self._normalize_receipts(receipts)
        if any(status not in ("completed", "rejected", "resolved_rolled_back")
               for status, _ in normalized.values()):
            raise ServiceError("invalid_checkpoint", "Only terminal receipts can be merged")
        with self._transaction() as db:
            manifest = db.execute(
                "SELECT * FROM checkpoints WHERE world_id=? AND save_id=? ORDER BY id DESC LIMIT 1",
                (world_id, save_id),
            ).fetchone()
            if manifest is None:
                raise ServiceError("not_found", "Checkpoint not found", 404)
            for sequence, (status, receipt_json) in normalized.items():
                request = db.execute(
                    "SELECT status FROM action_requests WHERE world_id=? AND request_sequence=?",
                    (world_id, sequence),
                ).fetchone()
                if request is None or request["status"] != status:
                    raise ServiceError("receipt_conflict", "Receipt does not match a terminal request", 409)
                existing = db.execute(
                    "SELECT status,receipt FROM checkpoint_receipts "
                    "WHERE world_id=? AND save_id=? AND request_sequence=?",
                    (world_id, save_id, sequence),
                ).fetchone()
                if existing is not None:
                    if existing["status"] != status or existing["receipt"] != receipt_json:
                        raise ServiceError("receipt_conflict", "Receipt differs from saved evidence", 409)
                else:
                    db.execute(
                        "INSERT INTO checkpoint_receipts(world_id,save_id,request_sequence,status,receipt) "
                        "VALUES (?,?,?,?,?)", (world_id, save_id, sequence, status, receipt_json),
                    )
                db.execute(
                    "UPDATE action_requests SET checkpoint_state='checkpointed' "
                    "WHERE world_id=? AND request_sequence=?", (world_id, sequence),
                )
            return self._checkpoint(manifest)

    @staticmethod
    def _checkpoint(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "world_id": row["world_id"], "world_epoch": row["world_epoch"],
            "save_id": row["save_id"], "watermark": row["watermark"],
            "retired_through": row["retired_through"] if row["retired_through"] is not None else row["watermark"],
            "tick": row["tick"], "save_sha256": row["save_sha256"],
            "save_checksum": row["save_checksum"],
        }

    def latest_checkpoint(self, world_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM checkpoints WHERE world_id=? ORDER BY id DESC LIMIT 1", (world_id,)
            ).fetchone()
        return self._checkpoint(row) if row else None
