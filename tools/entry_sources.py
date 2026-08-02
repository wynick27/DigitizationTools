"""Compatibility exports for the structured data-source layer."""

from tools.structured_sources import (
    INDEX_THRESHOLD_BYTES,
    BaseStructuredSource,
    IndexedJsonEntrySource,
    IndexedMdxTextSource,
    JsonEntrySource,
    MdxEntrySource,
    StructuredEntry,
    load_structured_source,
    should_build_index,
)

__all__ = [
    "INDEX_THRESHOLD_BYTES",
    "BaseStructuredSource",
    "IndexedJsonEntrySource",
    "IndexedMdxTextSource",
    "JsonEntrySource",
    "MdxEntrySource",
    "StructuredEntry",
    "load_structured_source",
    "should_build_index",
]