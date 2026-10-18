"""Small canonical intent journal shared by public gameplay clients.

An explicit new intent gets a fresh ID. A retry loads its original request,
including all references and version tokens. A recovered receipt describes the
original execution; it is not evidence of the current state of its targets.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import uuid

from .json_tools import dumps, loads


MAX_REQUEST_BYTES = 64 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class SavedActions:
    """Journal inside a caller-owned artifact directory, not a filesystem sandbox."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, request_id):
        if not isinstance(request_id, str) or IDENTIFIER.fullmatch(request_id) is None:
            raise ValueError("Invalid request ID")
        return self.directory / (request_id + ".json")

    @staticmethod
    def _validate(body):
        if (not isinstance(body, dict) or set(body) - {"request_id", "action", "preconditions"}
                or not {"request_id", "action"} <= set(body)
                or not isinstance(body["action"], dict)
                or set(body["action"]) != {"name", "parameters"}
                or not isinstance(body["action"]["name"], str)
                or not isinstance(body["action"]["parameters"], dict)
                or not isinstance(body.get("preconditions", {}), dict)):
            raise ValueError("Invalid saved action request")

    def save(self, body):
        """Persist before sending; return whether this is a newly saved ID."""
        self._validate(body)
        path = self._path(body["request_id"])
        data = dumps(body).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Action request exceeds 64 KiB")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            # Comparing canonical bytes also distinguishes true from 1.
            if dumps(self.load(body["request_id"])).encode("utf-8") != data:
                raise ValueError("Request ID already has a different saved body") from None
            return False
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if os.name == "posix":
            directory_descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return True

    def load(self, request_id):
        """Return the original body, without refreshing any target or token."""
        descriptor = os.open(self._path(request_id), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Saved action request must be a regular file")
            data = source.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Saved action request exceeds 64 KiB")
        body = loads(data.decode("utf-8"))
        self._validate(body)
        if body["request_id"] != request_id:
            raise ValueError("Saved action request ID mismatch")
        return body

    def new_intent(self, name, parameters, *, preconditions=None):
        """Mint and save a fresh decision; this method does not send it."""
        body = {"request_id": uuid.uuid4().hex, "action": {"name": name, "parameters": parameters}}
        if preconditions is not None:
            body["preconditions"] = preconditions
        self.save(body)
        # Return the durable canonical copy, detached from caller mutations.
        return self.load(body["request_id"])
