from enum import Enum
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Space(str, Enum):
    PERSONAL = "personal"
    SHARED = "shared"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    synology_host: str = Field(validation_alias="SYNOLOGY_HOST")
    # Application portal alias (Login Portal → Applications). Empty = DSM /webapi on :5001.
    synology_photos_alias: str = Field(default="photo", validation_alias="SYNOLOGY_PHOTOS_ALIAS")
    synology_verify_ssl: bool = Field(default=False, validation_alias="SYNOLOGY_VERIFY_SSL")
    synology_username: str = Field(validation_alias="SYNOLOGY_USERNAME")
    synology_password: str = Field(validation_alias="SYNOLOGY_PASSWORD")
    synology_space: Space = Field(default=Space.PERSONAL, validation_alias="SYNOLOGY_SPACE")
    # Thumbnail from NAS: sm (~360px) | m (~720px) | xl (~1280px). Use sm with local Ollama.
    synology_thumbnail_size: str = Field(
        default="sm",
        validation_alias="SYNOLOGY_THUMBNAIL_SIZE",
    )
    # Longest edge after download (0 = no resize). 512 is enough for tagging on Ollama.
    vision_max_edge: int = Field(
        default=512,
        ge=0,
        le=2048,
        validation_alias="VISION_MAX_EDGE",
    )

    openai_api_key: str = Field(validation_alias="OPENAI_API_KEY")
    openai_api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_BASE", "OPENAI_BASE_URL"),
    )
    openai_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_MODEL")
    openai_max_tokens: int = Field(
        default=256,
        ge=64,
        le=8192,
        validation_alias="OPENAI_MAX_TOKENS",
    )

    tag_prefix: str = Field(default="ai", validation_alias="TAG_PREFIX")
    max_tags: int = Field(default=12, ge=1, le=30, validation_alias="MAX_TAGS")
    skip_if_tagged: bool = Field(default=True, validation_alias="SKIP_IF_TAGGED")
    write_description: bool = Field(default=True, validation_alias="WRITE_DESCRIPTION")

    watch_interval_seconds: int = Field(
        default=300, ge=30, validation_alias="WATCH_INTERVAL_SECONDS"
    )

    state_path: Path = Field(
        default=Path(".state/processed.db"),
        validation_alias="STATE_PATH",
    )

    @property
    def api_base_url(self) -> str:
        base = f"https://{self.synology_host}"
        alias = self.synology_photos_alias.strip()
        if not alias:
            return f"{base}/webapi/entry.cgi"
        return f"{base}/{alias.strip('/')}/webapi/entry.cgi"
