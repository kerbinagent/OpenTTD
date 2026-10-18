#!/usr/bin/env python3
"""Small public HTTP primitives for an independently scoped gameplay player.

This helper imports only the public reference client. It has no native socket,
database, world-state inspection, planning or route construction shortcuts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from backend.client import BackendClient, BackendError, credential_from_env_or_file

IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
TERMINAL = {"completed", "rejected", "uncertain", "retired", "resolved_rolled_back"}


class PlayerClient:
    def __init__(self, origin, credential_file, artifacts):
        self.client = BackendClient(origin, credential_from_env_or_file(credential_file), timeout=15)
        self.artifacts = artifacts.resolve()
        for directory in (self.artifacts, self.artifacts / "requests", self.artifacts / "images"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def record(self, value):
        with (self.artifacts / "public-io.jsonl").open("a", encoding="utf-8") as target:
            target.write(json.dumps({"time": time.time(), **value}, allow_nan=False) + "\n")

    def request(self, method, path, body=None):
        if not path.startswith("/v1/"):
            raise ValueError("Only the public /v1 API is available")
        self.record({"request": {"method": method, "path": path, "body": body}})
        try:
            reply = self.client.request(method, path, body)
        except BackendError as error:
            self.record({"error": {"status": error.status, "code": error.code, "message": error.message}})
            raise
        except (OSError, TimeoutError) as error:
            self.record({"transport_error": type(error).__name__})
            raise
        self.record({"response": reply})
        return reply

    def issue(self, body):
        request_id = body["request_id"]
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ValueError("Invalid request ID")
        path = self.artifacts / "requests" / (request_id + ".json")
        if path.exists():
            if json.loads(path.read_text()) != body:
                raise ValueError("Request ID already has a different saved body")
        else:
            path.write_text(json.dumps(body, indent=2) + "\n")
        reply = self.request("POST", "/v1/actions", body)
        deadline = time.monotonic() + 20
        while reply["status"] not in TERMINAL:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Request {request_id} is pending; use status or retry with this same saved ID")
            time.sleep(0.1)
            reply = self.request("GET", "/v1/actions/" + request_id)
        return reply

    def capture(self, label):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label):
            raise ValueError("Image label must be a short plain filename")
        deadline = time.monotonic() + 20
        while True:
            try:
                metadata = self.request("POST", "/v1/view/images", {})
                break
            except BackendError as error:
                if error.status not in (429, 503) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        image_id = metadata["image_id"]
        self.record({"request": {"method": "GET", "path": "/v1/images/" + image_id, "body": None}})
        try:
            png = self.client.image(image_id)
        except BackendError as error:
            self.record({"error": {"status": error.status, "code": error.code, "message": error.message}})
            raise
        except (OSError, TimeoutError, ValueError) as error:
            self.record({"image_download_error": type(error).__name__})
            raise
        self.record({"response": {"status": 200, "content_type": "image/png", "bytes": len(png)}})
        path = self.artifacts / "images" / (image_id + ".png")
        path.write_bytes(png)
        value = {**metadata, "image_path": str(path)}
        (self.artifacts / (label + ".json")).write_text(json.dumps(value, indent=2) + "\n")
        self.record({"image": {"image_id": image_id, "bytes": len(png), "path": str(path)}})
        return value


def object_json(value):
    result = json.loads(value)
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("Expected a JSON object")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--artifacts", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("get")
    get.add_argument("path")
    post = commands.add_parser("post")
    post.add_argument("path")
    post.add_argument("body", type=object_json)
    observe = commands.add_parser("observe")
    observe.add_argument("query")
    observe.add_argument("parameters", nargs="?", type=object_json, default={})
    action = commands.add_parser("action")
    action.add_argument("name")
    action.add_argument("parameters", type=object_json)
    action.add_argument("--id", default=None)
    for command in ("status", "retry"):
        commands.add_parser(command).add_argument("request_id")
    commands.add_parser("capture").add_argument("label", nargs="?", default="view")
    for command in ("ui", "control"):
        sub = commands.add_parser(command)
        sub.add_argument("image_id")
        sub.add_argument("parameters", type=object_json)
        if command == "ui":
            sub.add_argument("--id", default=None)
    args = parser.parse_args()
    player = PlayerClient(args.origin, args.token_file, args.artifacts)
    if args.command == "get":
        result = player.request("GET", args.path)
    elif args.command == "post":
        result = player.issue(args.body) if args.path == "/v1/actions" else player.request("POST", args.path, args.body)
    elif args.command == "observe":
        result = player.request("POST", "/v1/observations", {"query": args.query, "parameters": args.parameters})
    elif args.command == "action":
        result = player.issue({"request_id": args.id or uuid.uuid4().hex,
                               "action": {"name": args.name, "parameters": args.parameters}})
    elif args.command in ("status", "retry"):
        if not IDENTIFIER.fullmatch(args.request_id):
            raise ValueError("Invalid request ID")
        result = (player.request("GET", "/v1/actions/" + args.request_id) if args.command == "status"
                  else player.issue(json.loads((player.artifacts / "requests" / (args.request_id + ".json")).read_text())))
    elif args.command == "capture":
        result = player.capture(args.label)
    elif args.command in ("ui", "control"):
        if "image_id" in args.parameters:
            raise ValueError("Supply image_id once as the separate argument")
        parameters = {**args.parameters, "image_id": args.image_id}
        result = (player.issue({"request_id": args.id or uuid.uuid4().hex,
                                "action": {"name": "ui.apply", "parameters": parameters}})
                  if args.command == "ui" else player.request("POST", "/v1/view/controls", parameters))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BackendError as error:
        print(json.dumps({"error": {"status": error.status, "code": error.code, "message": error.message}}), file=sys.stderr)
        raise SystemExit(1)
