from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, ValidationError

from synology_photos_ai.config import Settings

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    {
        "with",
        "that",
        "this",
        "from",
        "they",
        "them",
        "have",
        "were",
        "been",
        "into",
        "your",
        "there",
        "their",
        "what",
        "when",
        "where",
        "which",
        "while",
        "about",
        "after",
        "before",
        "behind",
        "under",
        "over",
        "photo",
        "image",
        "picture",
    }
)

_JSON_EXAMPLE = (
    '{"description": "Three potted plants on a patio with a wooden fence.", '
    '"tags": ["plants", "pots", "patio", "fence", "outdoor"]}'
)

_DESCRIPTION_RULES = (
    "Description: one or two short sentences stating what is visible — subjects, setting, "
    "activity. Write as catalog text for search (e.g. 'Three potted plants on a patio'). "
    "Do not mention 'the image', 'this photo', 'scene', or 'captures'."
)

# Meta openers vision models often repeat; stripped after inference.
_DESCRIPTION_PREFIX_RES = (
    re.compile(
        r"^the image captures (?:a|an) [\w-]+ scene(?:[,:]|\s+(?:showing|featuring|with|of))?\s+",
        re.I,
    ),
    re.compile(
        r"^the image captures (?:a|an)?\s*",
        re.I,
    ),
    re.compile(
        r"^this (?:image|photo|picture) (?:shows|captures|depicts|features|displays)\s+(?:a|an)?\s*",
        re.I,
    ),
    re.compile(
        r"^the (?:image|photo|picture) (?:shows|depicts|features|displays|presents)\s+(?:a|an)?\s*",
        re.I,
    ),
    re.compile(r"^in this (?:image|photo|picture),?\s+", re.I),
)


class PhotoAnalysis(BaseModel):
    description: str = Field(
        description="One or two sentences describing the photo for search and accessibility."
    )
    tags: list[str] = Field(
        description="Short lowercase tags (subjects, setting, mood, activities, objects)."
    )


class PhotoAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.openai_model
        self._use_json_prompt = settings.openai_api_base is not None

        llm_kwargs: dict = {
            "model": settings.openai_model,
            "api_key": settings.openai_api_key,
            "temperature": 0.2,
            "max_tokens": settings.openai_max_tokens,
        }
        self._repair_llm: ChatOpenAI | None = None
        if settings.openai_api_base:
            llm_kwargs["base_url"] = settings.openai_api_base
            # num_predict caps output; format=json asks Ollama to emit JSON (not all models obey).
            llm_kwargs["extra_body"] = {
                "format": "json",
                "options": {
                    "num_predict": settings.openai_max_tokens,
                    "temperature": 0.2,
                },
            }
            self._llm = ChatOpenAI(**llm_kwargs)
            repair_predict = min(192, settings.openai_max_tokens)
            repair_kwargs = {
                **llm_kwargs,
                "extra_body": {
                    "format": "json",
                    "options": {
                        "num_predict": repair_predict,
                        "temperature": 0.1,
                    },
                },
            }
            self._repair_llm = ChatOpenAI(**repair_kwargs)
        else:
            self._llm = ChatOpenAI(**llm_kwargs).with_structured_output(PhotoAnalysis)

    def analyze(
        self,
        *,
        image_bytes: bytes,
        filename: str,
        mime_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg",
    ) -> PhotoAnalysis:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{encoded}"
        messages = self._build_messages(filename=filename, data_url=data_url)

        logger.info(
            "Vision inference (description + tags) for %s (%s)",
            filename,
            self._model,
        )
        started = time.monotonic()
        if self._use_json_prompt:
            result = self._invoke_ollama_json(messages, filename=filename)
        else:
            result = self._llm.invoke(messages)
        logger.info(
            "Vision inference (description + tags) for %s finished in %.1fs",
            filename,
            time.monotonic() - started,
        )
        result.description = self._normalize_description(result.description)
        result.tags = self._normalize_tags(result.tags)
        if not result.tags and result.description:
            derived = self._derive_tags_from_description(result.description)
            result.tags = self._normalize_tags(derived)
            if result.tags:
                logger.info(
                    "Derived %d tag(s) from description for %s (model/fallback had no tags)",
                    len(result.tags),
                    filename,
                )
        return result

    def _build_messages(self, *, filename: str, data_url: str) -> list:
        max_tags = self._settings.max_tags
        if self._use_json_prompt:
            system_text = (
                "Photo tagger. Reply with ONLY this JSON shape, no other text:\n"
                f"{_JSON_EXAMPLE}\n"
                f"{_DESCRIPTION_RULES}\n"
                f"Max {max_tags} tags. Lowercase, hyphenated."
            )
        else:
            system_text = (
                "You label personal photo libraries. Respond only with the requested structured "
                "fields — no preamble or extra text. "
                f"{_DESCRIPTION_RULES} "
                f"Tags: at most {max_tags} items, lowercase, hyphenated for multi-word phrases, "
                "suitable as Synology Photos general tags."
            )

        return [
            SystemMessage(content=system_text),
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": f"Describe this photo for a library index. Filename: {filename}.",
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]
            ),
        ]

    def _build_repair_messages(self, initial_messages: list) -> list:
        """Second attempt: same image, stricter JSON-only instruction (no prior ramble)."""
        human = initial_messages[-1]
        if not isinstance(human, HumanMessage):
            human = initial_messages[0]
        max_tags = self._settings.max_tags
        return [
            SystemMessage(
                content=(
                    "You label personal photo libraries. "
                    "Reply with ONLY one JSON object and nothing else:\n"
                    f"{_JSON_EXAMPLE}\n"
                    f"{_DESCRIPTION_RULES}\n"
                    f"At most {max_tags} tags. Lowercase, hyphenated."
                )
            ),
            HumanMessage(content=human.content),
        ]

    def _uses_plain_text_fast_path(self, snippet: str) -> bool:
        """Models that never emit JSON — one vision call, tags from description words."""
        if "{" in snippet or not self._looks_like_description(snippet):
            return False
        return "bakllava" in self._model.lower()

    def _try_vision_json_retry(
        self, messages: list, *, filename: str
    ) -> PhotoAnalysis | None:
        """Second vision call with a stricter JSON-only prompt (same thumbnail)."""
        repair_messages = self._build_repair_messages(messages)
        repair_llm = self._repair_llm or self._llm
        logger.info(
            "Second vision call (JSON retry) for %s (%s)",
            filename,
            self._model,
        )
        started = time.monotonic()
        retry = repair_llm.invoke(repair_messages)
        retry_content = retry.content if isinstance(retry.content, str) else str(retry.content)
        try:
            result = self._parse_json_response(retry_content)
        except ValueError:
            logger.warning(
                "Second vision call for %s still non-JSON after %.1fs (%d chars)",
                filename,
                time.monotonic() - started,
                len(retry_content),
            )
            return None
        logger.info(
            "Second vision call for %s finished in %.1fs (JSON ok)",
            filename,
            time.monotonic() - started,
        )
        return result

    def _invoke_ollama_json(self, messages: list, *, filename: str) -> PhotoAnalysis:
        response = self._llm.invoke(messages)
        content = response.content
        if not isinstance(content, str):
            content = str(content)
        try:
            return self._parse_json_response(content)
        except ValueError:
            snippet = content.strip()
            if self._uses_plain_text_fast_path(snippet):
                logger.info(
                    "Plain-text vision from %s for %s (%d chars); tags derived from description",
                    self._model,
                    filename,
                    len(snippet),
                )
                return self._from_plain_description(snippet)
            logger.warning(
                "Model %s returned non-JSON for %s (%d chars)",
                self._model,
                filename,
                len(content),
            )

        # JSON-capable models (e.g. llava-llama3): retry vision before cheap word-split fallbacks.
        retried = self._try_vision_json_retry(messages, filename=filename)
        if retried is not None:
            return retried

        ramble = self._try_ramble_fallback(content)
        if ramble is not None:
            logger.info(
                "Using first-paragraph fallback for %s after failed JSON retry "
                "(%d tag(s) from description words)",
                filename,
                len(ramble.tags),
            )
            return ramble
        if self._looks_like_description(content):
            logger.warning(
                "Using plain-text description from %s for %s (no tags from model)",
                self._model,
                filename,
            )
            return self._from_plain_description(content)
        raise ValueError(
            f"Model {self._model} did not return usable JSON or description for {filename}"
        )

    def _parse_json_response(self, text: str) -> PhotoAnalysis:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return PhotoAnalysis.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError):
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return PhotoAnalysis.model_validate_json(match.group())
        raise ValueError(f"Model did not return valid JSON: {text[:300]!r}")

    def _try_ramble_fallback(self, content: str) -> PhotoAnalysis | None:
        """Last resort when JSON and a vision retry both failed — use the first paragraph."""
        text = content.strip()
        if len(text) < 400 or "{" in text[:600]:
            return None
        snippet = text[:500].split("\n\n")[0].split("\n")[0].strip()
        if not self._looks_like_description(snippet):
            return None
        return self._from_plain_description(snippet)

    @staticmethod
    def _looks_like_description(text: str) -> bool:
        t = text.strip()
        if len(t) < 12 or len(t) > 500:
            return False
        lower = t.lower()
        if "correct answer" in lower or re.search(r"\banswer is [A-D]\b", t, re.I):
            return False
        if t.startswith("{") or t.startswith("["):
            return False
        return bool(re.search(r"[a-zA-Z]{3,}", t))

    def _derive_tags_from_description(self, text: str) -> list[str]:
        """Word-split tags for plain-text / first-paragraph fallbacks (not model JSON tags)."""
        words = re.findall(r"[a-z]{3,}", text.lower())
        tags = [w for w in words if w not in _STOPWORDS]
        seen: set[str] = set()
        unique: list[str] = []
        for w in tags:
            if w not in seen:
                seen.add(w)
                unique.append(w)
        return unique[: self._settings.max_tags]

    def _from_plain_description(self, text: str) -> PhotoAnalysis:
        description = text.strip()
        return PhotoAnalysis(
            description=description,
            tags=self._derive_tags_from_description(description),
        )

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        prefix = self._settings.tag_prefix.strip()
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in tags:
            tag = re.sub(r"\s+", "-", raw.strip().lower())
            tag = re.sub(r"[^a-z0-9\-]", "", tag)
            if not tag:
                continue
            if prefix and not tag.startswith(f"{prefix}-"):
                tag = f"{prefix}-{tag}"
            if tag in seen:
                continue
            seen.add(tag)
            cleaned.append(tag)
            if len(cleaned) >= self._settings.max_tags:
                break
        return cleaned

    @staticmethod
    def _normalize_description(text: str) -> str:
        desc = text.strip()
        if not desc:
            return desc
        changed = True
        while changed:
            changed = False
            for pattern in _DESCRIPTION_PREFIX_RES:
                stripped = pattern.sub("", desc, count=1).strip()
                if stripped != desc:
                    desc = stripped
                    changed = True
                    break
        if desc and desc[0].islower():
            desc = desc[0].upper() + desc[1:]
        return desc
