"""Hold a private stock-fixture authority open for manual renderer acceptance.

Example:
  OPENTTD_TEST_LIBRARY_PATH=/local/dependencies python3 tests/integration/serve_fixture.py \
    --binary /path/to/openttd --baseset /path/to/baseset \
    --metadata /tmp/private-renderer-authority.json

SIGINT or SIGTERM stops the native server and releases its socket. No public
credentials are generated; the metadata describes private operator endpoints.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import threading

from engine_harness import LiveWorld


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--baseset", required=True)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--port", type=int, default=13979)
    args = parser.parse_args()
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    with LiveWorld(args.binary, args.baseset, port=args.port) as world:
        hello = world.completed(world.rpc("hello"))
        metadata = {
            "port": args.port,
            "world_id": world.world_id,
            "world_epoch": world.epoch,
            "engine_instance_id": world.instance,
            "authority_max_lag_ticks": hello.get("native_max_lag_ticks"),
            "socket_path": str(world.socket_path),
            "directory": str(world.directory),
            "actor": world.actor,
            "text_actor": world.actor,
        }
        descriptor = os.open(args.metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(metadata, output, sort_keys=True)
            output.write("\n")
        print(f"Private fixture authority ready on 127.0.0.1:{args.port}; metadata {args.metadata}", flush=True)
        try:
            while not stopped.wait(0.5):
                if world.process.poll() is not None:
                    raise RuntimeError(f"Native authority exited:\n{world.log_tail()}")
        finally:
            args.metadata.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
