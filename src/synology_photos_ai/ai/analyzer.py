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
        "the",
        "and",
        "for",
        "are",
        "was",
        "not",
        "but",
        "you",
        "she",
        "her",
        "his",
        "its",
        "our",
        "who",
        "how",
        "why",
        "can",
        "may",
        "all",
        "any",
        "out",
        "off",
        "one",
        "two",
        "three",
        "also",
        "very",
        "just",
        "only",
        "some",
        "such",
        "than",
        "then",
        "captures",
        "capture",
        "scene",
        "serene",
        "majestic",
        "individual",
        "moment",
        "moments",
        "tranquility",
        "tranquil",
        "peaceful",
        "peace",
        "picturesque",
        "serenity",
        "calm",
        "quiet",
        "has",
        "had",
        "was",
        "were",
        "are",
        "is",
        "been",
        "being",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "showing",
        "shows",
        "depicts",
        "featuring",
        "located",
        "appears",
        "seems",
        "likely",
        "possibly",
        "probably",
        "maybe",
    }
)

# Tags must not be auxiliaries, mood words, or meta labels (applied after ai- prefix too).
_INVALID_TAG_STEMS = frozenset(
    {
        "has",
        "had",
        "have",
        "was",
        "were",
        "are",
        "is",
        "the",
        "and",
        "for",
        "not",
        "but",
        "you",
        "she",
        "her",
        "his",
        "its",
        "our",
        "who",
        "how",
        "why",
        "can",
        "may",
        "all",
        "any",
        "out",
        "off",
        "one",
        "two",
        "also",
        "very",
        "just",
        "only",
        "some",
        "such",
        "than",
        "then",
        "serene",
        "serenity",
        "tranquil",
        "tranquility",
        "peaceful",
        "picturesque",
        "majestic",
        "scene",
        "captures",
        "capture",
        "moment",
        "individual",
        "showing",
        "shows",
        "located",
        "appears",
        "seems",
        "memorial-museum",
        "holocaust-memorial-museum",
        "holocaust-museum",
    }
)

# Generic venue labels models invent instead of describing what is visible.
_INVENTED_VENUE_RES = (
    re.compile(r"\bHolocaust Memorial Museum\b", re.I),
    re.compile(r"\bHolocaust Museum\b", re.I),
    re.compile(r"\bMemorial Museum of\b", re.I),
)

_JSON_EXAMPLE = (
    '{"description": "Iron gate with metal lettering on the lintel between brick gateposts at a '
    'former camp site.", '
    '"tags": ["gate", "inscription", "brick", "gateposts", "historical-site"]}'
)

_DESCRIPTION_RULES = (
    "Description: one or two short factual sentences — subjects, structures, activity, setting. "
    "Write catalog text for search, not poetry or travel copy. "
    "Do NOT use: serene, tranquil, tranquility, peaceful, moment, picturesque, majestic, "
    "captures, scene, or similar mood filler. "
    "Do NOT guess city, country, or landmark names unless clearly readable in the image or "
    "unmistakable; if unsure, describe visible features only — never invent a location. "
    "Visible text/signs: describe or briefly quote lettering you can read; do NOT translate "
    "inscriptions into a different English venue or museum name. "
    "Former camps and Holocaust sites: describe gates, fences, buildings, and any inscription "
    "factually; do NOT rename them (e.g. never call a camp entrance a 'Holocaust Memorial Museum'). "
    "Memorials and somber sites: neutral, factual wording only (no idyllic or leisure tone)."
)

_TAG_RULES = (
    "Tags: concrete nouns only (objects, structures, activities, place types). "
    "Lowercase, hyphenated. No verbs or auxiliaries (has, was, is, are). "
    "No mood adjectives (serene, tranquil, peaceful). No invented place or museum names."
)

_LOCATION_METADATA_RULES = (
    "When camera location metadata is supplied in the user message, treat it as authoritative: "
    "you may use those city, country, and landmark names in the description and tags; "
    "never invent a different city or country. "
    "If no location metadata is supplied, do NOT guess city, country, or landmark names "
    "unless clearly readable in the image or unmistakable from the scene alone."
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
    re.compile(
        r"^(?:a|an)\s+moment\s+of\s+(?:tranquility|peace|serenity|calm)\b[,:]?\s*",
        re.I,
    ),
    re.compile(
        r"^(?:this\s+)?(?:is\s+)?(?:a|an)\s+(?:serene|tranquil|peaceful|picturesque)\s+"
        r"(?:scene|view|moment|setting)\b[,:]?\s*",
        re.I,
    ),
)

# Flowery phrases removed anywhere in the description (not only at the start).
_DESCRIPTION_PHRASE_RES = (
    re.compile(
        r"\b(?:a|an)\s+moment\s+of\s+(?:tranquility|peace|serenity|calm)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:serene|tranquil|peaceful|picturesque|majestic)\s+(?:scene|view|moment|setting)\b",
        re.I,
    ),
    re.compile(r"\b(?:the|this)\s+scene\s+(?:shows|depicts|features|captures)\b", re.I),
)


class PhotoAnalysis(BaseModel):
    description: str = Field(
        description="One or two sentences describing the photo for search and accessibility."
    )
    tags: list[str] = Field(
        description="Short lowercase noun tags (objects, structures, activities — not mood or auxiliaries)."
    )


class PhotoAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.openai_model
        self._use_json_prompt = settings.openai_api_base is not None
        temperature = settings.openai_temperature
        repair_temperature = min(temperature, 0.05) if temperature > 0 else 0.0

        llm_kwargs: dict = {
            "model": settings.openai_model,
            "api_key": settings.openai_api_key,
            "temperature": temperature,
            "max_tokens": settings.openai_max_tokens,
        }
        if settings.openai_seed is not None:
            llm_kwargs["seed"] = settings.openai_seed
        self._repair_llm: ChatOpenAI | None = None
        if settings.openai_api_base:
            llm_kwargs["base_url"] = settings.openai_api_base
            options: dict = {
                "num_predict": settings.openai_max_tokens,
                "temperature": temperature,
            }
            if settings.openai_seed is not None:
                options["seed"] = settings.openai_seed
            llm_kwargs["extra_body"] = {
                "format": "json",
                "options": options,
            }
            self._llm = ChatOpenAI(**llm_kwargs)
            repair_predict = min(192, settings.openai_max_tokens)
            repair_options: dict = {
                "num_predict": repair_predict,
                "temperature": repair_temperature,
            }
            if settings.openai_seed is not None:
                repair_options["seed"] = settings.openai_seed
            repair_kwargs = {
                **llm_kwargs,
                "temperature": repair_temperature,
                "extra_body": {
                    "format": "json",
                    "options": repair_options,
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
        location_context: str | None = None,
    ) -> PhotoAnalysis:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{encoded}"
        messages = self._build_messages(
            filename=filename,
            data_url=data_url,
            location_context=location_context,
        )

        logger.info(
            "Vision inference (description + tags) for %s (%s)",
            filename,
            self._model,
        )
        if location_context:
            logger.info("EXIF location for %s: %s", filename, location_context)
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

    def _build_messages(
        self,
        *,
        filename: str,
        data_url: str,
        location_context: str | None = None,
    ) -> list:
        max_tags = self._settings.max_tags
        location_rules = (
            f"{_LOCATION_METADATA_RULES}\n" if self._settings.use_location_in_prompt else ""
        )
        if self._use_json_prompt:
            system_text = (
                "Photo tagger. Reply with ONLY this JSON shape, no other text:\n"
                f"{_JSON_EXAMPLE}\n"
                f"{_DESCRIPTION_RULES}\n"
                f"{_TAG_RULES}\n"
                f"{location_rules}"
                f"Max {max_tags} tags."
            )
        else:
            system_text = (
                "You label personal photo libraries. Respond only with the requested structured "
                "fields — no preamble or extra text. "
                f"{_DESCRIPTION_RULES} "
                f"{_TAG_RULES} "
                f"{location_rules}"
                f"At most {max_tags} tags."
            )

        return [
            SystemMessage(content=system_text),
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": self._build_human_prompt_text(
                            filename=filename,
                            location_context=location_context,
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]
            ),
        ]

    @staticmethod
    def _build_human_prompt_text(
        *,
        filename: str,
        location_context: str | None,
    ) -> str:
        parts = [
            f"Describe this photo for a library index. Filename: {filename}.",
        ]
        if location_context:
            parts.append(
                "Camera location from photo metadata (authoritative — do not contradict): "
                f"{location_context}."
            )
        else:
            parts.append(
                "State a city, country, or landmark only if clearly visible or "
                "you are highly confident; otherwise describe what you see without "
                "guessing the place."
            )
        parts.append(
            "If there is lettering on a gate, sign, or building, describe the "
            "text or quote short readable fragments — do not replace them with a "
            "made-up museum or venue name."
        )
        return " ".join(parts)

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
                    f"{_TAG_RULES}\n"
                    f"At most {max_tags} tags."
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

        short = self._try_short_text_fallback(content)
        if short is not None:
            logger.info(
                "Using short-text fallback for %s (%d tag(s) from description words)",
                filename,
                len(short.tags),
            )
            return short

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
            derived = self._from_plain_description(content)
            logger.warning(
                "Using plain-text description from %s for %s (%d tag(s) from description words)",
                self._model,
                filename,
                len(derived.tags),
            )
            return derived
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

    def _first_line_snippet(self, content: str) -> str | None:
        text = content.strip()
        if "{" in text[:400]:
            return None
        snippet = text[:500].split("\n\n")[0].split("\n")[0].strip()
        if not self._looks_like_description(snippet):
            return None
        return snippet

    def _try_short_text_fallback(self, content: str) -> PhotoAnalysis | None:
        """Non-JSON replies under 400 chars (too short for ramble fallback)."""
        text = content.strip()
        if len(text) < 120 or len(text) >= 400:
            return None
        snippet = self._first_line_snippet(content)
        if not snippet:
            return None
        return self._from_plain_description(snippet)

    def _try_ramble_fallback(self, content: str) -> PhotoAnalysis | None:
        """Last resort when JSON and a vision retry both failed — use the first paragraph."""
        text = content.strip()
        if len(text) < 400 or "{" in text[:600]:
            return None
        snippet = self._first_line_snippet(content)
        if not snippet:
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

    def _is_valid_tag_stem(self, stem: str) -> bool:
        if len(stem) < 3:
            return False
        if stem in _INVALID_TAG_STEMS:
            return False
        # Reject single auxiliary fragments from hyphen splits
        for part in stem.split("-"):
            if part in _INVALID_TAG_STEMS:
                return False
        return True

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
            stem = tag[len(prefix) + 1 :] if prefix and tag.startswith(f"{prefix}-") else tag
            if not self._is_valid_tag_stem(stem):
                continue
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
        for pattern in _DESCRIPTION_PHRASE_RES:
            desc = pattern.sub("", desc)
        for pattern in _INVENTED_VENUE_RES:
            if pattern.search(desc):
                logger.info("Replacing invented venue label in description: %r", pattern.pattern)
                desc = pattern.sub("historical memorial site", desc)
        desc = re.sub(r"\s{2,}", " ", desc).strip(" ,.;")
        # Clean up broken sentence starts after phrase removal
        desc = re.sub(r"^[,:;\s]+", "", desc)
        if desc and desc[0].islower():
            desc = desc[0].upper() + desc[1:]
        return desc
