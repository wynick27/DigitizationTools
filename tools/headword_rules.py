"""Headword extraction and normalization rule models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re


SCOPE_AUTO = "auto"
SCOPE_PARAGRAPH = "paragraph"
SCOPE_LINE = "line"


@dataclass(frozen=True)
class ExtractionRule:
    pattern: str
    group: int | str = 0
    flags: int = 0
    scope: str = SCOPE_AUTO
    line_fallback: bool = True

    @classmethod
    def from_dict(cls, value, fallback_pattern="", fallback_group=0):
        value = value or {}
        group = value.get("group", fallback_group)
        if isinstance(group, str) and group.isdigit():
            group = int(group)
        scope = str(value.get("scope", SCOPE_AUTO))
        if scope not in (SCOPE_AUTO, SCOPE_PARAGRAPH, SCOPE_LINE):
            scope = SCOPE_AUTO
        return cls(
            pattern=str(value.get("pattern", fallback_pattern)),
            group=group,
            flags=int(value.get("flags", 0) or 0),
            scope=scope,
            line_fallback=bool(value.get("line_fallback", True)),
        )


@dataclass(frozen=True)
class NormalizationRule:
    pattern: str
    replacement: str = ""
    name: str = ""
    flags: int = 0
    enabled: bool = True
    mode: str = "regex"

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            return value
        value = value or {}
        mode = str(value.get("mode", "regex"))
        if mode not in ("regex", "literal"):
            mode = "regex"
        return cls(
            pattern=str(value.get("pattern", "")),
            replacement=str(value.get("replacement", "")),
            name=str(value.get("name", "")),
            flags=int(value.get("flags", 0) or 0),
            enabled=bool(value.get("enabled", True)),
            mode=mode,
        )

    def regex_pattern(self):
        return re.escape(self.pattern) if self.mode == "literal" else self.pattern


HeadwordFilterRule = NormalizationRule


@dataclass(frozen=True)
class HeadwordProfile:
    extract_pattern: str
    group: int | str = 0
    flags: int = 0
    ignore_patterns: tuple[str, ...] = ()
    filters: tuple[NormalizationRule, ...] = ()
    json_headword_field: str = ""
    json_body_field: str = ""
    json_alias_field: str = ""
    language: str = "custom"
    extraction_scope: str = SCOPE_AUTO
    line_fallback: bool = True

    @classmethod
    def from_dict(cls, value, fallback_pattern="", fallback_group=0):
        value = value or {}
        extraction = value.get("extraction") or {}
        group = extraction.get("group", value.get("group", fallback_group))
        if isinstance(group, str) and group.isdigit():
            group = int(group)
        scope = str(extraction.get("scope", value.get("extraction_scope", SCOPE_AUTO)))
        if scope not in (SCOPE_AUTO, SCOPE_PARAGRAPH, SCOPE_LINE):
            scope = SCOPE_AUTO
        return cls(
            extract_pattern=str(extraction.get("pattern", value.get("extract_pattern", fallback_pattern))),
            group=group,
            flags=int(extraction.get("flags", value.get("flags", 0)) or 0),
            ignore_patterns=tuple(str(pattern) for pattern in value.get("ignore_patterns", []) if str(pattern)),
            filters=tuple(
                NormalizationRule.from_dict(rule)
                for rule in value.get("normalization_rules", value.get("filters", []))
            ),
            json_headword_field=str(value.get("json_headword_field", "")),
            json_body_field=str(value.get("json_body_field", "")),
            json_alias_field=str(value.get("json_alias_field", "")),
            language=str(value.get("language", "custom")),
            extraction_scope=scope,
            line_fallback=bool(extraction.get("line_fallback", value.get("line_fallback", True))),
        )

    @property
    def extraction(self):
        return ExtractionRule(
            self.extract_pattern,
            self.group,
            self.flags,
            self.extraction_scope,
            self.line_fallback,
        )

    def to_dict(self):
        result = asdict(self)
        result["ignore_patterns"] = list(self.ignore_patterns)
        result["normalization_rules"] = [asdict(rule) for rule in self.filters]
        result.pop("filters", None)
        result["extraction"] = asdict(self.extraction)
        return result

    def fingerprint(self):
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_COMMON_NORMALIZATION = (
    NormalizationRule("(", "\uff08", "\u534a\u89d2\u5de6\u62ec\u53f7", mode="literal"),
    NormalizationRule(")", "\uff09", "\u534a\u89d2\u53f3\u62ec\u53f7", mode="literal"),
    NormalizationRule(r"[\^*\u2bc5\u2b25]", "", "\u5ffd\u7565\u7279\u6b8a\u7b26\u53f7"),
)


_PRESETS = {
    "japanese": HeadwordProfile(
        r"^\s*(?:[-#>*]\s*)?(?:\*\*)?([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u3005\u3006\u30f5\u30f6\uff5e\-]+(?:\s*[\u3010\[].+?[\u3011\]])?)(?:\*\*)?",
        1,
        filters=_COMMON_NORMALIZATION,
        language="japanese",
        line_fallback=False,
    ),
    "english": HeadwordProfile(
        r"^\s*(?:[-#>*]\s*)?(?:\*\*)?([A-Za-z]+(?:['\u2019\-][A-Za-z]+)*(?:\s+[A-Za-z]+(?:['\u2019\-][A-Za-z]+)*){0,7})(?:\*\*)?",
        1,
        filters=_COMMON_NORMALIZATION,
        language="english",
        line_fallback=False,
    ),
    "french": HeadwordProfile(
        r"^\s*(?:[-#>*]\s*)?(?:\*\*)?([A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0152\u0153\u0178]+(?:['\u2019\-][A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0152\u0153\u0178]+)*(?:\s+[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0152\u0153\u0178]+(?:['\u2019\-][A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0152\u0153\u0178]+)*){0,7})(?:\*\*)?",
        1,
        filters=_COMMON_NORMALIZATION,
        language="french",
        line_fallback=False,
    ),
    "custom": HeadwordProfile("", language="custom"),
}


def language_profile(language):
    return _PRESETS.get(str(language or "").lower(), _PRESETS["custom"])


def language_presets():
    return dict(_PRESETS)


def merge_profile_override(base, override):
    if not override:
        return base
    override = dict(override)
    payload = base.to_dict()
    extraction = dict(payload.get("extraction") or {})
    legacy_extraction_keys = {
        "extract_pattern": "pattern",
        "group": "group",
        "flags": "flags",
        "extraction_scope": "scope",
        "line_fallback": "line_fallback",
    }
    for old_key, new_key in legacy_extraction_keys.items():
        if old_key in override:
            extraction[new_key] = override[old_key]
    if isinstance(override.get("extraction"), dict):
        extraction.update(override["extraction"])
    for key, value in override.items():
        if key not in legacy_extraction_keys and key not in ("extraction", "inherit"):
            payload[key] = value
    payload["extraction"] = extraction
    return HeadwordProfile.from_dict(payload)

def resolve_side_profile(project_config, side):
    project_config = project_config or {}
    legacy_pattern = project_config.get(f"regex_{side}", "")
    legacy_group = project_config.get(f"regex_group_{side}", 0)
    default_value = project_config.get("headword_profile_default") or {}
    base = HeadwordProfile.from_dict(default_value, legacy_pattern, legacy_group)
    side_value = (project_config.get("headword_profiles") or {}).get(side)
    return merge_profile_override(base, side_value) if side_value else base


def _ocr_override(project_config, source_data):
    overrides = project_config.get("ocr_headword_profiles") or {}
    if not isinstance(source_data, dict):
        return {}
    engine = str(source_data.get("engine_id", "") or "")
    model = str(source_data.get("model", source_data.get("model_id", "")) or "")
    for key in (f"{engine}:{model}" if model else "", engine, "default"):
        value = overrides.get(key) if key else None
        if isinstance(value, dict):
            return value
    return {}


def resolve_ocr_profile(project_config, source_data, target_profile):
    """Use OCR extraction overrides while retaining target-side normalization."""
    project_default = HeadwordProfile.from_dict(
        (project_config or {}).get("headword_profile_default") or target_profile.to_dict()
    )
    extraction_profile = merge_profile_override(
        project_default, _ocr_override(project_config or {}, source_data)
    )
    return HeadwordProfile(
        extraction_profile.extract_pattern,
        extraction_profile.group,
        extraction_profile.flags,
        extraction_profile.ignore_patterns,
        target_profile.filters,
        target_profile.json_headword_field,
        target_profile.json_body_field,
        target_profile.json_alias_field,
        extraction_profile.language,
        extraction_profile.extraction_scope,
        extraction_profile.line_fallback,
    )