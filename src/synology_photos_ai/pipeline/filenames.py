from __future__ import annotations

from synology_photos_ai.synology.client import PhotoItem

_RASTER_EXTS = frozenset(
    {"jpg", "jpeg", "heic", "heif", "png", "webp", "gif", "tif", "tiff"}
)
_RAW_EXTS = frozenset(
    {"nef", "nrw", "arw", "cr2", "cr3", "dng", "orf", "raf", "rw2", "raw"}
)


def file_stem(filename: str) -> str:
    return filename.rsplit(".", 1)[0].lower() if "." in filename else filename.lower()


def file_ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def is_raw(filename: str) -> bool:
    return file_ext(filename) in _RAW_EXTS


def is_raster(filename: str) -> bool:
    ext = file_ext(filename)
    if ext in _RASTER_EXTS:
        return True
    return bool(ext) and ext not in _RAW_EXTS


def processing_order_key(item: PhotoItem) -> tuple[str, int, str]:
    """Raster partners before RAW so analysis can be reused for NEF/etc."""
    stem = file_stem(item.filename)
    priority = 1 if is_raw(item.filename) else 0
    return (stem, priority, item.filename.lower())
