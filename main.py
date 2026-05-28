#!/usr/bin/env python3
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich import print as rprint

console = Console()


def _check_ffmpeg() -> None:
    result = subprocess.run(
        ["ffmpeg", "-version"],
        capture_output=True,
    )
    if result.returncode != 0:
        console.print(
            "[bold red]Error:[/bold red] ffmpeg is not installed or not on PATH.\n"
            "Install it with:  [cyan]brew install ffmpeg[/cyan]  (macOS)\n"
            "                  [cyan]sudo apt install ffmpeg[/cyan]  (Linux)"
        )
        sys.exit(1)


def _get_parser(filepath: Path):
    from parsers.pdf_parser import PDFParser
    from parsers.epub_parser import EPUBParser

    ext = filepath.suffix.lower()
    if ext == ".pdf":
        return PDFParser(filepath)
    elif ext in (".epub",):
        return EPUBParser(filepath)
    else:
        console.print(f"[bold red]Unsupported file format:[/bold red] {ext}")
        sys.exit(1)


def _print_chapters(chapters) -> None:
    table = Table(title="Chapters", show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Title")
    for ch in chapters:
        table.add_row(str(ch.number), ch.title)
    console.print(table)


def _convert_chapter(chapter, voice: str, output_path: Path) -> None:
    from analyzer.text_analyzer import TextAnalyzer
    from synthesizer.tts_engine import TTSEngine
    from assembler.audio import assemble_chapter, export_chapter

    analyzer = TextAnalyzer()
    engine = TTSEngine(voice=voice)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Analyzing '{chapter.title}'…", total=3)

        segments = analyzer.analyze_chapter(chapter)
        progress.update(task, advance=1, description="Synthesizing audio…")

        audio_clips = engine.synthesize_chapter(segments)
        progress.update(task, advance=1, description="Assembling audio…")

        audio = assemble_chapter(segments, audio_clips)
        export_chapter(audio, output_path)
        progress.update(task, advance=1, description="Done.")

    duration_s = len(audio) / 1000
    mins, secs = divmod(int(duration_s), 60)
    console.print(
        f"[green]✓[/green] Saved [bold]{output_path}[/bold] "
        f"({mins}m {secs}s, {len(segments)} segments)"
    )


def _play_chapter(chapter, voice: str) -> None:
    from analyzer.text_analyzer import TextAnalyzer
    from synthesizer.tts_engine import TTSEngine
    from assembler.audio import assemble_chapter
    from player.player import Player

    analyzer = TextAnalyzer()
    engine = TTSEngine(voice=voice)
    player = Player()

    with console.status(f"Preparing '[bold]{chapter.title}[/bold]'…"):
        segments = analyzer.analyze_chapter(chapter)
        audio_clips = engine.synthesize_chapter(segments)
        audio = assemble_chapter(segments, audio_clips)

    console.print(f"[cyan]▶[/cyan] Playing [bold]{chapter.title}[/bold]…  (Ctrl+C to stop)")
    try:
        player.play_segment(audio)
    except KeyboardInterrupt:
        player.stop()
        console.print("\n[yellow]Playback stopped.[/yellow]")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.argument("book", type=click.Path(exists=True, path_type=Path))
@click.option("--voice", "-v", default=None, help="Voice name (see lector voices)")
@click.option("--chapter", "-c", type=int, default=None, help="Chapter number to convert")
@click.option("--full", "-f", is_flag=True, help="Convert entire book")
@click.option("--play", "-p", is_flag=True, help="Play immediately instead of saving")
@click.option("--output-dir", "-o", type=click.Path(path_type=Path), default=None,
              help="Output directory (default: ./output/<book_name>/)")
def read(book: Path, voice, chapter, full, play, output_dir):
    """Convert a PDF or EPUB book to natural-sounding audio."""
    _check_ffmpeg()

    from config import DEFAULT_VOICE
    voice = voice or DEFAULT_VOICE

    parser = _get_parser(book)

    with console.status("Parsing book…"):
        try:
            chapters = parser.parse()
        except Exception as e:
            console.print(f"[bold red]Parse error:[/bold red] {e}")
            sys.exit(1)

    if not chapters:
        console.print("[bold red]No chapters found in this file.[/bold red]")
        sys.exit(1)

    console.print(
        f"\n[bold]{book.name}[/bold] — "
        f"[cyan]{len(chapters)} chapter(s)[/cyan]  |  voice: [yellow]{voice}[/yellow]\n"
    )

    # Determine which chapters to process
    if full:
        targets = chapters
    elif chapter is not None:
        matching = [ch for ch in chapters if ch.number == chapter]
        if not matching:
            console.print(f"[red]Chapter {chapter} not found.[/red]")
            _print_chapters(chapters)
            sys.exit(1)
        targets = matching
    else:
        _print_chapters(chapters)
        choice = click.prompt(
            "\nEnter chapter number to convert (or 'all')",
            default="1",
        )
        if choice.strip().lower() == "all":
            targets = chapters
        else:
            try:
                num = int(choice)
                targets = [ch for ch in chapters if ch.number == num]
                if not targets:
                    console.print(f"[red]Chapter {num} not found.[/red]")
                    sys.exit(1)
            except ValueError:
                console.print("[red]Invalid input.[/red]")
                sys.exit(1)

    # Output directory
    if output_dir is None:
        output_dir = Path("output") / book.stem

    # Process
    for ch in targets:
        if play:
            _play_chapter(ch, voice)
        else:
            from assembler.audio import chapter_filename
            fname = chapter_filename(book.stem, ch.number, ch.title)
            out_path = output_dir / fname
            _convert_chapter(ch, voice, out_path)


@cli.command()
def voices():
    """List available English voices."""
    from synthesizer.tts_engine import TTSEngine

    with console.status("Fetching voice list…"):
        try:
            voice_list = TTSEngine.list_voices()
        except Exception as e:
            console.print(f"[red]Could not fetch voices: {e}[/red]")
            from config import VOICES
            voice_list = VOICES

    table = Table(title="Available Voices", header_style="bold cyan")
    table.add_column("Voice Name")
    table.add_column("Locale")
    table.add_column("Gender (inferred)")
    for v in sorted(voice_list):
        parts = v.split("-")
        locale = "-".join(parts[:2]) if len(parts) >= 2 else "?"
        gender = "Female" if any(
            n in v for n in ("Aria", "Jenny", "Sonia", "Natasha", "Clara", "Neerja", "Emma")
        ) else "Male"
        table.add_row(v, locale, gender)
    console.print(table)


@cli.command("list-chapters")
@click.argument("book", type=click.Path(exists=True, path_type=Path))
def list_chapters(book: Path):
    """List all chapters in a book without converting."""
    parser = _get_parser(book)
    with console.status("Parsing book…"):
        try:
            chapters = parser.parse()
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
    _print_chapters(chapters)


if __name__ == "__main__":
    cli()
