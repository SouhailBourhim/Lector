# Lector

An intelligent audiobook reader that converts PDF and EPUB books into natural-sounding audio. Unlike standard TTS tools, Lector analyzes each sentence's structure to dynamically adjust reading pace, tone, and pausing — making it sound like a human narrator rather than a robot.

**100% free. No paid APIs. No cloud dependencies.**

**Stack:** Python 3.10+ · FastAPI · spaCy · edge-tts · PyMuPDF · ebooklib · pydub · WaveSurfer.js · Docker

The interesting part is not the text-to-speech — that is one library call. It is the layer in front of
it: a spaCy dependency-parse pass that scores each sentence for complexity, detects dialogue, and emits
per-sentence rate and pause parameters, so the output has the shape of narration instead of the flat
cadence of a screen reader.

---

## How It Works

Lector splits each chapter into sentence-level segments and assigns **prosody parameters** (rate, pitch, silence) to each one based on linguistic analysis:

| What Lector detects | What it does |
|---|---|
| Chapter / section titles | Reads slower (−20%), adds a 3-second pause after |
| Complex sentences (many clauses, long words) | Slows down (−15%) |
| Short action sentences | Speeds up (+5%) for energy |
| Dialogue (quoted speech) | Deterministic ±10% rate variation per sentence |
| Ellipsis `...` | 1-second pause |
| Em-dash `—` | 350ms pause |
| Question marks | 500ms pause |
| Paragraph boundaries | 900ms pause |

---

## Features

- **PDF and EPUB support** with layout-aware parsing (font sizes, bold/italic detection)
- **Natural prosody** via sentence-level rate and pause control
- **Dialogue detection** using regex + spaCy NLP (speech verbs, quote patterns)
- **Complexity scoring** using dependency parsing, clause counting, word length
- **8 English voices** from Microsoft Edge Neural TTS (free, no API key needed)
- **Web interface** — upload, select chapters, watch live progress, play or download
- **Progressive audio** — chapters appear as they finish; play chapter 1 while chapter 2 synthesizes
- **Disk cache** — re-converting the same chapter with the same voice is instant
- **CLI** for scripting and headless use
- **Docker-ready** for Railway / Render deployment

---

## Requirements

| Dependency | Install |
|---|---|
| Python 3.10+ | [python.org](https://www.python.org/) |
| ffmpeg | `brew install ffmpeg` (macOS) · `sudo apt install ffmpeg` (Linux) |

Lector uses Microsoft Edge's neural TTS, which requires an internet connection during synthesis. No API key or account is needed.

---

## Installation

```bash
# 1. Clone / download the project
cd Lector

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Download the spaCy language model
python -m spacy download en_core_web_sm

# 5. Verify ffmpeg
ffmpeg -version
```

---

## Web Interface

```bash
uvicorn server:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000), upload a PDF or EPUB, pick chapters and a voice, then watch the live progress. Audio cards appear as each chapter finishes — you can start playing immediately without waiting for the full book.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Server port |
| `TTS_CONCURRENCY` | `10` | Parallel TTS requests |
| `MAX_UPLOAD_MB` | `50` | Upload size limit |
| `CACHE_DIR` | `/tmp/lector_cache` | Chapter audio cache (7-day TTL) |

Copy `.env.example` to `.env` and adjust as needed.

---

## CLI Usage

### Convert a specific chapter

```bash
python main.py read book.pdf --chapter 3
```

### Convert the entire book

```bash
python main.py read book.pdf --full
```

### Play immediately without saving

```bash
python main.py read book.pdf --chapter 1 --play
```

### Choose a voice

```bash
python main.py read book.epub --chapter 2 --voice en-GB-SoniaNeural
```

### List chapters

```bash
python main.py list-chapters book.pdf
```

### Browse available voices

```bash
python main.py voices
```

### Custom output directory

```bash
python main.py read book.pdf --full --output-dir ~/Audiobooks
```

---

## Voices

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

---

## Project Structure

```
Lector/
├── server.py                # FastAPI web server
├── main.py                  # CLI entry point (click + rich)
├── config.py                # Prosody rules, voice list, env config
├── requirements.txt
├── Dockerfile
├── railway.toml             # Railway deployment config
├── render.yaml              # Render deployment config
├── .env.example
│
├── templates/
│   └── index.html           # Single-page web UI
│
├── static/
│   ├── style.css            # Dark theme
│   └── app.js               # Upload, SSE progress, WaveSurfer player
│
├── parsers/
│   ├── base.py              # FormattedSpan, BookChapter, abstract BookParser
│   ├── pdf_parser.py        # PyMuPDF: text + font metadata
│   └── epub_parser.py       # ebooklib + BeautifulSoup4
│
├── analyzer/
│   ├── models.py            # TextSegment, ProsodicHints
│   └── text_analyzer.py     # spaCy pipeline + prosody rule engine
│
├── synthesizer/
│   └── tts_engine.py        # Async edge-tts wrapper with retry + semaphore
│
├── assembler/
│   └── audio.py             # pydub: stitch clips + inject silence
│
└── player/
    └── player.py            # pygame playback (CLI only)
```

---

## Deployment

### Docker

```bash
docker build -t lector .
docker run -p 8000:8000 lector
```

### Railway

Push the repo and connect it to Railway — `railway.toml` configures the build, health check, and persistent cache volume automatically.

### Render

`render.yaml` is pre-configured with a 5 GB persistent disk for the audio cache. Connect your repo and deploy.

---

## Configuration

All prosody constants are in [`config.py`](config.py):

```python
# Slow down complex sentences more aggressively
PROSODY["complex_narration"]["rate"] = "-25%"

# Longer pause between paragraphs
PARAGRAPH_PAUSE_MS = 1200

# More parallel TTS requests (also settable via TTS_CONCURRENCY env var)
TTS_MAX_CONCURRENCY = 15
```

---

## Limitations

- **Scanned PDFs** (image-only) are not supported. Run `ocrmypdf input.pdf output.pdf` first.
- **Non-English text** — `en_core_web_sm` is English-only. Swap the spaCy model and voice for other languages.
- **Internet required** — edge-tts calls Microsoft's servers. Failed segments fall back to silence after 3 retries.
- **EPUB DRM** — only DRM-free EPUBs can be parsed.
- **Mathematical notation** — symbols in math-heavy books may be read oddly by the TTS engine.

---

## Troubleshooting

**`ffmpeg not found`**
```bash
brew install ffmpeg        # macOS
sudo apt install ffmpeg    # Ubuntu/Debian
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

**Port already in use**
```bash
lsof -ti :8000 | xargs kill -9
```

---

## Tech Stack

| Component | Library |
|---|---|
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| TTS engine | [edge-tts](https://github.com/rany2/edge-tts) — Microsoft Edge neural voices |
| PDF parsing | [PyMuPDF](https://pymupdf.readthedocs.io/) (fitz) |
| EPUB parsing | [ebooklib](https://github.com/aerkalov/ebooklib) + [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) |
| NLP | [spaCy](https://spacy.io/) (en_core_web_sm) |
| Audio assembly | [pydub](https://github.com/jiaaro/pydub) |
| Waveform player | [WaveSurfer.js](https://wavesurfer.xyz/) |
| CLI | [click](https://click.palletsprojects.com/) + [rich](https://github.com/Textualize/rich) |

---

## License

MIT
