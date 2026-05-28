from __future__ import annotations
import io
from pathlib import Path

from pydub import AudioSegment

from analyzer.models import TextSegment
from config import AUDIO_FRAME_RATE, AUDIO_CHANNELS, AUDIO_BITRATE


def _load_clip(mp3_bytes: bytes) -> AudioSegment:
    if not mp3_bytes:
        # Return 100ms silence as fallback for failed TTS segments
        return AudioSegment.silent(duration=100)
    clip = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
    return clip.set_frame_rate(AUDIO_FRAME_RATE).set_channels(AUDIO_CHANNELS)


def assemble_chapter(
    segments: list[TextSegment],
    audio_clips: list[bytes],
) -> AudioSegment:
    """Stitch synthesized clips together with prosody-aware silence gaps."""
    result = AudioSegment.empty()

    for segment, clip_bytes in zip(segments, audio_clips):
        clip = _load_clip(clip_bytes)

        if segment.hints.pause_before_ms > 0:
            result += AudioSegment.silent(duration=segment.hints.pause_before_ms)

        result += clip

        if segment.hints.pause_after_ms > 0:
            result += AudioSegment.silent(duration=segment.hints.pause_after_ms)

    return result


def export_chapter(audio: AudioSegment, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(str(output_path), format="mp3", bitrate=AUDIO_BITRATE)


def chapter_filename(book_stem: str, chapter_num: int, chapter_title: str) -> str:
    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "" for c in chapter_title
    ).strip()[:50]
    return f"{chapter_num:02d}_{safe_title}.mp3"
