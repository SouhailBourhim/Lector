from __future__ import annotations
import statistics
from pathlib import Path

import fitz  # PyMuPDF

from .base import BookChapter, BookParser, FormattedSpan, ScannedPDFError
from config import HEADING_FONT_RATIO


class PDFParser(BookParser):
    def parse(self) -> list[BookChapter]:
        doc = fitz.open(str(self.filepath))
        self._guard_scanned(doc)

        toc = doc.get_toc()
        if toc:
            self._chapters = self._parse_with_toc(doc, toc)
        else:
            self._chapters = self._parse_without_toc(doc)

        doc.close()
        return self._chapters

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _guard_scanned(self, doc: fitz.Document) -> None:
        total_text = "".join(page.get_text() for page in doc)
        if len(total_text.strip()) < 100 and len(doc) > 5:
            raise ScannedPDFError(
                "This PDF appears to be a scanned image with no text layer. "
                "Run `ocrmypdf input.pdf output.pdf` first to add a text layer."
            )

    def _parse_with_toc(
        self, doc: fitz.Document, toc: list
    ) -> list[BookChapter]:
        chapters: list[BookChapter] = []
        # toc entries: [level, title, page_number]  (1-based page numbers)
        top_level = min(entry[0] for entry in toc)
        boundaries = [
            (entry[1], entry[2] - 1)  # (title, 0-based start page)
            for entry in toc
            if entry[0] == top_level
        ]

        for idx, (title, start_page) in enumerate(boundaries):
            end_page = boundaries[idx + 1][1] if idx + 1 < len(boundaries) else len(doc)
            spans = self._extract_spans(doc, start_page, end_page)
            chapters.append(BookChapter(title=title, number=idx + 1, spans=spans))

        return chapters

    def _parse_without_toc(self, doc: fitz.Document) -> list[BookChapter]:
        all_spans = self._extract_spans(doc, 0, len(doc))

        chapters: list[BookChapter] = []
        current_title = "Introduction"
        current_spans: list[FormattedSpan] = []
        chapter_num = 1

        for span in all_spans:
            if span.is_heading and len(span.text.strip()) > 2:
                if current_spans:
                    chapters.append(
                        BookChapter(
                            title=current_title,
                            number=chapter_num,
                            spans=current_spans,
                        )
                    )
                    chapter_num += 1
                current_title = span.text.strip()
                current_spans = []
            else:
                current_spans.append(span)

        if current_spans:
            chapters.append(
                BookChapter(
                    title=current_title,
                    number=chapter_num,
                    spans=current_spans,
                )
            )

        if not chapters:
            # Fallback: single chapter with all content
            chapters = [BookChapter(title="Full Text", number=1, spans=all_spans)]

        return chapters

    def _extract_spans(
        self, doc: fitz.Document, start_page: int, end_page: int
    ) -> list[FormattedSpan]:
        raw_spans: list[dict] = []

        for page_num in range(start_page, end_page):
            page = doc[page_num]
            page_dict = page.get_text("dict", sort=True)
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:  # skip image blocks
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if not text:
                            continue
                        raw_spans.append(
                            {
                                "text": text,
                                "flags": span.get("flags", 0),
                                "font": span.get("font", ""),
                                "size": span.get("size", 12),
                            }
                        )
                # paragraph boundary after each block
                raw_spans.append({"paragraph_break": True})

        if not raw_spans:
            return []

        # Compute median font size to identify headings
        sizes = [s["size"] for s in raw_spans if not s.get("paragraph_break")]
        median_size = statistics.median(sizes) if sizes else 12

        result: list[FormattedSpan] = []
        for s in raw_spans:
            if s.get("paragraph_break"):
                result.append(FormattedSpan(text="", paragraph_break=True))
                continue

            flags = s["flags"]
            font = s["font"].lower()
            bold = bool(flags & 16) or "bold" in font
            italic = bool(flags & 2) or "italic" in font or "oblique" in font
            is_heading = s["size"] >= median_size * HEADING_FONT_RATIO

            result.append(
                FormattedSpan(
                    text=s["text"],
                    bold=bold,
                    italic=italic,
                    is_heading=is_heading,
                )
            )

        return result
