from __future__ import annotations

from typing import Any

# Order for human-readable place strings (Synology reverse geocode fields).
_ADDRESS_KEYS = (
    "landmark",
    "city",
    "town",
    "village",
    "district",
    "county",
    "state",
    "country",
)


def format_location_prompt(
    address: dict[str, Any] | None,
    gps: dict[str, Any] | None,
) -> str | None:
    """Build a short place string for the vision prompt (address preferred over coordinates)."""
    if address:
        parts: list[str] = []
        seen: set[str] = set()
        for key in _ADDRESS_KEYS:
            raw = address.get(key)
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            if not text:
                continue
            norm = text.lower()
            if norm in seen:
                continue
            parts.append(text)
            seen.add(norm)
        if parts:
            return ", ".join(parts)

    if gps:
        lat = gps.get("latitude")
        lon = gps.get("longitude")
        if lat is not None and lon is not None:
            try:
                return f"{float(lat):.5f}, {float(lon):.5f}"
            except (TypeError, ValueError):
                pass
    return None
