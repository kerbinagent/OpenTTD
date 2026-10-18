"""Operator entry point: ``python -m backend``."""

from __future__ import annotations

import argparse
import math
import os
import re
import stat
import sys
from pathlib import Path

from .json_tools import loads
from .errors import ServiceError


def _company(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 14:
        raise argparse.ArgumentTypeError("company must be between 0 and 14")
    return number


def _bounded_decimal(name: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        if not re.fullmatch(r"[1-9][0-9]*", value) or len(value) > len(str(maximum)):
            raise argparse.ArgumentTypeError(f"{name} must be a decimal integer from {minimum} to {maximum}")
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"{name} must be from {minimum} to {maximum}")
        return number
    return parse


def _capture_interval(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("capture interval must be a finite number from 1 to 60 seconds") from None
    if not math.isfinite(number) or not 1 <= number <= 60:
        raise argparse.ArgumentTypeError("capture interval must be a finite number from 1 to 60 seconds")
    return number


def _reserve_token_file(path: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, 0o600)


def _canonical_path(value: str, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Renderer paths must be nonempty strings")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Renderer paths must be canonical absolute paths")
    if directory and not path.is_dir():
        raise ValueError("Renderer artifact directory must already exist")
    return str(path)


def _renderer_config(path: str, store, world_id: str) -> dict:
    """Read a private operator map; no public request can select these paths."""
    _canonical_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077 or info.st_size > 32768):
            raise ValueError("Renderer config must be an owner-only regular file of at most 32 KiB")
        raw = file.read(32769)
    if len(raw) > 32768:
        raise ValueError("Renderer config exceeds 32 KiB")
    try:
        value = loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Invalid renderer JSON config") from None
    if (not isinstance(value, dict) or set(value) != {"version", "world_id", "runs"} or
        type(value["version"]) is not int or value["version"] != 1 or value["world_id"] != world_id or
        not isinstance(value["runs"], dict) or not 1 <= len(value["runs"]) <= 16):
        raise ValueError("Invalid renderer config schema or world")
    workers = {}
    sockets, directories = set(), set()
    for run_id, settings in value["runs"].items():
        if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
            raise ValueError("Invalid renderer run ID")
        principal = store.get_principal(run_id)
        if principal is None or principal.world_id != world_id or principal.mode not in ("text", "visual"):
            raise ValueError("Renderer run must be active in this world and text or visual")
        if not isinstance(settings, dict) or set(settings) != {"socket", "artifacts"}:
            raise ValueError("Invalid renderer run settings")
        socket_path = _canonical_path(settings["socket"])
        artifacts = _canonical_path(settings["artifacts"], directory=True)
        if socket_path in sockets or artifacts in directories:
            raise ValueError("Renderer sockets and artifact directories must be unique")
        sockets.add(socket_path)
        directories.add(artifacts)
        workers[run_id] = {"socket": socket_path, "artifacts": artifacts}
    return workers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenTTD backend operator commands")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the authenticated API")
    serve.add_argument("--database", required=True)
    serve.add_argument("--world", required=True)
    serve.add_argument("--engine-socket", required=True)
    serve.add_argument("--renderer-socket", help="separate private renderer IPC socket")
    serve.add_argument("--renderer-run", help="run ID served by the renderer")
    serve.add_argument("--renderer-artifacts", help="trusted renderer PNG artifact directory")
    serve.add_argument("--renderer-config", help="private JSON map of renderer runs")
    serve.add_argument("--agent-join-pool", help="opt-in private map of preprovisioned companies available to public agent joins")
    serve.add_argument("--transport", choices=("asgi", "http"), default="asgi",
                       help="ASGI production adapter (default) or local-development HTTP")
    serve.add_argument("--max-pending-company", type=_bounded_decimal("company limit", 1, 128), default=64)
    serve.add_argument("--max-pending-world", type=_bounded_decimal("world limit", 1, 1024), default=1024)
    serve.add_argument("--capture-interval", type=_capture_interval, default=None,
                       help="minimum seconds between captures per renderer run (1 to 60)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    profiles = commands.add_parser("profiles", help="serve model metadata for an existing running world")
    profiles.add_argument("--database", required=True)
    profiles.add_argument("--world", required=True)
    profiles.add_argument("--port", type=_bounded_decimal("port", 1, 65535), default=8081)
    provision = commands.add_parser("provision", help="create one scoped run credential")
    provision.add_argument("--database", required=True)
    provision.add_argument("--world", required=True)
    provision.add_argument("--identity", required=True)
    provision.add_argument("--company", required=True, type=_company)
    provision.add_argument("--mode", choices=("text", "visual"), required=True)
    provision.add_argument("--kind", choices=("human", "agent"), required=True)
    provision.add_argument("--name", required=True)
    provision.add_argument("--epoch", default="initial")
    provision.add_argument("--token-file", help="new private file for the one-time credential")
    args = parser.parse_args(argv)
    if args.command == "profiles" and not Path(args.database).is_file():
        parser.error("--database must name the existing world's database")
    if args.command == "serve":
        renderer_options = (args.renderer_socket, args.renderer_run, args.renderer_artifacts)
        if args.renderer_config and any(renderer_options):
            parser.error("--renderer-config cannot be combined with individual renderer options")
        if any(renderer_options) and not all(renderer_options):
            parser.error("--renderer-run, --renderer-socket and --renderer-artifacts must be supplied together")
        if args.capture_interval is not None and not (args.renderer_config or all(renderer_options)):
            parser.error("--capture-interval requires a configured renderer")
        if args.renderer_artifacts:
            try:
                _canonical_path(args.renderer_artifacts, directory=True)
            except ValueError as exc:
                parser.error(str(exc))

    from .store import Store

    descriptor = _reserve_token_file(args.token_file) if args.command == "provision" and args.token_file else None
    try:
        store = (Store(args.database, max_pending_company=args.max_pending_company,
                       max_pending_world=args.max_pending_world) if args.command == "serve"
                 else Store(args.database))
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            os.unlink(args.token_file)
        raise
    try:
        if args.command == "provision":
            try:
                store.enforce_single_world(args.world)
                token = store.provision(
                    args.world, args.identity, args.company, mode=args.mode,
                    kind=args.kind, display_name=args.name, world_epoch=args.epoch,
                )
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                    os.unlink(args.token_file)
                raise
            if args.token_file:
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                        file.write(token + "\n")
                except OSError:
                    os.unlink(args.token_file)
                    print("Credential file write failed; credential follows on stdout", file=sys.stderr)
                    print(token)
                    return 1
                print(f"Credential written to {args.token_file}", file=sys.stderr)
            else:
                print(token)
            return 0

        if args.command == "profiles":
            from .service import Service
            from .profiles import create_app
            import uvicorn

            service = Service(store, None, autostart=False, world_id=args.world)
            try:
                print("Serving metadata only on loopback; existing game broker is unchanged.", file=sys.stderr)
                uvicorn.run(
                    create_app(service), host="127.0.0.1", port=args.port,
                    workers=1, loop="asyncio", http="h11", limit_concurrency=128,
                    timeout_keep_alive=5, timeout_graceful_shutdown=15,
                    proxy_headers=False, access_log=False, log_level="warning",
                )
            finally:
                service.close()
            return 0

        from .bridge import UnixEngine
        from .service import Service

        renderer = None
        try:
            store.enforce_single_world(args.world)
            if args.renderer_config or args.renderer_socket:
                from .renderer import RendererGateway
                if args.renderer_config:
                    try:
                        entries = _renderer_config(args.renderer_config, store, args.world)
                    except (OSError, ValueError) as exc:
                        parser.error(f"Invalid renderer config: {exc}")
                    workers = {
                        run_id: {"engine": UnixEngine(entry["socket"], timeout=15),
                                 "artifacts": entry["artifacts"]}
                        for run_id, entry in entries.items()
                    }
                else:
                    workers = {args.renderer_run: {
                        "engine": UnixEngine(args.renderer_socket, timeout=15),
                        "artifacts": args.renderer_artifacts,
                    }}
                renderer = RendererGateway(
                    workers,
                    world_id=args.world,
                    world_epoch=store.world_epoch(args.world),
                    capture_interval=args.capture_interval if args.capture_interval is not None else 1.0,
                )
            admission = None
            if args.agent_join_pool:
                from .admission import AgentAdmission
                try:
                    admission = AgentAdmission.from_file(args.agent_join_pool, store, args.world, renderer)
                except (OSError, ValueError, ServiceError) as exc:
                    parser.error(f"Invalid agent join pool: {exc}")
            service = Service(store, UnixEngine(args.engine_socket), world_id=args.world,
                              renderer=renderer, admission=admission)
            try:
                if args.transport == "http":
                    from .http import create_server
                    server = create_server(service, args.host, args.port)
                    try:
                        print("Local development HTTP prototype; do not expose directly.", file=sys.stderr)
                        server.serve_forever(poll_interval=0.25)
                    except KeyboardInterrupt:
                        return 0
                    finally:
                        server.server_close()
                else:
                    try:
                        import uvicorn
                        from .asgi import create_app
                    except ImportError as exc:
                        print("ASGI dependencies missing; install backend/requirements.txt", file=sys.stderr)
                        raise SystemExit(2) from exc
                    app = create_app(service)
                    print(
                        "Serving ASGI on loopback. Configure a TLS reverse proxy for remote access.",
                        file=sys.stderr,
                    )
                    uvicorn.run(
                        app, host=args.host, port=args.port, workers=1, loop="asyncio", http="h11",
                        limit_concurrency=128, timeout_keep_alive=5, timeout_graceful_shutdown=70,
                        proxy_headers=False, access_log=False, log_level="warning",
                    )
            finally:
                service.close()
        finally:
            if renderer is not None:
                renderer.close()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
