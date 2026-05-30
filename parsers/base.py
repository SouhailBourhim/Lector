from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FormattedSpan:
    text: str
    bold: bool = False
    italic: bool = False
    is_heading: bool = False
    paragraph_break: bool = False  # sentinel: marks end of a paragraph block


@dataclass
class BookChapter:
    title: str
    number: int
    spans: list[FormattedSpan] = field(default_factory=list)

    def plain_text(self) -> str:
        parts = []
        for span in self.spans:
            if span.paragraph_break:
                parts.append("\n\n")
            else:
                parts.append(span.text)
        return "".join(parts)


class BookParser(ABC):
    def __init__(self, filepath: str | Path) -> None:
        self.filepath = Path(filepath)
        self._chapters: list[BookChapter] = []

    @abstractmethod
    def parse(self) -> list[BookChapter]:
        ...

    def get_chapter_count(self) -> int:
        if not self._chapters:
            self.parse()
        return len(self._chapters)


class ScannedPDFError(Exception):
    pass
