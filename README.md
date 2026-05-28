# Lector

An intelligent audiobook reader that converts PDF and EPUB books into natural-sounding audio. Unlike standard text-to-speech tools, Lector analyzes the structure and language of each sentence to dynamically adjust reading pace, tone, and pausing — making it sound like a human narrator rather than a robot.

**100% free. No paid APIs. No cloud dependencies.**

---

## How It Works

Most TTS tools read text at a flat, monotonous pace. Lector splits each chapter into sentence-level segments and assigns **prosody parameters** (rate, pitch, silence) to each one based on linguistic analysis:

| What Lector detects | What it does |
|---|---|
| Chapter / section titles | Reads slower (-20%), adds a 3-second pause before the next line |
| Complex sentences (many clauses, long words) | Slows down (-15%) so the listener can follow |
| Short action sentences | Speeds up (+5%) for energy |
| Dialogue (quoted speech) | Applies deterministic ±10% rate variation per sentence — sounds conversational |
| Ellipsis `...` | Pauses 1 second |
| Em-dash `—` | Pauses 350ms |
| Question marks | Pauses 500ms |
| Exclamation marks | Pauses 400ms |
| Paragraph boundaries | Pauses 900ms |

The result is audio that breathes naturally — the pace shifts with the text, pauses land where they should, and dialogue feels distinct from narration.

---

## Features

- **PDF and EPUB support** with layout-aware parsing (font sizes, bold/italic detection)
- **Natural prosody** via sentence-level rate and pause control
- **Dialogue detection** using regex + spaCy NLP (speech verbs, quote patterns)
- **Complexity scoring** using dependency parsing, clause counting, word length
- **38+ English voices** from Microsoft Edge Neural TTS (free, no API key needed)
- **Concurrent synthesis** with retry logic for reliability
- **Rich terminal UI** with progress bars and chapter tables
- **Play immediately** or save as MP3 files

---

## Requirements

### System dependencies

| Dependency | Install |
|---|---|
| Python 3.10+ | [python.org](https://www.python.org/) |
| ffmpeg | `brew install ffmpeg` (macOS) / `sudo apt install ffmpeg` (Linux) |

### Internet access

Lector uses Microsoft Edge's neural TTS voices, which require an internet connection during synthesis. No API key or account is needed.

---

## Installation

```bash
# 1. Clone or download the project
cd Lector

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Download the spaCy language model
python -m spacy download en_core_web_sm

# 5. Verify ffmpeg is available
ffmpeg -version
```

---

## Usage

### Convert a specific chapter

```bash
python main.py read book.pdf --chapter 3
```

Saves the audio to `output/book/03_Chapter Title.mp3`.

### Convert the entire book

```bash
python main.py read book.pdf --full
```

Creates one MP3 per chapter in `output/book/`.

### Play immediately without saving

```bash
python main.py read book.pdf --chapter 1 --play
```

### Choose a voice

```bash
python main.py read book.epub --chapter 2 --voice en-GB-SoniaNeural
```

### List all chapters in a book

```bash
python main.py list-chapters book.pdf
```

### Browse available voices

```bash
python main.py voices
```

### Specify a custom output directory

```bash
python main.py read book.pdf --full --output-dir ~/Audiobooks/Simpsons
```

### Interactive mode (no flags)

```bash
python main.py read book.pdf
```

Displays a chapter table and prompts you to enter a chapter number or `all`.

---

## Voices

Lector uses Microsoft Edge Neural TTS voices. Some recommended options:

| Voice | Accent | Gender |
|---|---|---|
| `en-US-AriaNeural` | American English | Female (default) |
| `en-US-GuyNeural` | American English | Male |
| `en-GB-SoniaNeural` | British English | Female |
| `en-GB-RyanNeural` | British English | Male |
| `en-AU-NatashaNeural` | Australian English | Female |
| `en-AU-WilliamNeural` | Australian English | Male |
| `en-CA-ClaraNeural` | Canadian English | Female |
| `en-IN-NeerjaNeural` | Indian English | Female |

Run `python main.py voices` to see the full list of 40+ English voices.

---

## Project Structure

```
Lector/
├── main.py                  # CLI entry point (click + rich)
├── config.py                # Prosody rules, voice list, tunable constants
├── requirements.txt
├── output/                  # Generated MP3 files (auto-created)
│
├── parsers/
│   ├── base.py              # FormattedSpan, BookChapter, abstract BookParser
│   ├── pdf_parser.py        # PyMuPDF: extracts text with font metadata
│   └── epub_parser.py       # ebooklib + BeautifulSoup: preserves structure
│
├── analyzer/
│   ├── models.py            # TextSegment, ProsodicHints dataclasses
│   └── text_analyzer.py     # spaCy pipeline + prosody rule engine
│
├── synthesizer/
│   └── tts_engine.py        # Async edge-tts wrapper with retry logic
│
├── assembler/
│   └── audio.py             # pydub: stitch clips + inject silence
│
└── player/
    └── player.py            # pygame playback with system fallback
```

---

## Configuration

All prosody constants live in [`config.py`](config.py). You can tune them without touching any other file:

```python
# Slow down complex sentences more aggressively
PROSODY["complex_narration"]["rate"] = "-25%"

# Longer pause between paragraphs
PARAGRAPH_PAUSE_MS = 1200

# Shorter chapter title pause
PROSODY["chapter_title"]["pause_after"] = 2000
```

---

## Limitations

- **Scanned PDFs** (image-only, no text layer) are not supported. Run `ocrmypdf input.pdf output.pdf` first to add a text layer.
- **Non-English text** — the NLP model (`en_core_web_sm`) is English-only. For other languages, change the spaCy model and choose a matching edge-tts voice.
- **Internet required** — edge-tts calls Microsoft's servers. If offline, synthesis will fail (empty silence is substituted per segment after 3 retries).
- **EPUB DRM** — DRM-protected EPUB files cannot be parsed. Only DRM-free EPUBs are supported.
- **Mathematical notation / symbols** — special characters in math-heavy books may be read oddly by TTS. This is a TTS engine limitation.

---

## Troubleshooting

**`ffmpeg not found`**
```bash
brew install ffmpeg       # macOS
sudo apt install ffmpeg   # Ubuntu/Debian
```

**`spaCy model not found`**
```bash
python -m spacy download en_core_web_sm
```

**`ScannedPDFError`**
```bash
pip install ocrmypdf
ocrmypdf input.pdf output.pdf
python main.py read output.pdf
```

**`edge-tts` network errors**
Lector retries each segment up to 3 times with exponential backoff. If your connection is unstable, failed segments are replaced with silence. Check your internet connection and try again.

**`pygame` audio issues on macOS**
If playback is silent or crashes, use `--output-dir` to save the MP3 and open it manually. The file is always saved before playback begins.

---

## Tech Stack

| Component | Library |
|---|---|
| TTS engine | [edge-tts](https://github.com/rany2/edge-tts) — Microsoft Edge neural voices |
| PDF parsing | [PyMuPDF](https://pymupdf.readthedocs.io/) (fitz) |
| EPUB parsing | [ebooklib](https://github.com/aerkalov/ebooklib) + [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) |
| NLP | [spaCy](https://spacy.io/) (en_core_web_sm) |
| Audio assembly | [pydub](https://github.com/jiaaro/pydub) |
| CLI | [click](https://click.palletsprojects.com/) + [rich](https://github.com/Textualize/rich) |
| Playback | [pygame](https://www.pygame.org/) |

---

## License

MIT
