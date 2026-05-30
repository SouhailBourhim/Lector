from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from pathlib import Path

from pydub import AudioSegment


class Player:
    """Plays AudioSegment or MP3 file using pygame, falling back to system player."""

    def __init__(self) -> None:
        self._pygame_ok = self._try_init_pygame()

    def play_segment(self, audio: AudioSegment) -> None:
        if self._pygame_ok:
            self._play_pygame(audio)
        else:
            self._play_system(audio)

    def play_file(self, path: Path) -> None:
        if self._pygame_ok:
            import pygame
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
        else:
            self._open_system_file(path)

    def stop(self) -> None:
        if self._pygame_ok:
            import pygame
            pygame.mixer.music.stop()

    # ------------------------------------------------------------------

    def _try_init_pygame(self) -> bool:
        try:
            import pygame
            pygame.mixer.init(frequency=24000, channels=1)
            return True
        except Exception:
            return False

    def _play_pygame(self, audio: AudioSegment) -> None:
        import pygame
        buf = io.BytesIO()
        audio.export(buf, format="mp3")
        buf.seek(0)
        pygame.mixer.music.load(buf)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)

    def _play_system(self, audio: AudioSegment) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp = Path(f.name)
        audio.export(str(tmp), format="mp3")
        self._open_system_file(tmp)

    def _open_system_file(self, path: Path) -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
