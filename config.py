DEFAULT_VOICE = "en-US-AriaNeural"

VOICES = [
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural",
    "en-AU-NatashaNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
    "en-IN-NeerjaNeural",
]

# Per-segment-type prosody defaults
PROSODY = {
    "chapter_title": dict(rate="-20%", pitch="+0Hz", pause_before=2000, pause_after=3000),
    "section_heading": dict(rate="-15%", pitch="+0Hz", pause_before=1000, pause_after=2000),
    "dialogue": dict(rate="+0%", pitch="+0Hz", pause_before=0, pause_after=50),
    "complex_narration": dict(rate="-15%", pitch="+0Hz", pause_before=0, pause_after=650),
    "action_narration": dict(rate="+5%", pitch="+0Hz", pause_before=0, pause_after=550),
    "default_narration": dict(rate="+0%", pitch="+0Hz", pause_before=0, pause_after=600),
}

# Pause overrides by trailing punctuation (ms), applied on top of segment defaults
PUNCTUATION_PAUSE = {
    "?": 500,
    "!": 400,
    "...": 1000,
    "…": 1000,
    "—": 350,
}

PARAGRAPH_PAUSE_MS = 900
SECTION_BREAK_PAUSE_MS = 2000

# Heading font size ratio: spans >= this × median size are treated as headings
HEADING_FONT_RATIO = 1.4

# TTS concurrency
TTS_MAX_CONCURRENCY = 3
TTS_RETRY_ATTEMPTS = 3

# Audio normalization
AUDIO_FRAME_RATE = 24000
AUDIO_CHANNELS = 1
AUDIO_BITRATE = "128k"

SPEECH_VERBS = {
    "said", "asked", "replied", "whispered", "shouted", "muttered",
    "cried", "called", "answered", "exclaimed", "murmured", "declared",
    "stated", "announced", "responded", "questioned", "demanded",
}

ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "vs", "etc", "jr", "sr",
    "prof", "rev", "gen", "sgt", "cpl", "pvt", "dept", "approx",
    "vol", "no", "fig", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
}

SKIP_EPUB_KEYWORDS = {"cover", "toc", "copyright", "ncx", "nav", "title-page"}
