"""Build the finite public agent starter kit once, before a listener starts.

Only the files below can enter the kit. Request paths never become filesystem
paths, and every source must be a bounded regular file in the same release.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Mapping
import zipfile


MAX_SOURCE = 512 * 1024
MAX_KIT = 2 * 1024 * 1024
_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATHS = (
    "COPYING.md",
    "backend/__init__.py",
    "backend/agent_client.py",
    "backend/capture_tools.py",
    "backend/client.py",
    "backend/errors.py",
    "backend/json_tools.py",
    "backend/observations.py",
    "backend/player_actions.py",
    "backend/player_mechanics.py",
    "backend/schemas/engine-catalog-v1.json",
    "backend/schemas/public-api-v1.json",
    "docs/llms-capture.txt",
    "docs/llms.txt",
)
_PUBLIC_SOURCES = {
    "/llms.txt": ("docs/llms.txt", "text/plain; charset=utf-8"),
    "/llms-capture.txt": ("docs/llms-capture.txt", "text/plain; charset=utf-8"),
    "/agent/public-api-v1.json": ("backend/schemas/public-api-v1.json", "application/json"),
    "/agent/engine-catalog-v1.json": ("backend/schemas/engine-catalog-v1.json", "application/json"),
}
PUBLIC_PATHS = frozenset({*_PUBLIC_SOURCES, "/agent/client.zip"})


def _read_source(directory: int, relative: str) -> bytes:
    """Traverse only an allowlisted relative name without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    current = os.dup(directory)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            child = os.open(part, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=current)
            os.close(current)
            current = child
        descriptor = os.open(parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=current)
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_SOURCE:
                raise ValueError("Invalid agent kit source")
            data = source.read(MAX_SOURCE + 1)
            if len(data) > MAX_SOURCE:
                raise ValueError("Agent kit source exceeds its size limit")
            return data
    finally:
        os.close(current)


def build_agent_assets(root: Path | None = None) -> Mapping[str, tuple[bytes, str]]:
    """Return immutable exact public URLs and bytes; missing sources fail startup.

    The uncompressed ZIP uses fixed ordering, metadata and timestamps so identical
    source bytes produce identical artifacts without a build tool or git access.
    Its manifest identifies each source by SHA256, including both public schemas.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(_ROOT if root is None else root, flags)
    try:
        sources = {}
        size = 0
        for name in SOURCE_PATHS:
            data = _read_source(directory, name)
            size += len(data)
            if size > MAX_KIT:
                raise ValueError("Agent kit sources exceed their size limit")
            sources[name] = data
    finally:
        os.close(directory)
    manifest = json.dumps({
        "format_version": 1,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()},
    }, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as kit:
        for name, data in sorted({**sources, "manifest.json": manifest}.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            kit.writestr(entry, data)
    data = archive.getvalue()
    if len(data) > MAX_KIT:
        raise ValueError("Agent kit archive exceeds its size limit")
    assets = {path: (sources[name], content_type)
              for path, (name, content_type) in _PUBLIC_SOURCES.items()}
    assets["/agent/client.zip"] = (data, "application/zip")
    return MappingProxyType(assets)
