from __future__ import annotations
import asyncio

import edge_tts

from analyzer.models import TextSegment
from config import TTS_MAX_CONCURRENCY, TTS_RETRY_ATTEMPTS, DEFAULT_VOICE


async def _synthesize_one(segment: TextSegment, voice: str) -> bytes:
    """Synthesize a single segment, returning raw MP3 bytes."""
    text = segment.text.strip()
    if not text:
        return b""

    for attempt in range(TTS_RETRY_ATTEMPTS):
        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=segment.hints.rate,
                pitch=segment.hints.pitch,
            )
            data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    data += chunk["data"]
            if data:
                return data
        except Exception:
            if attempt < TTS_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(2**attempt)

    return b""  # pydub assembler treats empty bytes as silence


class TTSEngine:
    def __init__(self, voice: str = DEFAULT_VOICE) -> None:
        self.voice = voice

    def synthesize_chapter(self, segments: list[TextSegment]) -> list[bytes]:
        """Synthesize all segments concurrently. Returns list of MP3 byte strings."""
        return asyncio.run(self._run(segments))

    async def _run(self, segments: list[TextSegment]) -> list[bytes]:
        # Semaphore created here so it's always bound to the current event loop
        semaphore = asyncio.Semaphore(TTS_MAX_CONCURRENCY)

        async def _with_sem(seg):
            async with semaphore:
                return await _synthesize_one(seg, self.voice)

        return await asyncio.gather(*[_with_sem(seg) for seg in segments])

    @staticmethod
    def list_voices() -> list[str]:
        """Return available voices from edge-tts (network call)."""
        async def _fetch():
            voices = await edge_tts.list_voices()
            return [v["ShortName"] for v in voices if v["ShortName"].startswith("en-")]
        return asyncio.run(_fetch())
