"""Durable, recipient-scoped chat for the backend service."""

from __future__ import annotations

import json
import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .errors import ServiceError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Social:
    SPECTATOR_COMPANY = 255
    ENGINE_MAX_BODY_BYTES = 768
    ENGINE_MAX_LABEL_BYTES = 80
    _PUBLIC_KEY = re.compile(r"[0-9a-fA-F]{64}\Z")

    @staticmethod
    def native_identity_id(world_id: str, canonical_key: str) -> str:
        return "native:" + hashlib.sha256((world_id + "\0" + canonical_key).encode()).hexdigest()

    def __init__(self, store: Any, message_limit: int = 10, window_seconds: float = 1.0,
                 max_message_bytes: int = 768, max_pending_world: int = 1024,
                 max_pending_company: int = 64):
        if (message_limit < 1 or window_seconds <= 0 or max_message_bytes < 1
                or max_pending_world < 1 or max_pending_company < 1):
            raise ValueError("social limits must be positive")
        self.store = store
        self.message_limit = message_limit
        self.window_seconds = window_seconds
        self.max_message_bytes = min(max_message_bytes, self.ENGINE_MAX_BODY_BYTES)
        self.max_pending_world = max_pending_world
        self.max_pending_company = max_pending_company
        with store.lock:
            store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS social_threads (
                    id TEXT PRIMARY KEY,
                    world_id TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('world', 'company', 'direct_company', 'direct_identity')),
                    company_a TEXT NOT NULL,
                    company_b TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(world_id, scope, company_a, company_b)
                );
                CREATE INDEX IF NOT EXISTS social_threads_world ON social_threads(world_id);
                CREATE TABLE IF NOT EXISTS social_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    world_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL REFERENCES social_threads(id),
                    sender_identity_id TEXT NOT NULL,
                    sender_company_id TEXT,
                    sender_kind TEXT NOT NULL,
                    sender_display_name TEXT NOT NULL,
                    body TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    tick INTEGER NOT NULL,
                    tick_authoritative INTEGER NOT NULL DEFAULT 0,
                    recipients_json TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    UNIQUE(world_id, sender_identity_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS social_messages_thread ON social_messages(thread_id, seq);
                CREATE TABLE IF NOT EXISTS social_message_recipients (
                    message_id TEXT NOT NULL REFERENCES social_messages(id),
                    identity_id TEXT NOT NULL,
                    company_id INTEGER NOT NULL,
                    PRIMARY KEY(message_id, identity_id, company_id)
                );
                CREATE INDEX IF NOT EXISTS social_recipient_identity ON social_message_recipients(identity_id, company_id, message_id);
                CREATE TABLE IF NOT EXISTS social_delivery_outbox (
                    message_id TEXT PRIMARY KEY REFERENCES social_messages(id),
                    world_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','delivered','uncertain')),
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS social_delivery_pending ON social_delivery_outbox(world_id,status,updated_at);
                CREATE TABLE IF NOT EXISTS social_native_roster (
                    world_id TEXT NOT NULL,
                    public_key TEXT NOT NULL,
                    wire_public_key TEXT NOT NULL,
                    identity_id TEXT NOT NULL,
                    company_id INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    PRIMARY KEY(world_id,public_key)
                );
                CREATE INDEX IF NOT EXISTS social_roster_identity ON social_native_roster(world_id,identity_id,company_id,active);
                CREATE TABLE IF NOT EXISTS social_message_native_recipients (
                    message_id TEXT NOT NULL REFERENCES social_messages(id),
                    public_key TEXT NOT NULL,
                    identity_id TEXT NOT NULL,
                    company_id INTEGER NOT NULL,
                    PRIMARY KEY(message_id,public_key)
                );
                CREATE TABLE IF NOT EXISTS social_native_receipts (
                    world_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition IN ('accepted','rejected')),
                    reason TEXT,
                    PRIMARY KEY(world_id,event_id),
                    UNIQUE(world_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS social_blocks (
                    world_id TEXT NOT NULL,
                    blocker_identity_id TEXT NOT NULL,
                    blocked_identity_id TEXT NOT NULL,
                    PRIMARY KEY(world_id, blocker_identity_id, blocked_identity_id)
                );
                CREATE TABLE IF NOT EXISTS social_mutes (
                    world_id TEXT NOT NULL,
                    identity_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL REFERENCES social_threads(id),
                    PRIMARY KEY(world_id, identity_id, thread_id)
                );
                CREATE TABLE IF NOT EXISTS social_reports (
                    id TEXT PRIMARY KEY,
                    world_id TEXT NOT NULL,
                    message_id TEXT NOT NULL REFERENCES social_messages(id),
                    reporter_identity_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS social_rate_events (
                    world_id TEXT NOT NULL,
                    owner_key TEXT NOT NULL,
                    sent_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS social_rate_owner ON social_rate_events(world_id, owner_key, sent_at);
            """)
            columns = {row["name"] for row in store.connection.execute(
                "PRAGMA table_info(social_native_roster)")}
            if "wire_public_key" not in columns:
                store.connection.execute("ALTER TABLE social_native_roster ADD COLUMN wire_public_key TEXT")
                store.connection.execute(
                    "UPDATE social_native_roster SET wire_public_key=public_key WHERE wire_public_key IS NULL")
            for row in store.connection.execute(
                    "SELECT world_id,public_key FROM social_native_roster").fetchall():
                canonical = row["public_key"].lower()
                if canonical == row["public_key"]:
                    continue
                occupied = store.connection.execute(
                    "SELECT 1 FROM social_native_roster WHERE world_id=? AND public_key=?",
                    (row["world_id"], canonical),
                ).fetchone()
                if occupied is None:
                    store.connection.execute(
                        "UPDATE social_native_roster SET public_key=? WHERE world_id=? AND public_key=?",
                        (canonical, row["world_id"], row["public_key"]),
                    )
                    store.connection.execute(
                        """UPDATE social_message_native_recipients SET public_key=?
                           WHERE public_key=? AND message_id IN
                           (SELECT id FROM social_messages WHERE world_id=?)""",
                        (canonical, row["public_key"], row["world_id"]),
                    )

    def _current(self, principal: Any) -> Any:
        current = self.store.validate_principal(principal)
        return principal if current is None else current

    @staticmethod
    def _company(value: Any) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _transaction(connection: sqlite3.Connection, operation: Any) -> Any:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = operation()
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

    def _get_thread(self, principal: Any, thread_id: str) -> sqlite3.Row:
        row = self.store.connection.execute(
            "SELECT * FROM social_threads WHERE id=? AND world_id=?", (thread_id, principal.world_id)
        ).fetchone()
        own = self._company(principal.company_id)
        allowed = row is not None and (
            row["scope"] == "world"
            or (own != "255" and row["scope"] == "company" and row["company_a"] == own)
            or (own != "255" and row["scope"] == "direct_company" and own in (row["company_a"], row["company_b"]))
            or (row["scope"] == "direct_identity" and "identity:" + principal.identity_id
                in (row["company_a"], row["company_b"]))
        )
        if not allowed:
            raise ServiceError("thread_unavailable", "Thread unavailable", 404)
        return row

    def _ensure_thread(self, world_id: str, scope: str, company_a: str = "", company_b: str = "") -> sqlite3.Row:
        conn = self.store.connection
        conn.execute(
            "INSERT OR IGNORE INTO social_threads(id,world_id,scope,company_a,company_b,created_at) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), world_id, scope, company_a, company_b, _utc_now()),
        )
        return conn.execute(
            "SELECT * FROM social_threads WHERE world_id=? AND scope=? AND company_a=? AND company_b=?",
            (world_id, scope, company_a, company_b),
        ).fetchone()

    def _thread_dict(self, principal: Any, row: sqlite3.Row) -> dict[str, Any]:
        muted = self.store.connection.execute(
            "SELECT 1 FROM social_mutes WHERE world_id=? AND identity_id=? AND thread_id=?",
            (principal.world_id, principal.identity_id, row["id"]),
        ).fetchone() is not None
        ids = [] if row["scope"] in ("world", "direct_identity") else [int(row["company_a"])]
        if row["scope"] == "direct_company":
            ids.append(int(row["company_b"]))
        return {"id": row["id"], "thread_id": row["id"], "scope": row["scope"],
                "company_ids": ids,
                "identity_ids": ([row["company_a"][9:], row["company_b"][9:]]
                                 if row["scope"] == "direct_identity" else []),
                "created_at": row["created_at"], "muted": muted}

    def create_thread(self, principal: Any, scope: str, company_ids: Any = None) -> dict[str, Any]:
        with self.store.lock:
            principal = self._current(principal)
            own = self._company(principal.company_id)
            if scope == "world":
                if company_ids not in (None, []):
                    raise ServiceError("invalid_thread", "Invalid thread", 400)
                a = b = ""
            elif scope == "company":
                if not own or own == "255" or company_ids not in (None, [], [principal.company_id], [own]):
                    raise ServiceError("invalid_thread", "Invalid thread", 400)
                a, b = own, ""
            elif scope == "direct_company":
                if not own or own == "255" or not isinstance(company_ids, (list, tuple)) or len(company_ids) != 2:
                    raise ServiceError("invalid_thread", "Invalid thread", 400)
                ids = [self._company(x) for x in company_ids]
                if not all(ids) or len(set(ids)) != 2 or own not in ids:
                    raise ServiceError("invalid_thread", "Invalid thread", 400)
                other = next(x for x in ids if x != own)
                if not other.isdecimal() or not self.store.company_exists(principal.world_id, int(other)):
                    raise ServiceError("thread_unavailable", "Thread unavailable", 404)
                a, b = sorted(ids)
            else:
                raise ServiceError("invalid_thread", "Invalid thread", 400)
            row = self._transaction(
                self.store.connection, lambda: self._ensure_thread(principal.world_id, scope, a, b)
            )
            return self._thread_dict(principal, row)

    def list_threads(self, principal: Any) -> list[dict[str, Any]]:
        with self.store.lock:
            principal = self._current(principal)
            own = self._company(principal.company_id)
            def ensure_defaults() -> None:
                self._ensure_thread(principal.world_id, "world")
                if own and own != "255":
                    self._ensure_thread(principal.world_id, "company", own)
            self._transaction(self.store.connection, ensure_defaults)
            rows = self.store.connection.execute(
                """SELECT * FROM social_threads WHERE world_id=? AND
                   (scope='world' OR (scope='company' AND company_a=?) OR
                    (scope='direct_company' AND (company_a=? OR company_b=?)) OR
                    (scope='direct_identity' AND (company_a=? OR company_b=?)))
                   ORDER BY created_at, id""",
                (principal.world_id, own, own, own,
                 "identity:" + principal.identity_id, "identity:" + principal.identity_id),
            ).fetchall()
            return [self._thread_dict(principal, row) for row in rows]

    def _recipients(self, principal: Any, thread: sqlite3.Row) -> list[dict[str, Any]]:
        # One snapshot per identity; several resumed runs can receive the same message.
        principals = self.store.principals_for_world(principal.world_id)
        result: dict[tuple[str, int], dict[str, Any]] = {}
        for member in principals:
            if self._eligible_run(member, thread):
                result[(member.identity_id, member.company_id)] = {"identity_id": member.identity_id,
                    "display_name": member.display_name, "kind": member.kind,
                    "company_id": member.company_id}
        result[(principal.identity_id, principal.company_id)] = {"identity_id": principal.identity_id,
            "display_name": principal.display_name, "kind": principal.kind,
            "company_id": principal.company_id}
        return [result[key] for key in sorted(result)]

    def _eligible_run(self, principal: Any, thread: sqlite3.Row) -> bool:
        company = self._company(principal.company_id)
        return (thread["scope"] == "world" or
                (thread["scope"] == "company" and company == thread["company_a"]) or
                (thread["scope"] == "direct_company" and company in
                 (thread["company_a"], thread["company_b"])) or
                (thread["scope"] == "direct_identity" and "identity:" + principal.identity_id
                 in (thread["company_a"], thread["company_b"])))

    def _filter_blocked(self, principal: Any, thread: sqlite3.Row,
                        recipients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if thread["scope"] not in ("direct_company", "direct_identity"):
            return recipients
        def is_other(recipient: dict[str, Any]) -> bool:
            if thread["scope"] == "direct_identity":
                return recipient["identity_id"] != principal.identity_id
            return self._company(recipient["company_id"]) != self._company(principal.company_id)
        other = [r for r in recipients if is_other(r)]
        if not other:
            raise ServiceError("delivery_unavailable", "Delivery unavailable", 403)
        blockers = {row[0] for row in self.store.connection.execute(
            "SELECT blocker_identity_id FROM social_blocks WHERE world_id=? AND blocked_identity_id=?",
            (principal.world_id, principal.identity_id),
        ).fetchall()}
        filtered = [r for r in recipients if r["identity_id"] not in blockers]
        if not any(is_other(r) for r in filtered):
            raise ServiceError("delivery_unavailable", "Delivery unavailable", 403)
        return filtered

    def _check_backlog(self, principal: Any) -> None:
        conn = self.store.connection
        world_pending = conn.execute(
            "SELECT COUNT(*) FROM social_delivery_outbox WHERE world_id=? AND status!='delivered'",
            (principal.world_id,),
        ).fetchone()[0]
        company_pending = conn.execute(
            """SELECT COUNT(*) FROM social_delivery_outbox o JOIN social_messages m ON m.id=o.message_id
               WHERE o.world_id=? AND m.sender_company_id=? AND o.status!='delivered'""",
            (principal.world_id, principal.company_id),
        ).fetchone()[0]
        if world_pending >= self.max_pending_world or company_pending >= self.max_pending_company:
            raise ServiceError("chat_backlog", "Chat delivery backlog is full", 429)

    def _consume_rate(self, principal: Any) -> None:
        conn = self.store.connection
        now = time.time()
        conn.execute("DELETE FROM social_rate_events WHERE sent_at<=?", (now-self.window_seconds,))
        owner_keys = ["identity:" + principal.identity_id]
        if principal.company_id is not None:
            owner_keys.append("company:" + self._company(principal.company_id))
        for key in owner_keys:
            count = conn.execute(
                "SELECT COUNT(*) FROM social_rate_events WHERE world_id=? AND owner_key=? AND sent_at>?",
                (principal.world_id, key, now-self.window_seconds),
            ).fetchone()[0]
            if count >= self.message_limit:
                raise ServiceError("rate_limited", "Message rate limit exceeded", 429)
        conn.executemany(
            "INSERT INTO social_rate_events(world_id,owner_key,sent_at) VALUES(?,?,?)",
            [(principal.world_id, key, now) for key in owner_keys],
        )

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        company_ids = [] if row["scope"] in ("world", "direct_identity") else [int(row["company_a"])]
        if row["scope"] == "direct_company":
            company_ids.append(int(row["company_b"]))
        return {"id": row["id"], "message_id": row["id"], "thread_id": row["thread_id"],
                "world_id": row["world_id"], "scope": row["scope"],
                "recipient_company_ids": company_ids,
                "recipient_identity_ids": ([row["company_a"][9:], row["company_b"][9:]]
                                           if row["scope"] == "direct_identity" else []),
                "sender": {
                    "identity_id": row["sender_identity_id"],
                    "company_id": (None if row["sender_company_id"] is None or
                                   int(row["sender_company_id"]) == Social.SPECTATOR_COMPANY
                                   else int(row["sender_company_id"])),
                    "kind": row["sender_kind"], "display_name": row["sender_display_name"]},
                "recipients": json.loads(row["recipients_json"]), "body": row["body"],
                "timestamp": row["timestamp"], "tick": row["tick"],
                "tick_authoritative": bool(row["tick_authoritative"])}

    def _message_by_id(self, message_id: str) -> sqlite3.Row | None:
        return self.store.connection.execute(
            """SELECT m.*,t.scope,t.company_a,t.company_b FROM social_messages m JOIN social_threads t ON t.id=m.thread_id
               WHERE m.id=?""", (message_id,)
        ).fetchone()

    def send(self, principal: Any, thread_id: str, body: str, request_id: str, tick: int = 0) -> dict[str, Any]:
        return self._submit(principal, thread_id, body, request_id, tick)

    def _submit(self, principal: Any, thread_id: str, body: str, request_id: str,
                tick: int = 0, *, message_id: str | None = None,
                tick_authoritative: bool = False,
                native_receipt: tuple[str, str] | None = None) -> dict[str, Any]:
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > self.max_message_bytes:
            raise ServiceError("invalid_message", "Invalid message", 400)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ServiceError("invalid_request", "Invalid request", 400)
        if type(tick) is not int or tick < 0:
            raise ServiceError("invalid_message", "Invalid message", 400)
        with self.store.lock:
            principal = self._current(principal)
            if len(principal.display_name.encode("utf-8")) > self.ENGINE_MAX_LABEL_BYTES:
                raise ServiceError("invalid_message", "Invalid message", 400)
            thread = self._get_thread(principal, thread_id)
            conn = self.store.connection
            existing = conn.execute(
                """SELECT m.*,t.scope,t.company_a,t.company_b FROM social_messages m JOIN social_threads t ON t.id=m.thread_id
                   WHERE m.world_id=? AND m.sender_identity_id=? AND m.request_id=?""",
                (principal.world_id, principal.identity_id, request_id),
            ).fetchone()
            if existing is not None:
                if (existing["thread_id"] != thread_id or existing["body"] != body
                        or (message_id is not None and existing["id"] != message_id)):
                    raise ServiceError("request_conflict", "Request conflict", 409)
                return self._message_dict(existing)

            def persist() -> dict[str, Any]:
                recipients = self._filter_blocked(principal, thread, self._recipients(principal, thread))
                self._check_backlog(principal)
                self._consume_rate(principal)
                new_message_id = message_id or str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO social_messages(id,world_id,thread_id,sender_identity_id,sender_company_id,
                       sender_kind,sender_display_name,body,timestamp,tick,tick_authoritative,recipients_json,request_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_message_id, principal.world_id, thread_id, principal.identity_id,
                     principal.company_id, principal.kind, principal.display_name, body,
                     _utc_now(), tick, int(tick_authoritative),
                     json.dumps(recipients, separators=(",", ":")), request_id),
                )
                conn.executemany(
                    "INSERT INTO social_message_recipients(message_id,identity_id,company_id) VALUES(?,?,?)",
                    [(new_message_id, r["identity_id"], r["company_id"]) for r in recipients],
                )
                memberships = {(r["identity_id"], r["company_id"]) for r in recipients}
                native_rows = conn.execute(
                    """SELECT public_key,identity_id,company_id FROM social_native_roster
                       WHERE world_id=? AND active=1""", (principal.world_id,),
                ).fetchall()
                conn.executemany(
                    "INSERT INTO social_message_native_recipients VALUES(?,?,?,?)",
                    [(new_message_id, r["public_key"], r["identity_id"], r["company_id"])
                     for r in native_rows if (r["identity_id"], r["company_id"]) in memberships],
                )
                message = self._message_dict(self._message_by_id(new_message_id))
                conn.execute(
                    "INSERT INTO social_delivery_outbox(message_id,world_id,payload,status,updated_at) VALUES(?,?,?,?,?)",
                    (new_message_id, principal.world_id, json.dumps(message, separators=(",", ":")),
                     "pending", _utc_now()),
                )
                if native_receipt is not None:
                    conn.execute(
                        "INSERT INTO social_native_receipts VALUES(?,?,?,?,?,?)",
                        (principal.world_id, native_receipt[0], new_message_id,
                         native_receipt[1], "accepted", None),
                    )
                muted = {r[0] for r in conn.execute(
                    "SELECT identity_id FROM social_mutes WHERE world_id=? AND thread_id=?",
                    (principal.world_id, thread_id),
                ).fetchall()}
                for member in self.store.principals_for_world(principal.world_id):
                    if ((member.identity_id, member.company_id) in memberships
                            and member.identity_id not in muted
                            and self._eligible_run(member, thread)):
                        self.store.append_event_in_transaction(conn, member, "chat.message", message)
                return message

            return self._transaction(conn, persist)

    def pending_deliveries(self, world_id: str, limit: int = 16) -> list[dict[str, Any]]:
        """Return only work safe to replay to an engine that deduplicates message IDs."""
        if not isinstance(world_id, str) or not world_id or type(limit) is not int or not 1 <= limit <= 200:
            raise ServiceError("invalid_delivery", "Invalid delivery query", 400)
        with self.store.lock:
            rows = self.store.connection.execute(
                """SELECT message_id,payload,status FROM social_delivery_outbox
                   WHERE world_id=? AND status='pending' ORDER BY updated_at,message_id LIMIT ?""",
                (world_id, limit),
            ).fetchall()
            return [{"message_id": row["message_id"], "world_id": world_id,
                     "status": row["status"], "payload": json.loads(row["payload"])} for row in rows]

    def mark_delivered(self, message_id: str, tick: int | None = None) -> None:
        if not isinstance(message_id, str) or not message_id or (
                tick is not None and (type(tick) is not int or tick < 0)):
            raise ServiceError("invalid_delivery", "Invalid delivery acknowledgement", 400)
        with self.store.lock:
            conn = self.store.connection
            def acknowledge() -> None:
                outbox = conn.execute(
                    "SELECT * FROM social_delivery_outbox WHERE message_id=?", (message_id,)
                ).fetchone()
                if outbox is None:
                    raise ServiceError("message_unavailable", "Message unavailable", 404)
                row = self._message_by_id(message_id)
                native_origin = row["request_id"].startswith("native:")
                if tick is not None and row["tick_authoritative"] and row["tick"] != tick and not native_origin:
                    raise ServiceError("delivery_conflict", "Delivery acknowledgement conflicts", 409)
                if outbox["status"] == "delivered":
                    if tick is None or row["tick_authoritative"]:
                        return
                if tick is not None and not row["tick_authoritative"]:
                    conn.execute(
                        "UPDATE social_messages SET tick=?,tick_authoritative=1 WHERE id=?",
                        (tick, message_id),
                    )
                conn.execute(
                    "UPDATE social_delivery_outbox SET status='delivered',updated_at=? WHERE message_id=?",
                    (_utc_now(), message_id),
                )
                if tick is None or native_origin:
                    return
                thread = conn.execute("SELECT * FROM social_threads WHERE id=?", (row["thread_id"],)).fetchone()
                recipient_memberships = {(r[0], r[1]) for r in conn.execute(
                    "SELECT identity_id,company_id FROM social_message_recipients WHERE message_id=?", (message_id,)
                )}
                muted = {r[0] for r in conn.execute(
                    "SELECT identity_id FROM social_mutes WHERE world_id=? AND thread_id=?",
                    (row["world_id"], row["thread_id"]),
                )}
                for member in self.store.principals_for_world(row["world_id"]):
                    if ((member.identity_id, member.company_id) in recipient_memberships
                            and member.identity_id not in muted
                            and self._eligible_run(member, thread)):
                        self.store.append_event_in_transaction(conn, member, "chat.message.tick",
                            {"message_id": message_id, "thread_id": row["thread_id"], "tick": tick})
            self._transaction(conn, acknowledge)

    def mark_uncertain(self, message_id: str) -> None:
        """Fence a delivery whose engine outcome is unknown until it is reconciled."""
        with self.store.lock:
            conn = self.store.connection
            def fence() -> None:
                row = conn.execute(
                    "SELECT status FROM social_delivery_outbox WHERE message_id=?", (message_id,)
                ).fetchone()
                if row is None:
                    raise ServiceError("message_unavailable", "Message unavailable", 404)
                if row["status"] == "pending":
                    conn.execute(
                        "UPDATE social_delivery_outbox SET status='uncertain',updated_at=? WHERE message_id=?",
                        (_utc_now(), message_id),
                    )
            self._transaction(conn, fence)

    @classmethod
    def normalize_native_roster(cls, roster: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Choose one stable social identity per key from connected sessions.

        A key may have several native connections. The lowest remote ClientID
        supplies its public label; a local-server row without a handle sorts
        last. Conflicting company assignments become spectator membership.
        """
        if not isinstance(roster, list):
            raise ServiceError("invalid_roster", "Invalid native roster", 400)
        groups = {}
        for entry in roster:
            if not isinstance(entry, dict):
                raise ServiceError("invalid_roster", "Invalid native roster", 400)
            wire_key = entry.get("public_key")
            key = wire_key.lower() if isinstance(wire_key, str) else wire_key
            company = entry.get("company_id", cls.SPECTATOR_COMPANY)
            if company is None:
                company = cls.SPECTATOR_COMPANY
            label = entry.get("label") or entry.get("display_name")
            handle = entry.get("native_handle")
            if (not isinstance(key, str) or not 1 <= len(key) <= 256
                    or type(company) is not int or not (0 <= company <= 14 or company == 255)
                    or not isinstance(label, str) or not label.strip()
                    or len(label.encode("utf-8")) > cls.ENGINE_MAX_LABEL_BYTES
                    or entry.get("kind", "human") != "human"):
                raise ServiceError("invalid_roster", "Invalid native roster", 400)
            if handle is not None and (not isinstance(handle, dict) or
                    set(handle) != {"id", "generation", "engine_instance_id"} or
                    type(handle["id"]) is not int or not 0 <= handle["id"] <= 0xFFFFFFFF or
                    type(handle["generation"]) is not int or
                    not 1 <= handle["generation"] <= 0xFFFFFFFFFFFFFFFF or
                    not isinstance(handle["engine_instance_id"], str) or
                    not 1 <= len(handle["engine_instance_id"]) <= 256):
                raise ServiceError("invalid_roster", "Invalid native roster", 400)
            groups.setdefault(key, []).append((wire_key, company, label, handle))
        normalized = []
        for key, entries in sorted(groups.items()):
            wire_key, _, label, handle = min(
                entries, key=lambda item: (item[3] is None,
                                           item[3]["id"] if item[3] is not None else 0,
                                           item[2], item[0]))
            companies = {item[1] for item in entries}
            company = next(iter(companies)) if len(companies) == 1 else cls.SPECTATOR_COMPANY
            row = {"public_key": wire_key, "company_id": company,
                   "label": label, "kind": "human"}
            if handle is not None:
                row["native_handle"] = dict(handle)
            normalized.append(row)
        return normalized

    def update_native_roster(self, world_id: str, roster: list[dict[str, Any]]) -> None:
        """Replace trusted native social membership, coalescing duplicate keys."""
        if not isinstance(world_id, str) or not world_id:
            raise ServiceError("invalid_roster", "Invalid native roster", 400)
        prepared = []
        for entry in self.normalize_native_roster(roster):
            wire_key = entry["public_key"]
            key = wire_key.lower()
            company = entry["company_id"]
            label = entry["label"]
            identity = self.native_identity_id(world_id, key)
            prepared.append((key, wire_key, identity, company, label))
        with self.store.lock:
            conn = self.store.connection
            def update() -> None:
                conn.execute("INSERT OR IGNORE INTO worlds(id,epoch) VALUES(?,?)", (world_id, "native"))
                previous = {row["public_key"]: row for row in conn.execute(
                    "SELECT * FROM social_native_roster WHERE world_id=?", (world_id,)
                )}
                for row in previous.values():
                    conn.execute("UPDATE runs SET active=0 WHERE id=?", (row["run_id"],))
                    conn.execute("UPDATE memberships SET active=0 WHERE world_id=? AND identity_id=? AND company_id=?",
                                 (world_id, row["identity_id"], row["company_id"]))
                conn.execute("UPDATE social_native_roster SET active=0 WHERE world_id=?", (world_id,))
                for key, wire_key, identity, company, label in prepared:
                    old = previous.get(key)
                    run_id = old["run_id"] if old is not None and old["company_id"] == company else secrets.token_hex(16)
                    conn.execute("INSERT OR IGNORE INTO identities(id) VALUES(?)", (identity,))
                    conn.execute(
                        "INSERT INTO memberships(world_id,identity_id,company_id,active) VALUES(?,?,?,1) "
                        "ON CONFLICT(world_id,identity_id,company_id) DO UPDATE SET active=1",
                        (world_id, identity, company),
                    )
                    if old is not None and old["company_id"] == company:
                        conn.execute("UPDATE runs SET active=1,display_name=? WHERE id=?", (label, run_id))
                    else:
                        conn.execute(
                            """INSERT INTO runs(id,world_id,identity_id,company_id,mode,kind,display_name,
                               cursor_secret,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (run_id, world_id, identity, company, "native", "human", label,
                             secrets.token_hex(32), time.time()),
                        )
                    conn.execute(
                        """INSERT INTO social_native_roster(world_id,public_key,wire_public_key,
                           identity_id,company_id,run_id,display_name,active) VALUES(?,?,?,?,?,?,?,1)
                           ON CONFLICT(world_id,public_key) DO UPDATE SET
                           wire_public_key=excluded.wire_public_key,
                           identity_id=excluded.identity_id,company_id=excluded.company_id,
                           run_id=excluded.run_id,display_name=excluded.display_name,active=1""",
                        (world_id, key, wire_key, identity, company, run_id, label),
                    )
            self._transaction(conn, update)

    def native_memberships_for_keys(self, world_id: str, keys: Any) -> set[tuple[str, int]]:
        """Resolve verified renderer keys to active native roster memberships.

        This is an internal read-only lookup for the participant projection. It
        does not change chat recipients or expose public keys to API callers.
        """
        if not isinstance(world_id, str) or not world_id or not isinstance(
                keys, (set, frozenset, list, tuple)):
            raise ServiceError("invalid_native_keys", "Invalid native keys", 400)
        canonical = {key.lower() for key in keys if isinstance(key, str)
                     and self._PUBLIC_KEY.fullmatch(key)}
        if len(canonical) > 1024:
            raise ServiceError("invalid_native_keys", "Invalid native keys", 400)
        if not canonical:
            return set()
        with self.store.lock:
            memberships = set()
            ordered = sorted(canonical)
            for start in range(0, len(ordered), 256):
                chunk = ordered[start:start + 256]
                rows = self.store.connection.execute(
                    """SELECT identity_id,company_id FROM social_native_roster
                       WHERE world_id=? AND active=1 AND public_key IN ("""
                    + ",".join("?" for _ in chunk) + ")",
                    (world_id, *chunk),
                ).fetchall()
                memberships.update((row["identity_id"], row["company_id"]) for row in rows)
            return memberships

    def admit_native(self, world_id: str, pending: dict[str, Any]) -> dict[str, Any]:
        """Admit one pre-wire chat.pending from the trusted engine connection."""
        if not isinstance(world_id, str) or not world_id or not isinstance(pending, dict):
            raise ServiceError("invalid_native_chat", "Invalid native chat pending", 400)
        event_id, message_id = pending.get("event_id"), pending.get("message_id")
        if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in (event_id, message_id)):
            raise ServiceError("invalid_native_chat", "Invalid native chat pending", 400)
        sender = pending.get("sender")
        body, tick, scope = pending.get("body"), pending.get("tick"), pending.get("scope")
        sender_key = sender.get("public_key") if isinstance(sender, dict) else None
        if isinstance(sender_key, str):
            sender_key = sender_key.lower()
        sender_company = sender.get("company_id") if isinstance(sender, dict) else None
        if sender_company is None:
            sender_company = self.SPECTATOR_COMPANY
        target_key = pending.get("target_public_key")
        if isinstance(target_key, str):
            target_key = target_key.lower()
        valid = (pending.get("type", "chat.pending") == "chat.pending"
                 and isinstance(sender_key, str) and type(sender_company) is int
                 and isinstance(body, str) and bool(body.strip())
                 and len(body.encode("utf-8")) <= self.max_message_bytes
                 and type(tick) is int and tick >= 0
                 and scope in ("world", "company", "direct_company", "direct_identity"))
        digest_fields = {"message_id": message_id, "sender_public_key": sender_key,
                         "sender_company_id": sender_company, "scope": scope,
                         "body": body, "tick": tick,
                         "target_company_id": pending.get("target_company_id"),
                         "target_public_key": target_key}
        try:
            digest = hashlib.sha256(json.dumps(digest_fields, sort_keys=True, separators=(",", ":"),
                                               allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError) as exc:
            raise ServiceError("invalid_native_chat", "Invalid native chat pending", 400) from exc
        with self.store.lock:
            conn = self.store.connection
            receipt = conn.execute(
                "SELECT * FROM social_native_receipts WHERE world_id=? AND (event_id=? OR message_id=?)",
                (world_id, event_id, message_id),
            ).fetchone()
            if receipt is not None:
                if receipt["digest"] != digest or receipt["message_id"] != message_id:
                    return {"event_id": event_id, "message_id": message_id,
                            "disposition": "rejected", "reason": "request_conflict"}
                result = {"event_id": event_id, "message_id": message_id,
                          "disposition": receipt["disposition"], "reason": receipt["reason"]}
                if receipt["disposition"] == "accepted":
                    result["thread_id"] = self._message_by_id(message_id)["thread_id"]
                return result
            if self._message_by_id(message_id) is not None:
                return {"event_id": event_id, "message_id": message_id,
                        "disposition": "rejected", "reason": "request_conflict"}
            if not valid:
                self._transaction(conn, lambda: conn.execute(
                    "INSERT INTO social_native_receipts VALUES(?,?,?,?,?,?)",
                    (world_id, event_id, message_id, digest, "rejected", "unavailable"),
                ))
                return {"event_id": event_id, "message_id": message_id,
                        "disposition": "rejected", "reason": "unavailable"}
            try:
                native = conn.execute(
                    """SELECT * FROM social_native_roster WHERE world_id=? AND public_key=?
                       AND company_id=? AND active=1""",
                    (world_id, sender_key, sender_company),
                ).fetchone()
                if native is None:
                    raise ServiceError("sender_unavailable", "Sender unavailable", 403)
                principal = self.store.get_principal(native["run_id"])
                if principal is None:
                    raise ServiceError("sender_unavailable", "Sender unavailable", 403)
                self.store.validate_principal(principal)
                if principal.company_id == self.SPECTATOR_COMPANY and scope != "world":
                    raise ServiceError("scope_unavailable", "Scope unavailable", 403)
                if scope == "world":
                    if pending.get("target_company_id") is not None or target_key is not None:
                        raise ServiceError("scope_unavailable", "Scope unavailable", 403)
                    thread_id = self.create_thread(principal, "world")["id"]
                elif scope == "company":
                    if pending.get("target_company_id") not in (None, principal.company_id):
                        raise ServiceError("scope_unavailable", "Scope unavailable", 403)
                    if target_key is not None:
                        raise ServiceError("scope_unavailable", "Scope unavailable", 403)
                    thread_id = self.create_thread(principal, "company")["id"]
                elif scope == "direct_company":
                    other = pending.get("target_company_id")
                    if (type(other) is not int or other == principal.company_id or other == 255
                            or target_key is not None):
                        raise ServiceError("scope_unavailable", "Scope unavailable", 403)
                    thread_id = self.create_thread(principal, "direct_company",
                                                   [principal.company_id, other])["id"]
                else:
                    if (not isinstance(target_key, str) or target_key == sender_key
                            or pending.get("target_company_id") is not None):
                        raise ServiceError("scope_unavailable", "Scope unavailable", 403)
                    target = conn.execute(
                        """SELECT * FROM social_native_roster WHERE world_id=? AND public_key=?
                           AND active=1""", (world_id, target_key),
                    ).fetchone()
                    if target is None or target["company_id"] == 255:
                        raise ServiceError("scope_unavailable", "Scope unavailable", 403)
                    endpoints = sorted(("identity:" + principal.identity_id,
                                        "identity:" + target["identity_id"]))
                    thread = self._transaction(conn, lambda: self._ensure_thread(
                        world_id, "direct_identity", endpoints[0], endpoints[1]))
                    thread_id = thread["id"]
                message = self._submit(principal, thread_id, body, "native:" + message_id,
                                       tick, message_id=message_id, tick_authoritative=True,
                                       native_receipt=(event_id, digest))
                return {"event_id": event_id, "message_id": message_id,
                        "disposition": "accepted", "reason": None, "thread_id": message["thread_id"]}
            except ServiceError:
                self._transaction(conn, lambda: conn.execute(
                    "INSERT INTO social_native_receipts VALUES(?,?,?,?,?,?)",
                    (world_id, event_id, message_id, digest, "rejected", "unavailable"),
                ))
                return {"event_id": event_id, "message_id": message_id,
                        "disposition": "rejected", "reason": "unavailable"}

    def native_delivery(self, message: str | dict[str, Any]) -> dict[str, Any]:
        """Build exact native targets from the immutable admission snapshot."""
        message_id = message if isinstance(message, str) else message.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ServiceError("invalid_delivery", "Invalid native delivery", 400)
        with self.store.lock:
            row = self._message_by_id(message_id)
            if row is None:
                raise ServiceError("message_unavailable", "Message unavailable", 404)
            keys = self.store.connection.execute(
                """SELECT r.wire_public_key FROM social_message_native_recipients n
                   JOIN social_native_roster r ON r.world_id=? AND r.public_key=n.public_key
                     AND r.identity_id=n.identity_id AND r.company_id=n.company_id AND r.active=1
                   LEFT JOIN social_blocks b ON b.world_id=? AND b.blocker_identity_id=n.identity_id
                     AND b.blocked_identity_id=?
                   WHERE n.message_id=? AND b.blocker_identity_id IS NULL ORDER BY r.wire_public_key""",
                (row["world_id"], row["world_id"], row["sender_identity_id"], message_id),
            ).fetchall()
            return {"message_id": message_id, "delivery_request_id": message_id,
                    "world_id": row["world_id"], "thread_id": row["thread_id"],
                    "scope": row["scope"], "body": row["body"], "timestamp": row["timestamp"],
                    "tick": row["tick"], "sender": {"label": row["sender_display_name"],
                    "kind": row["sender_kind"],
                    "company_id": (None if row["sender_company_id"] is None or
                                   int(row["sender_company_id"]) == self.SPECTATOR_COMPANY
                                   else int(row["sender_company_id"]))},
                    "recipient_public_keys": [item[0] for item in keys]}

    def history(self, principal: Any, thread_id: str, after: str | None = None,
                limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ServiceError("invalid_limit", "Invalid limit", 400)
        with self.store.lock:
            principal = self._current(principal)
            self._get_thread(principal, thread_id)
            seq = 0
            if after is not None:
                cursor = self.store.connection.execute(
                    """SELECT m.seq FROM social_messages m JOIN social_message_recipients r
                       ON r.message_id=m.id WHERE m.id=? AND m.thread_id=? AND r.identity_id=? AND r.company_id=?""",
                    (after, thread_id, principal.identity_id, principal.company_id),
                ).fetchone()
                if cursor is None:
                    raise ServiceError("history_unavailable", "History unavailable", 404)
                seq = cursor[0]
            rows = self.store.connection.execute(
                """SELECT m.*,t.scope,t.company_a,t.company_b FROM social_messages m JOIN social_threads t ON t.id=m.thread_id
                   JOIN social_message_recipients r ON r.message_id=m.id
                   WHERE m.thread_id=? AND m.seq>? AND r.identity_id=? AND r.company_id=? ORDER BY m.seq LIMIT ?""",
                (thread_id, seq, principal.identity_id, principal.company_id, limit),
            ).fetchall()
            messages = [self._message_dict(row) for row in rows]
            return {"thread_id": thread_id, "messages": messages,
                    "next_after": messages[-1]["message_id"] if messages else after}

    def block(self, principal: Any, identity_id: str, blocked: bool = True) -> dict[str, Any]:
        if not isinstance(identity_id, str) or not identity_id:
            raise ServiceError("identity_unavailable", "Identity unavailable", 404)
        with self.store.lock:
            principal = self._current(principal)
            if identity_id == principal.identity_id:
                raise ServiceError("identity_unavailable", "Identity unavailable", 404)
            if identity_id not in {member.identity_id for member in
                                   self.store.principals_for_world(principal.world_id)}:
                raise ServiceError("identity_unavailable", "Identity unavailable", 404)
            def update() -> None:
                if blocked:
                    self.store.connection.execute(
                        "INSERT OR IGNORE INTO social_blocks VALUES(?,?,?)",
                        (principal.world_id, principal.identity_id, identity_id),
                    )
                else:
                    self.store.connection.execute(
                        "DELETE FROM social_blocks WHERE world_id=? AND blocker_identity_id=? AND blocked_identity_id=?",
                        (principal.world_id, principal.identity_id, identity_id),
                    )
            self._transaction(self.store.connection, update)
            return {"identity_id": identity_id, "blocked": bool(blocked)}

    def mute(self, principal: Any, thread_id: str, muted: bool = True) -> dict[str, Any]:
        with self.store.lock:
            principal = self._current(principal)
            self._get_thread(principal, thread_id)
            def update() -> None:
                if muted:
                    self.store.connection.execute(
                        "INSERT OR IGNORE INTO social_mutes VALUES(?,?,?)",
                        (principal.world_id, principal.identity_id, thread_id),
                    )
                else:
                    self.store.connection.execute(
                        "DELETE FROM social_mutes WHERE world_id=? AND identity_id=? AND thread_id=?",
                        (principal.world_id, principal.identity_id, thread_id),
                    )
            self._transaction(self.store.connection, update)
            return {"thread_id": thread_id, "muted": bool(muted)}

    def report(self, principal: Any, message_id: str, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 1024:
            raise ServiceError("invalid_report", "Invalid report", 400)
        with self.store.lock:
            principal = self._current(principal)
            row = self._message_by_id(message_id)
            if row is None or row["world_id"] != principal.world_id:
                raise ServiceError("message_unavailable", "Message unavailable", 404)
            self._get_thread(principal, row["thread_id"])
            recipient = self.store.connection.execute(
                "SELECT 1 FROM social_message_recipients WHERE message_id=? AND identity_id=? AND company_id=?",
                (message_id, principal.identity_id, principal.company_id),
            ).fetchone()
            if recipient is None:
                raise ServiceError("message_unavailable", "Message unavailable", 404)
            report_id = str(uuid.uuid4())
            self._transaction(self.store.connection, lambda: self.store.connection.execute(
                "INSERT INTO social_reports VALUES(?,?,?,?,?,?)",
                (report_id, principal.world_id, message_id, principal.identity_id, reason, _utc_now()),
            ))
            return {"report_id": report_id, "status": "received"}
