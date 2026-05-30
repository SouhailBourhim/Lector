from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProsodicHints:
    rate: str           # e.g. "+5%", "-15%", "+0%"
    pitch: str          # e.g. "+1Hz", "+0Hz"
    pause_before_ms: int
    pause_after_ms: int


@dataclass
class TextSegment:
    text: str
    segment_type: str   # chapter_title | section_heading | dialogue |
                        # complex_narration | action_narration | default_narration
    hints: ProsodicHints
