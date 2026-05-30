from __future__ import annotations

import re
import unicodedata
from statistics import mean

import spacy

from analyzer.models import ProsodicHints, TextSegment
from config import (
    ABBREVIATIONS,
    PARAGRAPH_PAUSE_MS,
    PROSODY,
    PUNCTUATION_PAUSE,
    SPEECH_VERBS,
)
from parsers.base import BookChapter, FormattedSpan

# Normalize curly quotes to straight ASCII before regex matching
_QUOTE_MAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})

_DIALOGUE_RE = re.compile(r'"[^"]{1,300}"')
_ELLIPSIS_RE = re.compile(r"\.{3}|…")
_EM_DASH_RE = re.compile(r"—")


def _normalize_quotes(text: str) -> str:
    return unicodedata.normalize("NFC", text).translate(_QUOTE_MAP)


class TextAnalyzer:
    def __init__(self) -> None:
        try:
            self._nlp = spacy.load("en_core_web_sm")
        except OSError:
            raise RuntimeError(
                "spaCy model not found. Run: python -m spacy download en_core_web_sm"
            )

    def analyze_chapter(self, chapter: BookChapter) -> list[TextSegment]:
        segments: list[TextSegment] = []

        # Chapter title as first segment
        segments.append(self._make_heading_segment(chapter.title, "chapter_title"))

        # Build text + span offset index for formatting lookup
        text, offset_map = self._build_text(chapter.spans)

        # spaCy sentence segmentation
        doc = self._nlp(text)
        sentences = list(doc.sents)
        sentences = self._merge_abbreviation_splits(sentences, text)

        for i, sent in enumerate(sentences):
            sent_text = sent.text.strip()
            if not sent_text:
                continue

            # Check if this position follows a paragraph break
            pause_before = self._paragraph_pause_before(sent.start_char, offset_map)

            # Check formatting at this position
            span_meta = self._span_meta_at(sent.start_char, offset_map)

            if span_meta.get("is_heading"):
                segments.append(self._make_heading_segment(sent_text, "section_heading"))
                continue

            # Sub-sentence splitting on ellipsis / em-dash
            sub_segments = self._split_on_breaks(sent_text, pause_before, doc, sent)
            segments.extend(sub_segments)

        return segments

    # ------------------------------------------------------------------
    # Heading segments
    # ------------------------------------------------------------------

    def _make_heading_segment(self, text: str, seg_type: str) -> TextSegment:
        p = PROSODY[seg_type]
        return TextSegment(
            text=text,
            segment_type=seg_type,
            hints=ProsodicHints(
                rate=p["rate"],
                pitch=p["pitch"],
                pause_before_ms=p["pause_before"],
                pause_after_ms=p["pause_after"],
            ),
        )

    # ------------------------------------------------------------------
    # Text & offset index construction
    # ------------------------------------------------------------------

    def _build_text(
        self, spans: list[FormattedSpan]
    ) -> tuple[str, list[dict]]:
        """Return (full_text, offset_map).

        offset_map is a list of dicts with keys:
            start, end, bold, italic, is_heading, paragraph_break
        sorted by start position.
        """
        parts: list[str] = []
        offset_map: list[dict] = []
        pos = 0

        for span in spans:
            if span.paragraph_break:
                chunk = "\n\n"
                offset_map.append(
                    dict(
                        start=pos,
                        end=pos + len(chunk),
                        bold=False,
                        italic=False,
                        is_heading=False,
                        paragraph_break=True,
                    )
                )
                parts.append(chunk)
                pos += len(chunk)
            else:
                chunk = span.text
                if not chunk:
                    continue
                offset_map.append(
                    dict(
                        start=pos,
                        end=pos + len(chunk),
                        bold=span.bold,
                        italic=span.italic,
                        is_heading=span.is_heading,
                        paragraph_break=False,
                    )
                )
                parts.append(chunk)
                pos += len(chunk)

        return "".join(parts), offset_map

    def _span_meta_at(self, char_pos: int, offset_map: list[dict]) -> dict:
        for entry in offset_map:
            if entry["start"] <= char_pos < entry["end"]:
                return entry
        return {}

    def _paragraph_pause_before(self, char_pos: int, offset_map: list[dict]) -> int:
        """Return PARAGRAPH_PAUSE_MS if the nearest preceding entry is a paragraph break."""
        preceding = [e for e in offset_map if e["end"] <= char_pos]
        if preceding:
            last = preceding[-1]
            if last["paragraph_break"]:
                return PARAGRAPH_PAUSE_MS
        return 0

    # ------------------------------------------------------------------
    # Abbreviation-aware sentence merging
    # ------------------------------------------------------------------

    def _merge_abbreviation_splits(self, sents, text: str) -> list:
        merged = []
        skip_next = False
        sent_list = list(sents)
        for i, sent in enumerate(sent_list):
            if skip_next:
                skip_next = False
                continue
            raw   = sent.text.rstrip()
            parts = raw.rstrip(".").split()          # guard against "..."-only sentences
            last_word = parts[-1].lower() if parts else ""
            if last_word in ABBREVIATIONS and i + 1 < len(sent_list):
                # Merge with next sentence by creating a combined span
                merged.append(_MergedSent(sent, sent_list[i + 1]))
                skip_next = True
            else:
                merged.append(sent)
        return merged

    # ------------------------------------------------------------------
    # Sub-sentence splitting on ellipsis / em-dash
    # ------------------------------------------------------------------

    def _split_on_breaks(
        self, text: str, pause_before: int, doc, spacy_sent
    ) -> list[TextSegment]:
        # Find split points
        splits: list[tuple[int, int]] = []  # (match_start, pause_ms)
        for m in _ELLIPSIS_RE.finditer(text):
            splits.append((m.end(), PUNCTUATION_PAUSE.get("...", 1000)))
        for m in _EM_DASH_RE.finditer(text):
            splits.append((m.start(), PUNCTUATION_PAUSE.get("—", 350)))
        splits.sort()

        if not splits:
            return [self._classify(text, pause_before, doc, spacy_sent)]

        parts: list[TextSegment] = []
        prev = 0
        for pos, pause_ms in splits:
            chunk = text[prev:pos].strip()
            if chunk:
                seg = self._classify(
                    chunk,
                    pause_before if not parts else 0,
                    doc,
                    spacy_sent,
                )
                seg.hints.pause_after_ms = pause_ms
                parts.append(seg)
            prev = pos

        tail = text[prev:].strip()
        if tail:
            parts.append(
                self._classify(tail, 0, doc, spacy_sent)
            )
        return parts

    # ------------------------------------------------------------------
    # Sentence classification
    # ------------------------------------------------------------------

    def _classify(
        self, text: str, pause_before: int, doc, spacy_sent
    ) -> TextSegment:
        norm = _normalize_quotes(text)

        # Dialogue detection
        if self._is_dialogue(norm, doc, spacy_sent):
            return self._dialogue_segment(text, pause_before)

        # Narration: score complexity
        score = self._complexity(text, spacy_sent)
        word_count = len([t for t in spacy_sent if not t.is_punct])

        if score >= 4:
            seg_type = "complex_narration"
        elif score <= 0 and word_count < 15:
            seg_type = "action_narration"
        else:
            seg_type = "default_narration"

        p = PROSODY[seg_type]
        pause_after = self._trailing_pause(text, p["pause_after"])
        return TextSegment(
            text=text,
            segment_type=seg_type,
            hints=ProsodicHints(
                rate=p["rate"],
                pitch=p["pitch"],
                pause_before_ms=pause_before,
                pause_after_ms=pause_after,
            ),
        )

    # ------------------------------------------------------------------
    # Dialogue detection
    # ------------------------------------------------------------------

    def _is_dialogue(self, norm_text: str, doc, spacy_sent) -> bool:
        # Tier 1: straight-quote match
        if _DIALOGUE_RE.search(norm_text):
            return True

        # Tier 2: sentence starts/ends with quote (open dialogue block)
        if norm_text.startswith('"') or norm_text.endswith('"'):
            return True

        # Tier 3: speech verb in sentence
        for token in spacy_sent:
            if token.lemma_.lower() in SPEECH_VERBS:
                return True

        return False

    def _dialogue_segment(self, text: str, pause_before: int) -> TextSegment:
        p = PROSODY["dialogue"]
        # Deterministic ±10% rate variation per sentence
        rate_delta = (hash(text) % 21) - 10
        rate = f"{rate_delta:+d}%"
        pause_after = self._trailing_pause(text, p["pause_after"])
        return TextSegment(
            text=text,
            segment_type="dialogue",
            hints=ProsodicHints(
                rate=rate,
                pitch=p["pitch"],
                pause_before_ms=pause_before,
                pause_after_ms=pause_after,
            ),
        )

    # ------------------------------------------------------------------
    # Complexity scoring
    # ------------------------------------------------------------------

    def _complexity(self, text: str, spacy_sent) -> int:
        score = 0
        tokens = [t for t in spacy_sent if not t.is_punct]
        word_count = len(tokens)

        if word_count > 25:
            score += 2
        elif word_count > 15:
            score += 1
        elif word_count < 10:
            score -= 1

        subordinate_labels = {"advcl", "relcl", "ccomp", "xcomp", "acl"}
        score += sum(1 for t in spacy_sent if t.dep_ in subordinate_labels)

        comma_count = text.count(",")
        score += min(comma_count, 3)

        if ";" in text or ":" in text:
            score += 1

        alpha_tokens = [t for t in tokens if t.is_alpha]
        if alpha_tokens:
            avg_len = mean(len(t.text) for t in alpha_tokens)
            if avg_len > 6:
                score += 1

        return score

    # ------------------------------------------------------------------
    # Trailing punctuation → pause override
    # ------------------------------------------------------------------

    def _trailing_pause(self, text: str, default_ms: int) -> int:
        stripped = text.rstrip()
        for punct, ms in PUNCTUATION_PAUSE.items():
            if stripped.endswith(punct):
                return ms
        return default_ms


class _MergedSent:
    """Lightweight wrapper that merges two spaCy sentences into one logical unit."""

    def __init__(self, s1, s2) -> None:
        self._s1 = s1
        self._s2 = s2
        self.text = s1.text + " " + s2.text
        self.start_char = s1.start_char

    def __iter__(self):
        yield from self._s1
        yield from self._s2
