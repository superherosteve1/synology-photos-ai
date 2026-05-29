from __future__ import annotations

import logging
import mimetypes
import time
from dataclasses import dataclass

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from synology_photos_ai.ai.analyzer import PhotoAnalysis, PhotoAnalyzer
from synology_photos_ai.ai.image_prep import prepare_for_vision
from synology_photos_ai.config import Settings
from synology_photos_ai.store.state import ProcessState, StateStore
from synology_photos_ai.synology.client import PhotoItem, SynologyPhotosClient

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class ProcessStats:
    scanned: int = 0
    skipped: int = 0
    processed: int = 0
    failed: int = 0


class PhotoProcessor:
    def __init__(
        self,
        settings: Settings,
        client: SynologyPhotosClient,
        analyzer: PhotoAnalyzer,
        store: StateStore,
    ) -> None:
        self._settings = settings
        self._client = client
        self._analyzer = analyzer
        self._store = store

    def process_items(
        self,
        items: list[PhotoItem],
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> ProcessStats:
        stats = ProcessStats()
        tag_cache = {tag.name: tag.id for tag in self._client.list_tags()}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Processing photos...", total=len(items))
            for item in items:
                stats.scanned += 1
                progress.update(task, advance=1, description=f"Photo {item.id}: {item.filename}")

                if not self._should_process(item, force=force):
                    stats.skipped += 1
                    continue

                try:
                    self._process_one(
                        item, tag_cache=tag_cache, dry_run=dry_run, force=force
                    )
                    stats.processed += 1
                except Exception:
                    stats.failed += 1
                    logger.exception("Failed to process photo %s (%s)", item.id, item.filename)

        return stats

    def _should_process(self, item: PhotoItem, *, force: bool) -> bool:
        if force:
            return True
        if self._store.is_processed(item.id):
            return False
        if self._settings.skip_if_tagged and item.tags:
            tag_names = {t.get("name", "") for t in item.tags}
            prefix = self._settings.tag_prefix
            if prefix and any(name.startswith(f"{prefix}-") for name in tag_names):
                return False
        return True

    def _process_one(
        self,
        item: PhotoItem,
        *,
        tag_cache: dict[str, int],
        dry_run: bool,
        force: bool = False,
    ) -> PhotoAnalysisResult:
        t0 = time.monotonic()
        image_bytes = self._client.download_thumbnail(
            item, size=self._settings.synology_thumbnail_size
        )
        if not image_bytes:
            raise RuntimeError(f"No thumbnail available for photo {item.id}")
        download_s = time.monotonic() - t0

        mime_type = _guess_mime(item.filename)
        if self._settings.vision_max_edge > 0:
            image_bytes, mime_type = prepare_for_vision(
                image_bytes,
                max_edge=self._settings.vision_max_edge,
            )

        t1 = time.monotonic()
        analysis = self._analyzer.analyze(
            image_bytes=image_bytes,
            filename=item.filename,
            mime_type=mime_type,
        )
        logger.info(
            "Photo %s: NAS thumbnail %.1fs, vision %.1fs",
            item.filename,
            download_s,
            time.monotonic() - t1,
        )

        if dry_run:
            if force:
                old = self._existing_prefix_tag_names(item)
                if old:
                    console.print(f"  [dim]would remove tags: {', '.join(old)}[/dim]")
            console.print(f"[cyan]DRY RUN[/cyan] {item.filename}: {analysis.description}")
            console.print(f"  tags: {', '.join(analysis.tags)}")
            return PhotoAnalysisResult(item=item, analysis=analysis)

        if force:
            old_tag_ids = self._existing_prefix_tag_ids(item)
            if old_tag_ids:
                self._client.remove_tags_from_items([item.id], old_tag_ids)

        # Set description before tags: on some DSM versions Browse.Item.set clears
        # existing tag associations when it runs after add_tag.
        if self._settings.write_description and analysis.description:
            try:
                self._client.set_description(item.id, analysis.description)
                logger.info("Wrote description for photo %s (%s)", item.id, item.filename)
            except Exception as exc:
                logger.warning(
                    "Could not set description for photo %s (%s): %s",
                    item.id,
                    item.filename,
                    exc,
                )
                console.print(
                    f"[yellow]Warning:[/yellow] description not saved for {item.filename}: {exc}"
                )

        if not analysis.tags:
            logger.warning(
                "No tags to apply for photo %s (%s); description may still have been written",
                item.id,
                item.filename,
            )
            console.print(
                f"[yellow]Warning:[/yellow] no tags generated for {item.filename}"
            )
        else:
            tag_ids = [
                self._client.ensure_tag(name, tag_cache)
                for name in analysis.tags
            ]
            self._client.add_tags_to_items([item.id], tag_ids)
            logger.info(
                "Applied %d tag(s) to photo %s (%s): %s",
                len(tag_ids),
                item.id,
                item.filename,
                ", ".join(analysis.tags),
            )

        self._store.mark_processed(
            ProcessState(
                photo_id=item.id,
                filename=item.filename,
                description=analysis.description,
                tags=analysis.tags,
                processed_at=StateStore.now_iso(),
                model=self._settings.openai_model,
            )
        )
        return PhotoAnalysisResult(item=item, analysis=analysis)

    def _existing_prefix_tag_names(self, item: PhotoItem) -> list[str]:
        prefix = self._settings.tag_prefix.strip()
        if not prefix or not item.tags:
            return []
        needle = f"{prefix}-"
        return sorted(
            t.get("name", "")
            for t in item.tags
            if t.get("name", "").startswith(needle)
        )

    def _existing_prefix_tag_ids(self, item: PhotoItem) -> list[int]:
        prefix = self._settings.tag_prefix.strip()
        if not prefix or not item.tags:
            return []
        needle = f"{prefix}-"
        return [
            int(t["id"])
            for t in item.tags
            if t.get("name", "").startswith(needle) and t.get("id") is not None
        ]


@dataclass
class PhotoAnalysisResult:
    item: PhotoItem
    analysis: PhotoAnalysis


def _guess_mime(filename: str) -> "LiteralMime":
    guessed, _ = mimetypes.guess_type(filename)
    if guessed in {"image/jpeg", "image/png", "image/webp"}:
        return guessed  # type: ignore[return-value]
    return "image/jpeg"


LiteralMime = str
