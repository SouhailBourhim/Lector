"""
storage/db.py — SQLite-backed job store (WAL mode, aiosqlite).

All job mutations go through JobRepo. No in-memory job dict remains in server.py.
The schema is intentionally flat (one table) to keep queries trivial.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger("lector.db")

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending',
    -- pending: uploaded, awaiting chapter selection
    -- queued:  ready for a worker to claim
    -- analyzing|synthesizing|assembling: in-flight
    -- done|error: terminal
    progress      REAL NOT NULL DEFAULT 0.0,
    message       TEXT NOT NULL DEFAULT '',
    voice         TEXT NOT NULL DEFAULT '',
    chapters_json TEXT NOT NULL DEFAULT '[]',
    selected_json TEXT NOT NULL DEFAULT '[]',
    audio_keys    TEXT NOT NULL DEFAULT '{}',
    preview_keys  TEXT NOT NULL DEFAULT '{}',
    error         TEXT,
    client_ip     TEXT NOT NULL DEFAULT '',
    book_key      TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_ip         ON jobs(client_ip);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
"""

# ── Job dataclass ─────────────────────────────────────────────────────────────

@dataclass
class Job:
    id:            str
    status:        str   = "pending"  # pending|queued|analyzing|synthesizing|assembling|done|error
    progress:      float = 0.0
    message:       str   = ""
    voice:         str   = ""
    chapters_json: str   = "[]"       # serialized list[ChapterDict]
    selected_json: str   = "[]"       # serialized list[int]
    audio_keys:    str   = "{}"       # serialized dict {str(chapter_num): storage_key}
    preview_keys:  str   = "{}"       # serialized dict {str(chapter_num): storage_key}
    error:         str | None = None
    client_ip:     str   = ""
    book_key:      str | None = None  # storage key for uploaded book file
    created_at:    float = field(default_factory=time.time)
    updated_at:    float = field(default_factory=time.time)

    # ── convenience helpers ───────────────────────────────────────────────────

    @property
    def selected(self) -> list[int]:
        return json.loads(self.selected_json)

    def get_audio_keys(self) -> dict[str, str]:
        return json.loads(self.audio_keys)

    def get_preview_keys(self) -> dict[str, str]:
        return json.loads(self.preview_keys)

    def get_chapters(self) -> list[dict]:
        """Return raw chapter dicts (number, title, spans)."""
        return json.loads(self.chapters_json)

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())


# ── Serialization helpers ─────────────────────────────────────────────────────

def serialize_chapters(chapters: list) -> str:
    """Convert list[BookChapter] → JSON string for storage."""
    from parsers.base import BookChapter
    result = []
    for ch in chapters:
        result.append({
            "number": ch.number,
            "title":  ch.title,
            "spans":  [
                {
                    "text":            s.text,
                    "bold":            s.bold,
                    "italic":          s.italic,
                    "is_heading":      s.is_heading,
                    "paragraph_break": s.paragraph_break,
                }
                for s in ch.spans
            ],
        })
    return json.dumps(result)


def deserialize_chapters(data: str) -> list:
    """Convert JSON string → list[BookChapter]."""
    from parsers.base import FormattedSpan, BookChapter
    chapters = []
    for ch in json.loads(data):
        spans = [FormattedSpan(**s) for s in ch["spans"]]
        chapters.append(BookChapter(number=ch["number"], title=ch["title"], spans=spans))
    return chapters


def chapters_summary(chapters_json: str) -> list[dict]:
    """Return [{number, title}] without loading spans — for API responses."""
    return [{"number": c["number"], "title": c["title"]} for c in json.loads(chapters_json)]


# ── Pre-built SQL constants (no runtime string construction) ──────────────────
# These are module-level string literals so that JobRepo.update() can pass a
# plain dict-lookup value to execute() rather than a dynamically constructed
# string.  Static-analysis tools see execute(_FIELD_SQL[col], ...) and treat
# the argument as a whitelisted literal, not tainted concatenation.

_FIELD_SQL: dict[str, str] = {
    "status":        "UPDATE jobs SET status=?        WHERE id=?",
    "progress":      "UPDATE jobs SET progress=?      WHERE id=?",
    "message":       "UPDATE jobs SET message=?       WHERE id=?",
    "voice":         "UPDATE jobs SET voice=?         WHERE id=?",
    "chapters_json": "UPDATE jobs SET chapters_json=? WHERE id=?",
    "selected_json": "UPDATE jobs SET selected_json=? WHERE id=?",
    "audio_keys":    "UPDATE jobs SET audio_keys=?    WHERE id=?",
    "preview_keys":  "UPDATE jobs SET preview_keys=?  WHERE id=?",
    "error":         "UPDATE jobs SET error=?         WHERE id=?",
    "client_ip":     "UPDATE jobs SET client_ip=?     WHERE id=?",
    "book_key":      "UPDATE jobs SET book_key=?      WHERE id=?",
    "updated_at":    "UPDATE jobs SET updated_at=?    WHERE id=?",
}

