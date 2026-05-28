from __future__ import annotations
from pathlib import Path

import chardet
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Tag

from .base import BookChapter, BookParser, FormattedSpan
from config import SKIP_EPUB_KEYWORDS


class EPUBParser(BookParser):
    def parse(self) -> list[BookChapter]:
        book = epub.read_epub(str(self.filepath), options={"ignore_ncx": True})
        self._chapters = []
        chapter_num = 1

        for item_id, _ in book.spine:
            item = book.get_item_with_id(item_id)
            if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue

            filename = item.get_name().lower()
            if any(kw in filename for kw in SKIP_EPUB_KEYWORDS):
                continue

            content = self._decode(item.get_content())
            soup = BeautifulSoup(content, "html.parser")

            title = self._extract_title(soup, chapter_num)
            spans = self._extract_spans(soup)

            # Skip near-empty items (covers, blank pages, etc.)
            plain = " ".join(s.text for s in spans if not s.paragraph_break)
            if len(plain.split()) < 50:
                continue

            self._chapters.append(
                BookChapter(title=title, number=chapter_num, spans=spans)
            )
            chapter_num += 1

        return self._chapters

    # ------------------------------------------------------------------

    def _decode(self, raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            detected = chardet.detect(raw)
            enc = detected.get("encoding") or "latin-1"
            return raw.decode(enc, errors="replace")

    def _extract_title(self, soup: BeautifulSoup, fallback_num: int) -> str:
        for tag in ("h1", "h2", "h3", "title"):
            el = soup.find(tag)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return f"Chapter {fallback_num}"

    def _extract_spans(self, soup: BeautifulSoup) -> list[FormattedSpan]:
        body = soup.find("body") or soup
        spans: list[FormattedSpan] = []
        self._walk(body, spans, bold=False, italic=False, in_heading=False)
        return spans

    def _walk(
        self,
        node: Tag | NavigableString,
        spans: list[FormattedSpan],
        bold: bool,
        italic: bool,
        in_heading: bool,
    ) -> None:
        if isinstance(node, NavigableString):
            text = str(node)
            if text.strip():
                spans.append(
                    FormattedSpan(
                        text=text,
                        bold=bold,
                        italic=italic,
                        is_heading=in_heading,
                    )
                )
            return

        tag_name = node.name.lower() if node.name else ""

        # Update formatting flags
        new_bold = bold or tag_name in ("strong", "b")
        new_italic = italic or tag_name in ("em", "i")
        new_heading = in_heading or tag_name in ("h1", "h2", "h3", "h4")

        # Paragraph/block boundary before block-level elements
        is_block = tag_name in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "br", "li")
        if is_block and spans and not spans[-1].paragraph_break:
            spans.append(FormattedSpan(text="", paragraph_break=True))

        for child in node.children:
            self._walk(child, spans, new_bold, new_italic, new_heading)

        # Paragraph boundary after block-level elements
        if is_block and spans and not spans[-1].paragraph_break:
            spans.append(FormattedSpan(text="", paragraph_break=True))
