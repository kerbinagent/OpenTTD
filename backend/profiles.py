"""Metadata-only adapter for an already-running world's database.

This temporary companion reuses the canonical authentication and model report
service without opening an engine connection or starting a second broker.
"""

from .asgi import _error, create_app as create_full_app


_ROUTES = frozenset({
    ("GET", b"/v1/me"),
    ("GET", b"/v1/participants"),
    ("PUT", b"/v1/me/model"),
    ("DELETE", b"/v1/me/model"),
})


def create_app(service):
    """Filter before the full adapter's static, event, and image dispatch."""
    if service.engine is not None or service._thread is not None:
        raise ValueError("Metadata-only service must have no engine or broker")
    delegate = create_full_app(service)

    async def app(scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("raw_path", scope["path"].encode("utf-8"))
            if ((scope["method"], path) not in _ROUTES or
                    scope["path"].encode("utf-8") != path):
                await _error(404, "not_found", "Metadata endpoint not found")(scope, receive, send)
                return
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await delegate(scope, receive, send)

    return app
