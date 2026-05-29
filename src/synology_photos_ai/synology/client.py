from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import httpx

from synology_photos_ai.config import Settings, Space
from synology_photos_ai.synology.spaces import ApiNames, api_names_for

logger = logging.getLogger(__name__)

PAGE_SIZE = 500
LIST_ADDITIONAL = [
    "thumbnail",
    "resolution",
    "tag",
    "description",
]


@dataclass
class PhotoItem:
    id: int
    filename: str
    item_type: str
    folder_id: int | None
    indexed_time: int | None
    thumbnail: dict[str, Any] | None
    tags: list[dict[str, Any]] = field(default_factory=list)
    description: str | None = None

    @property
    def is_photo(self) -> bool:
        return self.item_type in {"photo", "live"}


@dataclass
class TagInfo:
    id: int
    name: str


class SynologyPhotosClient:
    """Thin client for Synology Photos web APIs (unofficial)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api = api_names_for(settings.synology_space)
        self._sid: str | None = None
        self._client = httpx.Client(
            verify=settings.synology_verify_ssl,
            timeout=httpx.Timeout(120.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SynologyPhotosClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def login(self) -> None:
        data = {
            "api": "SYNO.API.Auth",
            "version": "3",
            "method": "login",
            "account": self._settings.synology_username,
            "passwd": self._settings.synology_password,
            "session": "Foto",
        }
        payload = self._post(data)
        self._sid = payload["data"]["sid"]
        logger.info("Logged in to Synology Photos (%s space)", self._settings.synology_space.value)

    def logout(self) -> None:
        if not self._sid:
            return
        self._post(
            {
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "logout",
            }
        )
        self._sid = None

    def _post(self, data: dict[str, Any]) -> dict[str, Any]:
        if self._sid:
            data = {**data, "_sid": self._sid}
        response = self._client.post(self._settings.api_base_url, data=data)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            error = payload.get("error") or {}
            code = error.get("code", "unknown")
            types = error.get("errors", {}).get("types", [])
            if code == 403 and any(t.get("type") in {"otp", "authenticator"} for t in types):
                raise RuntimeError(
                    "Synology login requires 2FA (OTP/authenticator). Use a dedicated DSM user "
                    "without 2FA, or an app-specific password if your DSM version supports it."
                )
            raise RuntimeError(f"Synology API error {code}: {data.get('api')}.{data.get('method')}")
        return payload

    @staticmethod
    def _json_list(value: list[Any] | Any) -> str:
        if isinstance(value, list):
            return json.dumps(value)
        return json.dumps([value])

    def _parse_item(self, raw: dict[str, Any]) -> PhotoItem:
        additional = raw.get("additional") or {}
        return PhotoItem(
            id=raw["id"],
            filename=raw.get("filename", ""),
            item_type=raw.get("type", "photo"),
            folder_id=raw.get("folder_id"),
            indexed_time=raw.get("indexed_time"),
            thumbnail=(additional.get("thumbnail") or None),
            tags=list(additional.get("tag") or []),
            description=additional.get("description"),
        )

    def count_photos(self, *, folder_id: int | None = None) -> int:
        return self._count_photos(folder_id=folder_id)

    def list_photos(
        self, *, folder_id: int | None = None, limit: int | None = None
    ) -> list[PhotoItem]:
        """List photos; stop early when ``limit`` is set (avoids paging the whole library)."""
        if limit is None:
            return self.list_all_photos(folder_id=folder_id)

        items: list[PhotoItem] = []
        offset = 0
        while len(items) < limit:
            page = self._list_photos_page(
                offset=offset, limit=PAGE_SIZE, folder_id=folder_id
            )
            if not page:
                break
            for item in page:
                if item.is_photo:
                    items.append(item)
                    if len(items) >= limit:
                        return items[:limit]
            offset += PAGE_SIZE
        return items

    def list_all_photos(self, *, folder_id: int | None = None) -> list[PhotoItem]:
        total = self._count_photos(folder_id=folder_id)
        items: list[PhotoItem] = []
        offset = 0
        while offset < total:
            batch = min(PAGE_SIZE, total - offset)
            items.extend(self._list_photos_page(offset=offset, limit=batch, folder_id=folder_id))
            offset += batch
        return [item for item in items if item.is_photo]

    def list_recently_added(self, *, limit: int = 200) -> list[PhotoItem]:
        payload = self._post(
            {
                "api": self._api.browse_recently_added,
                "version": "2",
                "method": "list",
                "offset": 0,
                "limit": limit,
                "additional": self._json_list(LIST_ADDITIONAL),
            }
        )
        return [
            self._parse_item(raw)
            for raw in payload["data"]["list"]
            if self._parse_item(raw).is_photo
        ]

    def _count_photos(self, *, folder_id: int | None) -> int:
        data: dict[str, Any] = {
            "api": self._api.browse_item,
            "version": "1",
            "method": "count",
        }
        if folder_id is not None:
            data["folder_id"] = folder_id
        payload = self._post(data)
        return int(payload["data"]["count"])

    def _list_photos_page(
        self, *, offset: int, limit: int, folder_id: int | None
    ) -> list[PhotoItem]:
        data: dict[str, Any] = {
            "api": self._api.browse_item,
            "version": "1",
            "method": "list",
            "offset": offset,
            "limit": limit,
            "additional": self._json_list(LIST_ADDITIONAL),
        }
        if folder_id is not None:
            data["folder_id"] = folder_id
        payload = self._post(data)
        return [self._parse_item(raw) for raw in payload["data"]["list"]]

    def download_thumbnail(self, item: PhotoItem, *, size: str = "xl") -> bytes | None:
        thumb = item.thumbnail
        if not thumb:
            logger.warning("Photo %s (%s) has no thumbnail metadata from list API", item.id, item.filename)
            return None
        unit_id = thumb.get("unit_id", item.id)
        cache_key = thumb.get("cache_key")
        if not cache_key:
            logger.warning(
                "Photo %s (%s) missing thumbnail cache_key (states: %s)",
                item.id,
                item.filename,
                {k: thumb.get(k) for k in ("sm", "m", "xl", "preview")},
            )
            return None
        seen: set[str] = set()
        for candidate in (size, "m", "sm", "xl"):
            if candidate in seen:
                continue
            seen.add(candidate)
            state = str(thumb.get(candidate, "")).lower()
            if state != "ready":
                continue
            response = self._client.post(
                self._settings.api_base_url,
                data={
                    "api": self._api.thumbnail,
                    "version": "1",
                    "method": "get",
                    "_sid": self._sid,
                    "id": unit_id,
                    "type": "unit",
                    "size": candidate,
                    "cache_key": cache_key,
                },
            )
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                continue
            return response.content
        logger.warning(
            "No ready thumbnail for photo %s (%s); NAS states sm=%s m=%s xl=%s preview=%s",
            item.id,
            item.filename,
            thumb.get("sm"),
            thumb.get("m"),
            thumb.get("xl"),
            thumb.get("preview"),
        )
        return None

    def list_tags(self) -> list[TagInfo]:
        count_payload = self._post(
            {
                "api": self._api.browse_general_tag,
                "version": "1",
                "method": "count",
            }
        )
        total = int(count_payload["data"]["count"])
        tags: list[TagInfo] = []
        offset = 0
        while offset < total:
            batch = min(PAGE_SIZE, total - offset)
            payload = self._post(
                {
                    "api": self._api.browse_general_tag,
                    "version": "1",
                    "method": "list",
                    "offset": offset,
                    "limit": batch,
                }
            )
            for row in payload["data"]["list"]:
                tags.append(TagInfo(id=row["id"], name=row["name"]))
            offset += batch
        return tags

    def ensure_tag(self, name: str, cache: dict[str, int]) -> int:
        if name in cache:
            return cache[name]
        payload = self._post(
            {
                "api": self._api.browse_general_tag,
                "version": "1",
                "method": "create",
                "name": name,
            }
        )
        tag_id = int(payload["data"]["tag"]["id"])
        cache[name] = tag_id
        return tag_id

    def _parse_get_item_data(self, data: dict[str, Any], item_id: int) -> PhotoItem:
        """Browse.Item.get often returns data.list (batch), not a single top-level item."""
        if "list" in data and isinstance(data["list"], list):
            rows = data["list"]
            for raw in rows:
                if isinstance(raw, dict) and raw.get("id") == item_id:
                    return self._parse_item(raw)
            if len(rows) == 1 and isinstance(rows[0], dict):
                return self._parse_item(rows[0])
        if isinstance(data.get("item"), dict):
            return self._parse_item(data["item"])
        if "id" in data:
            return self._parse_item(data)
        raise RuntimeError(
            f"Unexpected get item response for id {item_id}: keys={list(data.keys())}"
        )

    def get_item(self, item_id: int) -> PhotoItem:
        last_error: Exception | None = None
        for id_value in (self._json_list([item_id]), item_id):
            try:
                payload = self._post(
                    {
                        "api": self._api.browse_item,
                        "version": "1",
                        "method": "get",
                        "id": id_value,
                        "additional": self._json_list(LIST_ADDITIONAL),
                    }
                )
                data = payload.get("data") or {}
                if isinstance(data, dict):
                    return self._parse_get_item_data(data, item_id)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not get item {item_id}") from last_error

    def add_tags_to_items(self, item_ids: list[int], tag_ids: list[int]) -> None:
        if not item_ids or not tag_ids:
            return
        remaining = list(item_ids)
        while remaining:
            batch = remaining[:PAGE_SIZE]
            remaining = remaining[PAGE_SIZE:]
            self._post(
                {
                    "api": self._api.browse_item,
                    "version": "1",
                    "method": "add_tag",
                    "id": self._json_list(batch),
                    "tag": self._json_list(tag_ids),
                }
            )

    def add_tag_to_item(self, item_id: int, tag_id: int) -> None:
        self.add_tags_to_items([item_id], [tag_id])

    def remove_tags_from_items(self, item_ids: list[int], tag_ids: list[int]) -> None:
        if not item_ids or not tag_ids:
            return
        remaining = list(item_ids)
        while remaining:
            batch = remaining[:PAGE_SIZE]
            remaining = remaining[PAGE_SIZE:]
            self._post(
                {
                    "api": self._api.browse_item,
                    "version": "1",
                    "method": "remove_tag",
                    "id": self._json_list(batch),
                    "tag": self._json_list(tag_ids),
                }
            )

    def set_description(self, item_id: int, description: str) -> None:
        """Set item description (EXIF/ImageDescription metadata in Synology Photos)."""
        self._post(
            {
                "api": self._api.browse_item,
                "version": "1",
                "method": "set",
                "id": self._json_list([item_id]),
                "description": description,
            }
        )
