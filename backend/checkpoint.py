"""Process-local policy for scheduling one world's durable checkpoints.

The broker owns RPC, admission, and persistence. This object only decides when
to quiesce bridge dispatch and retry a checkpoint; a create receipt never
counts as a durable save.
"""

import math
import time


EARLY_WINDOW = 7500  # Leave room below the engine's 8192 unretired entries.


class CheckpointPolicy:
    def __init__(self, interval=30, retry_min=1, retry_max=30,
                 failure_grace=120, clock=time.monotonic):
        if (not callable(clock) or any(not isinstance(value, (int, float))
                or isinstance(value, bool) or not math.isfinite(value) or value <= 0
                for value in (interval, retry_min, retry_max, failure_grace))
                or retry_min > retry_max):
            raise ValueError("Checkpoint intervals must be positive and bounded")
        self.interval = float(interval)
        self.retry_min = float(retry_min)
        self.retry_max = float(retry_max)
        self.failure_grace = float(failure_grace)
        self.clock = clock
        now = clock()
        self._started_at = now
        self._last_success_at = None
        self._completed_id = None
        self._next_due_at = now + self.interval
        self._retry_at = now
        self._retry_delay = self.retry_min
        self._quiescing = False
        self._saving = False
        self._last_state = None

    @property
    def quiescing(self):
        return self._quiescing

    @property
    def saving(self):
        return self._saving

    @property
    def admission_blocked(self):
        """A stale save requires operator attention and bounded admission."""
        reference = self._last_success_at if self._last_success_at is not None else self._started_at
        return self.clock() - reference >= self.failure_grace

    @property
    def status(self):
        """Coarse operator metrics; no save path, receipt, or public game data."""
        now = self.clock()
        return {"saving": self._saving, "quiescing": self._quiescing,
                "overdue": now >= self._next_due_at,
                "last_success_age": None if self._last_success_at is None else max(0.0, now - self._last_success_at)}

    def observe(self, state):
        """Observe the flat ``checkpoint.status`` result from the engine."""
        if not isinstance(state, dict):
            raise ValueError("Checkpoint status must be an object")
        status = state.get("status")
        checkpoint_id = state.get("checkpoint_id")
        if status not in ("idle", "saving", "completed", "failed"):
            raise ValueError("Unknown checkpoint status")
        if status == "completed":
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                self.failed()
            elif checkpoint_id != self._completed_id:
                now = self.clock()
                self._completed_id = checkpoint_id
                self._last_success_at = now
                self._next_due_at = now + self.interval
                self._retry_at = now
                self._retry_delay = self.retry_min
                self._quiescing = False
                self._saving = False
        elif status == "saving":
            self._quiescing = True
            self._saving = True
        elif status == "failed" and self._last_state != "failed":
            self._saving = False
            self.failed()
        self._last_state = status

    def due(self, unretired_window=0):
        """Enter quiescence on a timer or before the engine dedupe window fills."""
        if type(unretired_window) is not int or unretired_window < 0:
            raise ValueError("Unretired window must be nonnegative")
        if self.clock() >= self._next_due_at or unretired_window >= EARLY_WINDOW:
            self._quiescing = True
        return self._quiescing

    def should_create(self, queued_count):
        """The broker must first drain already queued bridge mutations."""
        if type(queued_count) is not int or queued_count < 0:
            raise ValueError("Queued count must be nonnegative")
        return self._quiescing and not self._saving and queued_count == 0 and self.clock() >= self._retry_at

    def created(self, reply_status):
        """A create receipt starts waiting; it is never proof of durability."""
        if reply_status in ("queued", "completed", "ok"):
            self._quiescing = True
            self._saving = True
            self._last_state = "saving"
            return
        self.failed()

    def failed(self):
        """Retry transport, busy, or save failure with bounded exponential delay."""
        self._quiescing = True
        self._saving = False
        now = self.clock()
        self._retry_at = now + self._retry_delay
        self._retry_delay = min(self.retry_max, self._retry_delay * 2)
        self._last_state = "failed"
