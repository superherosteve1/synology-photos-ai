from __future__ import annotations

import logging
import mimetypes
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from synology_photos_ai.ai.analyzer import PhotoAnalysis, PhotoAnalyzer
from synology_photos_ai.ai.image_prep import prepare_for_vision
from synology_photos_ai.config import Settings
from synology_photos_ai.pipeline.filenames import (
    file_stem,
    is_raw,
    is_raster,
    processing_order_key,
)
from synology_photos_ai.store.state import ProcessState, StateStore
from synology_photos_ai.synology.client import PhotoItem, SynologyPhotosClient
from synology_photos_ai.synology.location import format_location_prompt

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class ProcessStats:
    scanned: int = 0
    skipped: int = 0
    processed: int = 0
    failed: int = 0


@dataclass
class _PendingVision:
    item: PhotoItem
    future: Future[PhotoAnalysis]


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
        ordered = sorted(items, key=processing_order_key)
        parallel = self._settings.process_parallel
        if dry_run or parallel <= 1:
            return self._process_items_sequential(ordered, force=force, dry_run=dry_run)
        return self._process_items_parallel(ordered, force=force, parallel=parallel)

    def _process_items_sequential(
        self,
        ordered: list[PhotoItem],
        *,
        force: bool,
        dry_run: bool,
    ) -> ProcessStats:
        stats = ProcessStats()
        tag_cache = {tag.name: tag.id for tag in self._client.list_tags()}
        analysis_by_stem: dict[str, PhotoAnalysis] = {}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Processing photos...", total=len(ordered))
            for item in ordered:
                stats.scanned += 1
                progress.update(task, advance=1, description=f"Photo {item.id}: {item.filename}")

                if not self._should_process(item, force=force):
                    stats.skipped += 1
                    continue

                try:
                    reused = self._reused_analysis_for_item(item, analysis_by_stem)
                    result = self._process_one(
                        item,
                        tag_cache=tag_cache,
                        dry_run=dry_run,
                        force=force,
                        reused_analysis=reused,
                    )
                    if self._settings.reuse_jpeg_analysis_for_raw and is_raster(item.filename):
                        analysis_by_stem[file_stem(item.filename)] = result.analysis
                    stats.processed += 1
                except Exception:
                    stats.failed += 1
                    logger.exception("Failed to process photo %s (%s)", item.id, item.filename)

        return stats

    def _process_items_parallel(
        self,
        ordered: list[PhotoItem],
        *,
        force: bool,
        parallel: int,
    ) -> ProcessStats:
        stats = ProcessStats()
        tag_cache = {tag.name: tag.id for tag in self._client.list_tags()}
        analysis_by_stem: dict[str, PhotoAnalysis] = {}
        pending: deque[_PendingVision] = deque()
        in_flight_raster: dict[str, Future[PhotoAnalysis]] = {}

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Processing photos...", total=len(ordered))
                for item in ordered:
                    stats.scanned += 1
                    progress.update(
                        task, advance=1, description=f"Photo {item.id}: {item.filename}"
                    )

                    if not self._should_process(item, force=force):
                        stats.skipped += 1
                        continue

                    try:
                        reused = self._reused_analysis_for_item(
                            item,
                            analysis_by_stem,
                            in_flight_raster=in_flight_raster,
                        )
                        if reused is not None:
                            while pending:
                                entry = pending[0]
                                try:
                                    self._apply_pending_head(
                                        pending,
                                        in_flight_raster,
                                        analysis_by_stem=analysis_by_stem,
                                        tag_cache=tag_cache,
                                        force=force,
                                    )
                                    stats.processed += 1
                                except Exception:
                                    stats.failed += 1
                                    logger.exception(
                                        "Failed to process photo %s (%s)",
                                        entry.item.id,
                                        entry.item.filename,
                                    )
                            self._apply_writes(
                                item,
                                reused,
                                tag_cache=tag_cache,
                                force=force,
                                dry_run=False,
                                reused_from=self._reuse_partner_name(item),
                            )
                            stats.processed += 1
                            continue

                        future = executor.submit(self._run_vision, item)
                        stem = file_stem(item.filename)
                        if is_raster(item.filename):
                            in_flight_raster[stem] = future
                        pending.append(_PendingVision(item=item, future=future))

                        while len(pending) >= parallel:
                            entry = pending[0]
                            try:
                                self._apply_pending_head(
                                    pending,
                                    in_flight_raster,
                                    analysis_by_stem=analysis_by_stem,
                                    tag_cache=tag_cache,
                                    force=force,
                                )
                                stats.processed += 1
                            except Exception:
                                stats.failed += 1
                                logger.exception(
                                    "Failed to process photo %s (%s)",
                                    entry.item.id,
                                    entry.item.filename,
                                )
                    except Exception:
                        stats.failed += 1
                        logger.exception(
                            "Failed to process photo %s (%s)", item.id, item.filename
                        )

                while pending:
                    entry = pending[0]
                    try:
                        self._apply_pending_head(
                            pending,
                            in_flight_raster,
                            analysis_by_stem=analysis_by_stem,
                            tag_cache=tag_cache,
                            force=force,
                        )
                        stats.processed += 1
                    except Exception:
                        stats.failed += 1
                        logger.exception(
                            "Failed to process photo %s (%s)",
                            entry.item.id,
                            entry.item.filename,
                        )

        return stats

    def _apply_pending_head(
        self,
        pending: deque[_PendingVision],
        in_flight_raster: dict[str, Future[PhotoAnalysis]],
        *,
        analysis_by_stem: dict[str, PhotoAnalysis],
        tag_cache: dict[str, int],
        force: bool,
    ) -> None:
        entry = pending[0]
        try:
            analysis = entry.future.result()
        except Exception:
            pending.popleft()
            stem = file_stem(entry.item.filename)
            if is_raster(entry.item.filename) and in_flight_raster.get(stem) is entry.future:
                del in_flight_raster[stem]
            raise

        pending.popleft()
        stem = file_stem(entry.item.filename)
        if is_raster(entry.item.filename) and in_flight_raster.get(stem) is entry.future:
            del in_flight_raster[stem]

        result = self._apply_writes(
            entry.item,
            analysis,
            tag_cache=tag_cache,
            force=force,
            dry_run=False,
        )
        if self._settings.reuse_jpeg_analysis_for_raw and is_raster(entry.item.filename):
            analysis_by_stem[stem] = result.analysis

    def _reuse_partner_name(self, item: PhotoItem) -> str:
        partner = self._store.find_raster_analysis_for_stem(file_stem(item.filename))
        return partner.filename if partner else "JPEG partner in this batch"

    def _reused_analysis_for_item(
        self,
        item: PhotoItem,
        analysis_by_stem: dict[str, PhotoAnalysis],
        in_flight_raster: dict[str, Future[PhotoAnalysis]] | None = None,
    ) -> PhotoAnalysis | None:
        if not self._settings.reuse_jpeg_analysis_for_raw or not is_raw(item.filename):
            return None
        stem = file_stem(item.filename)
        cached = analysis_by_stem.get(stem)
        if cached is not None:
            return cached
        if in_flight_raster is not None and stem in in_flight_raster:
            analysis = in_flight_raster.pop(stem).result()
            analysis_by_stem[stem] = analysis
            return analysis
        stored = self._store.find_raster_analysis_for_stem(stem)
        if stored is None:
            return None
        return PhotoAnalysis(description=stored.description, tags=list(stored.tags))

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

    def _location_context_for_item(self, item: PhotoItem) -> str | None:
        if not self._settings.use_location_in_prompt:
            return None
        return format_location_prompt(item.address, item.gps)

    def _process_one(
        self,
        item: PhotoItem,
        *,
        tag_cache: dict[str, int],
        dry_run: bool,
        force: bool = False,
        reused_analysis: PhotoAnalysis | None = None,
    ) -> PhotoAnalysisResult:
        if reused_analysis is not None:
            return self._apply_writes(
                item,
                reused_analysis,
                tag_cache=tag_cache,
                dry_run=dry_run,
                force=force,
                reused_from=self._reuse_partner_name(item),
            )
        analysis = self._run_vision(item)
        return self._apply_writes(
            item,
            analysis,
            tag_cache=tag_cache,
            dry_run=dry_run,
            force=force,
        )

    def _run_vision(self, item: PhotoItem) -> PhotoAnalysis:
        t0 = time.monotonic()
        image_bytes = self._client.download_thumbnail(
            item, size=self._settings.synology_thumbnail_size
        )
        if not image_bytes:
            ext = item.filename.rsplit(".", 1)[-1].lower() if "." in item.filename else ""
            hint = (
                " RAW/NEF files may need time to index — retry later, or check thumbnail "
                "status in Photos."
                if ext in {"nef", "nrw", "arw", "cr2", "cr3", "dng", "orf", "raf", "rw2"}
                else ""
            )
            raise RuntimeError(
                f"No thumbnail available for photo {item.id} ({item.filename}).{hint}"
            )
        download_s = time.monotonic() - t0

        mime_type = _guess_mime(item.filename)
        if self._settings.vision_max_edge > 0:
            image_bytes, mime_type = prepare_for_vision(
                image_bytes,
                max_edge=self._settings.vision_max_edge,
            )

        t1 = time.monotonic()
        location_context = self._location_context_for_item(item)
        analysis = self._analyzer.analyze(
            image_bytes=image_bytes,
            filename=item.filename,
            mime_type=mime_type,
            location_context=location_context,
        )
        logger.info(
            "Photo %s: NAS thumbnail %.1fs, vision %.1fs",
            item.filename,
            download_s,
            time.monotonic() - t1,
        )
        return analysis

    def _apply_writes(
        self,
        item: PhotoItem,
        analysis: PhotoAnalysis,
        *,
        tag_cache: dict[str, int],
        dry_run: bool,
        force: bool,
        reused_from: str | None = None,
    ) -> PhotoAnalysisResult:
        if reused_from is not None:
            logger.info(
                "Reusing analysis from %s for %s (no vision call)",
                reused_from,
                item.filename,
            )

        if dry_run:
            if force:
                old = self._existing_prefix_tag_names(item)
                if old:
                    console.print(f"  [dim]would remove tags: {', '.join(old)}[/dim]")
            console.print(f"[cyan]DRY RUN[/cyan] {item.filename}")
            if self._settings.write_description:
                console.print(f"  description: {analysis.description}")
            else:
                console.print(
                    f"  [dim]description (WRITE_DESCRIPTION=false — would not write): "
                    f"{analysis.description}[/dim]"
                )
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
            self._apply_tags(item, analysis.tags, tag_cache=tag_cache)

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

    def _apply_tags(
        self,
        item: PhotoItem,
        tag_names: list[str],
        *,
        tag_cache: dict[str, int],
    ) -> None:
        tag_ids = [self._client.ensure_tag(name, tag_cache) for name in tag_names]
        self._client.add_tags_to_items([item.id], tag_ids)
        logger.info(
            "Applied %d tag(s) to photo %s (%s): %s",
            len(tag_ids),
            item.id,
            item.filename,
            ", ".join(tag_names),
        )
        missing = self._tags_missing_on_nas(item.id, expected=tag_names)
        if not missing:
            return
        logger.warning(
            "NAS missing %d tag(s) after add_tag for %s (%s): %s",
            len(missing),
            item.id,
            item.filename,
            ", ".join(missing),
        )
        console.print(
            f"[yellow]Warning:[/yellow] re-applying {len(missing)} tag(s) one-by-one for "
            f"{item.filename}"
        )
        for name in missing:
            tag_id = self._client.ensure_tag(name, tag_cache)
            self._client.add_tag_to_item(item.id, tag_id)
        still_missing = self._tags_missing_on_nas(item.id, expected=tag_names)
        if still_missing:
            logger.warning(
                "Tags still missing on NAS for %s (%s): %s",
                item.id,
                item.filename,
                ", ".join(still_missing),
            )
            console.print(
                f"[yellow]Warning:[/yellow] tags not visible on NAS for {item.filename}: "
                f"{', '.join(still_missing)} — check the JPG/NEF pair in Photos"
            )

    def _tags_missing_on_nas(self, item_id: int, *, expected: list[str]) -> list[str]:
        try:
            fresh = self._client.get_item(item_id)
        except Exception as exc:
            logger.warning(
                "Could not verify tags on NAS for item %s (tags may still be applied): %s",
                item_id,
                exc,
            )
            return []
        on_nas = {t.get("name", "") for t in fresh.tags}
        return [name for name in expected if name not in on_nas]

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
