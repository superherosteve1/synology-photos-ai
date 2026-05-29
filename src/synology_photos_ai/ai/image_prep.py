from __future__ import annotations

import io
from typing import Literal

from PIL import Image

Mime = Literal["image/jpeg", "image/png", "image/webp"]


def prepare_for_vision(
    image_bytes: bytes,
    *,
    max_edge: int,
    jpeg_quality: int = 82,
) -> tuple[bytes, Mime]:
    """Downscale and re-encode for vision APIs (smaller payload = faster inference)."""
    if max_edge <= 0:
        return image_bytes, _detect_mime(image_bytes)

    with Image.open(io.BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        width, height = img.size
        longest = max(width, height)
        if longest > max_edge:
            scale = max_edge / longest
            img = img.resize(
                (int(width * scale), int(height * scale)),
                Image.Resampling.LANCZOS,
            )
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=jpeg_quality, optimize=True)
        return out.getvalue(), "image/jpeg"


def _detect_mime(image_bytes: bytes) -> Mime:
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"
