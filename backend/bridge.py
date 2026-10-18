"""Private bounded Unix-domain RPC adapter; contains no public credentials."""

import socket
import threading

from .errors import ServiceError
from .json_tools import dumps, loads

MAX_MESSAGE_BYTES = 65536


class UnixEngine:
    """One bounded NDJSON request/reply per connection to a trusted local engine.

    Connection failures are safe to retry. Once any bytes may have been sent,
    failures are uncertain and must be reconciled by immutable request ID.
    """

    def __init__(self, socket_path, timeout=2.0, max_connections=8):
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(max_connections)

    def call(self, message):
        try:
            data = dumps(message).encode("utf-8") + b"\n"
        except (ValueError, TypeError, RecursionError):
            raise ServiceError("invalid_request", status=400) from None
        if len(data) > MAX_MESSAGE_BYTES:
            raise ServiceError("request_too_large", status=413)
        if not self.slots.acquire(blocking=False):
            raise ServiceError("engine_busy", status=503)
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                sent = True  # sendall may fail after writing only a prefix.
                connection.sendall(data)
                response = bytearray()
                while b"\n" not in response:
                    chunk = connection.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(response)))
                    if not chunk:
                        raise ValueError("Truncated engine reply")
                    response.extend(chunk)
                    if len(response) > MAX_MESSAGE_BYTES:
                        raise ValueError("Engine reply too large")
                line, trailing = bytes(response).split(b"\n", 1)
                if trailing.strip():
                    raise ValueError("Unexpected engine messages")
                result = loads(line.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("Invalid engine reply")
                if result.get("protocol_version") != 1 or result.get("correlation_id") != message.get("correlation_id"):
                    raise ValueError("Engine reply correlation mismatch")
                return result
        except (OSError, ValueError, UnicodeError, RecursionError):
            code = "engine_uncertain" if sent else "engine_unavailable"
            raise ServiceError(code, status=503) from None
        finally:
            self.slots.release()
