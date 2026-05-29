from __future__ import annotations

import logging
import time

from synology_photos_ai.config import Settings
from synology_photos_ai.pipeline.processor import PhotoProcessor, ProcessStats
from synology_photos_ai.synology.client import SynologyPhotosClient

logger = logging.getLogger(__name__)


class PhotoWatcher:
    def __init__(
        self,
        settings: Settings,
        client: SynologyPhotosClient,
        processor: PhotoProcessor,
    ) -> None:
        self._settings = settings
        self._client = client
        self._processor = processor

    def run_forever(self, *, dry_run: bool = False) -> None:
        interval = self._settings.watch_interval_seconds
        logger.info("Watching for newly added photos every %s seconds", interval)
        while True:
            stats = self.poll_once(dry_run=dry_run)
            logger.info(
                "Watch cycle complete: scanned=%s processed=%s skipped=%s failed=%s",
                stats.scanned,
                stats.processed,
                stats.skipped,
                stats.failed,
            )
            time.sleep(interval)

    def poll_once(self, *, dry_run: bool = False) -> ProcessStats:
        recent = self._client.list_recently_added(limit=500)
        return self._processor.process_items(recent, force=False, dry_run=dry_run)
