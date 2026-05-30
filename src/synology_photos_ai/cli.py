from __future__ import annotations

import logging
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from synology_photos_ai.ai.analyzer import PhotoAnalyzer
from synology_photos_ai.config import Settings
from synology_photos_ai.pipeline.processor import PhotoProcessor, ProcessStats
from synology_photos_ai.pipeline.watcher import PhotoWatcher
from synology_photos_ai.store.state import StateStore
from synology_photos_ai.synology.client import SynologyPhotosClient

app = typer.Typer(
    no_args_is_help=True,
    help="Describe Synology Photos and apply AI-generated tags via OpenAI/LangChain.",
)
console = Console()


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@app.command("ping")
def ping(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Verify Synology Photos login."""
    _configure_logging(verbose)
    settings = _load_settings()
    with SynologyPhotosClient(settings) as client:
        client.login()
        count = client.count_photos()
        console.print(f"[green]Connected[/green] — {count} items in {settings.synology_space.value} space")


@app.command("process")
def process(
    folder_id: Annotated[
        Optional[int], typer.Option(help="Limit to a Synology folder id")
    ] = None,
    limit: Annotated[
        Optional[int], typer.Option(help="Process at most N photos (after filters)")
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            help=(
                "Re-process photos even if already in local state or tagged with ai-*; "
                "removes old ai-* tags and applies new tags. Descriptions are written "
                "only when WRITE_DESCRIPTION=true"
            )
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Analyze only; do not write tags or descriptions")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Batch describe and tag photos."""
    _configure_logging(verbose)
    settings = _load_settings()
    store = StateStore(settings.state_path)
    try:
        with SynologyPhotosClient(settings) as client:
            client.login()
            items = client.list_photos(folder_id=folder_id, limit=limit)
            processor = PhotoProcessor(
                settings,
                client,
                PhotoAnalyzer(settings),
                store,
            )
            stats = processor.process_items(items, force=force, dry_run=dry_run)
    finally:
        store.close()

    _print_stats(stats)


@app.command("watch")
def watch(
    dry_run: Annotated[bool, typer.Option(help="Analyze only; do not write")] = False,
    once: Annotated[bool, typer.Option(help="Run one poll cycle and exit")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Poll Recently Added and tag new photos."""
    _configure_logging(verbose)
    settings = _load_settings()
    store = StateStore(settings.state_path)
    try:
        with SynologyPhotosClient(settings) as client:
            client.login()
            processor = PhotoProcessor(
                settings,
                client,
                PhotoAnalyzer(settings),
                store,
            )
            watcher = PhotoWatcher(settings, client, processor)
            if once:
                stats = watcher.poll_once(dry_run=dry_run)
                _print_stats(stats)
            else:
                watcher.run_forever(dry_run=dry_run)
    finally:
        store.close()


@app.command("status")
def status(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Show local processing state."""
    _configure_logging(verbose)
    settings = _load_settings()
    store = StateStore(settings.state_path)
    try:
        table = Table(title="Processed photos (local state)")
        table.add_column("Photo ID")
        table.add_column("Filename")
        table.add_column("Tags")
        table.add_column("Processed at")
        for row in store.recent(limit=20):
            table.add_row(
                str(row.photo_id),
                row.filename,
                ", ".join(row.tags),
                row.processed_at,
            )
        console.print(table)
        console.print(f"Total processed locally: {store.count()}")
    finally:
        store.close()


def _print_stats(stats: ProcessStats) -> None:
    console.print(
        f"Done — scanned={stats.scanned} processed={stats.processed} "
        f"skipped={stats.skipped} failed={stats.failed}"
    )


if __name__ == "__main__":
    app()