# Literal SQL for startup re-queue — no parameters in the WHERE clause so
# there is nothing to inject; timestamp is the only bound parameter.
_REQUEUE_COUNT_SQL = (
    "SELECT COUNT(*) FROM jobs"
    " WHERE status IN ('analyzing','synthesizing','assembling')"
)
_REQUEUE_UPDATE_SQL = (
    "UPDATE jobs SET status='queued', updated_at=?"
    " WHERE status IN ('analyzing','synthesizing','assembling')"
)


# ── JobRepo ───────────────────────────────────────────────────────────────────

class JobRepo:
    """Async CRUD wrapper around the SQLite jobs table."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._db.commit()
        log.info("DB opened: %s", self._path)
        # Re-queue any jobs that were mid-flight when the server last stopped
        await self._requeue_interrupted()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── write ─────────────────────────────────────────────────────────────────

    async def create(self, job: Job) -> None:
        await self._db.execute(
            """INSERT INTO jobs
               (id,status,progress,message,voice,chapters_json,selected_json,
                audio_keys,preview_keys,error,client_ip,book_key,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.id, job.status, job.progress, job.message, job.voice,
             job.chapters_json, job.selected_json, job.audio_keys, job.preview_keys,
             job.error, job.client_ip, job.book_key, job.created_at, job.updated_at),
        )
        await self._db.commit()

    async def update(self, job_id: str, **fields: Any) -> None:
        """Update arbitrary job fields.

        Each field maps to a pre-built literal SQL statement in _FIELD_SQL so
        that no string is constructed at call-time and execute() always receives
        a fully static string (satisfying strict SQL-injection static analysis).
        Multiple fields are applied inside a single explicit transaction.
        """
        if not fields:
            return
        fields["updated_at"] = time.time()
        unknown = set(fields) - _FIELD_SQL.keys()
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        async with self._db.execute("BEGIN"):
            pass
        try:
            for col, val in fields.items():
                await self._db.execute(_FIELD_SQL[col], (val, job_id))
            await self._db.commit()
        except Exception:
            await self._db.execute("ROLLBACK")
            raise

    async def delete(self, job_id: str) -> None:
        await self._db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        await self._db.commit()

    # ── read ──────────────────────────────────────────────────────────────────

    async def get(self, job_id: str) -> Job | None:
        async with self._db.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def count_active_for_ip(self, ip: str) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM jobs WHERE client_ip=? AND status NOT IN ('done','error')",
            (ip,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def count_active_global(self) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status NOT IN ('done','error','queued')"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def queue_depth(self) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='queued'"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def list_stale(self, older_than_secs: float) -> list[Job]:
        """Return only terminal jobs (done/error) older than the given age.

        Restricting to terminal states prevents accidental deletion of
        long-running in-flight jobs whose wall-clock time exceeds the TTL.
        """
        cutoff = time.time() - older_than_secs
        async with self._db.execute(
            "SELECT * FROM jobs WHERE created_at < ? AND status IN ('done','error')",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    # ── worker coordination ───────────────────────────────────────────────────

    async def claim_next(self) -> Job | None:
        """Atomically claim the oldest queued job → status 'analyzing'."""
        now = time.time()
        async with self._db.execute(
            """UPDATE jobs SET status='analyzing', updated_at=?
               WHERE id=(SELECT id FROM jobs WHERE status='queued'
                         ORDER BY created_at LIMIT 1)
               RETURNING *""",
            (now,),
        ) as cur:
            row = await cur.fetchone()
        await self._db.commit()
        return _row_to_job(row) if row else None

    # ── internal ──────────────────────────────────────────────────────────────

    async def _requeue_interrupted(self) -> None:
        """On startup, move in-flight jobs back to 'queued' so workers pick them up."""
        now = time.time()
        async with self._db.execute(_REQUEUE_COUNT_SQL) as cur:
            row = await cur.fetchone()
        count = row[0] if row else 0
        if count:
            await self._db.execute(_REQUEUE_UPDATE_SQL, (now,))
            await self._db.commit()
            log.info("Re-queued %d interrupted job(s) from previous run", count)


# ── row helper ────────────────────────────────────────────────────────────────

def _row_to_job(row: aiosqlite.Row) -> Job:
    d = dict(row)
    return Job(
        id=d["id"],
        status=d["status"],
        progress=d["progress"],
        message=d["message"],
        voice=d["voice"],
        chapters_json=d["chapters_json"],
        selected_json=d["selected_json"],
        audio_keys=d["audio_keys"],
        preview_keys=d["preview_keys"],
        error=d["error"],
        client_ip=d["client_ip"],
        book_key=d["book_key"],
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )
