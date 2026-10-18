"""Wire-level checks for the bounded Unix engine adapter."""

import json
import os
import socket
import tempfile
import threading
import unittest

from backend.bridge import MAX_MESSAGE_BYTES, UnixEngine
from backend.errors import ServiceError


class OneReplyServer:
    def __init__(self, path, respond):
        self.path = path
        self.respond = respond
        self.received = None
        self.failure = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(2):
            raise AssertionError("Unix fixture did not start")
        return self

    def __exit__(self, *_):
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("Unix fixture did not finish")
        if self.failure:
            raise self.failure

    def _serve(self):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(self.path)
                server.listen(1)
                server.settimeout(2)
                self.ready.set()
                with server.accept()[0] as peer:
                    peer.settimeout(2)
                    data = bytearray()
                    while b"\n" not in data:
                        chunk = peer.recv(4096)
                        if not chunk:
                            break
                        data.extend(chunk)
                    self.received = json.loads(bytes(data).split(b"\n", 1)[0])
                    reply = self.respond(self.received)
                    if reply is not None:
                        peer.sendall(reply)
        except BaseException as exc:
            self.failure = exc
            self.ready.set()


class UnixEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "engine.sock")
        self.message = {"protocol_version": 1, "correlation_id": "request-1", "type": "hello",
                        "world_id": "world-a", "world_epoch": "initial"}

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def reply(request, **changes):
        value = {"protocol_version": 1, "correlation_id": request["correlation_id"],
                 "type": request["type"] + ".result", "status": "completed", "result": {}}
        value.update(changes)
        return json.dumps(value).encode() + b"\n"

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_round_trip_preserves_envelope_and_correlation(self):
        with OneReplyServer(self.path, self.reply) as fixture:
            result = UnixEngine(self.path).call(self.message)
        self.assertEqual(fixture.received, self.message)
        self.assertEqual(result["correlation_id"], "request-1")

    def test_reply_with_wrong_correlation_or_protocol_is_uncertain(self):
        for changes in ({"correlation_id": "someone-else"}, {"protocol_version": 2}):
            with self.subTest(changes=changes):
                with OneReplyServer(self.path, lambda request: self.reply(request, **changes)):
                    self.assert_code("engine_uncertain", lambda: UnixEngine(self.path).call(self.message))
                os.unlink(self.path)

    def test_truncated_and_oversized_replies_are_uncertain(self):
        for reply in (b'{"protocol_version":1', b"x" * (MAX_MESSAGE_BYTES + 1)):
            with self.subTest(length=len(reply)):
                with OneReplyServer(self.path, lambda _request: reply):
                    self.assert_code("engine_uncertain", lambda: UnixEngine(self.path).call(self.message))
                os.unlink(self.path)

    def test_request_size_rejected_before_connect(self):
        oversized = dict(self.message, payload="x" * MAX_MESSAGE_BYTES)
        self.assert_code("request_too_large", lambda: UnixEngine(self.path).call(oversized))

    def test_disconnect_before_connect_is_unavailable(self):
        self.assert_code("engine_unavailable", lambda: UnixEngine(self.path).call(self.message))


if __name__ == "__main__":
    unittest.main()
