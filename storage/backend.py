"""
storage/backend.py — StorageBackend protocol + LocalStorage implementation.

All audio files, uploaded books, and demo clips go through this abstraction.
Swap LocalStorage for S3Storage without touching any synthesis or server code.

Key naming conventions
  chapter cache : cache/{sha256}.mp3
  job audio     : jobs/{job_id}/{chapter_num}.mp3
  job preview   : jobs/{job_id}/{chapter_num}_preview.mp3
  uploaded book : books/{job_id}{.pdf|.epub}
  demo clip     : demo/{voice_id}.mp3
  demo hero     : demo/hero.mp3
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import aiofiles
import aiofiles.os

log = logging.getLogger("lector.storage")


@runtime_checkable
class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def delete(self, key: str) -> None: ...
    def local_path(self, key: str) -> Path | None:
        """Return local filesystem path for FileResponse, or None if remote."""
        ...


class LocalStorage:
    """File-system storage under a single root directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # ── internal ────────────────────────────────────────────────────────────────

    def _resolve(self, key: str) -> Path:
        """Convert key to an absolute path, preventing traversal attacks."""
        parts = [p for p in key.replace("\\", "/").split("/") if p and p != ".."]
        return self.root.joinpath(*parts) if parts else self.root / "_invalid"

    # ── protocol ────────────────────────────────────────────────────────────────

    async def put(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)
        log.debug("storage.put %s (%d bytes)", key, len(data))

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    async def delete(self, key: str) -> None:
        try:
            await aiofiles.os.remove(self._resolve(key))
        except OSError:
            pass

    def local_path(self, key: str) -> Path:
        return self._resolve(key)

    # ── helpers ─────────────────────────────────────────────────────────────────

    async def put_from_path(self, key: str, src: Path) -> None:
        """Copy a local file into storage (used after pydub export)."""
        data = src.read_bytes()
        await self.put(key, data)

    async def copy_to_path(self, key: str, dest: Path) -> None:
        """Write storage contents to a local path (e.g. for pydub to read)."""
        data = await self.get(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
