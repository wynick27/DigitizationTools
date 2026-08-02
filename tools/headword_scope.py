"""Scope, request-version and draft state for the entry comparison view."""

from __future__ import annotations

from dataclasses import dataclass

from tools.headword_matching import ExtractionResult, entry_body_segments


SCOPE_CURRENT_PAGE = "current_page"
SCOPE_GLOBAL = "global"


@dataclass(frozen=True)
class ScopeSelection:
    items: tuple
    reason: str = ""


class HeadwordScopeController:
    def __init__(self, project_config=None):
        self.project_config = project_config if project_config is not None else {}
        saved = self.project_config.get("headword_view_scope", SCOPE_CURRENT_PAGE)
        self.scope = saved if saved in (SCOPE_CURRENT_PAGE, SCOPE_GLOBAL) else SCOPE_CURRENT_PAGE
        self.page = None
        self.request_version = 0
        self.filters = {}
        self.selection_ids = ()
        self.drafts = {}

    def set_scope(self, scope):
        if scope not in (SCOPE_CURRENT_PAGE, SCOPE_GLOBAL):
            raise ValueError(scope)
        changed = self.scope != scope
        self.scope = scope
        self.project_config["headword_view_scope"] = scope
        if changed:
            self.request_version += 1
        return changed

    def set_page(self, page):
        page = int(page) if page is not None else None
        changed = self.page != page
        self.page = page
        if changed and self.scope == SCOPE_CURRENT_PAGE:
            self.request_version += 1
        return changed

    def new_request(self):
        self.request_version += 1
        return self.request_version

    def accepts(self, version):
        return int(version) == self.request_version

    @staticmethod
    def item_id(side, item):
        if item is None:
            return ""
        stable = item.metadata.get("stable_id") if item.metadata else None
        return f"{side}:{item.source_id}:{stable if stable is not None else item.order}"

    def put_draft(self, side, item, text):
        key = self.item_id(side, item)
        if key:
            self.drafts[key] = str(text)

    def get_draft(self, side, item, default=""):
        return self.drafts.get(self.item_id(side, item), default)

    def discard_draft(self, side, item):
        self.drafts.pop(self.item_id(side, item), None)

    def clear_project_state(self, project_config):
        self.project_config = project_config
        saved = project_config.get("headword_view_scope", SCOPE_CURRENT_PAGE)
        self.scope = saved if saved in (SCOPE_CURRENT_PAGE, SCOPE_GLOBAL) else SCOPE_CURRENT_PAGE
        self.page = None
        self.request_version += 1
        self.filters.clear()
        self.selection_ids = ()
        self.drafts.clear()

    @staticmethod
    def select_items(result: ExtractionResult, pages, page, anchor_keys=()):
        if page is None:
            return ScopeSelection((), "no current page")
        anchor_keys = {str(key) for key in anchor_keys if key}
        selected = []
        for item in result.items:
            if item.page is None:
                if item.key in anchor_keys or anchor_keys.intersection(item.aliases):
                    selected.append(item)
                continue
            if item.page == page:
                selected.append(item)
                continue
            if pages and any(segment_page == page for segment_page, _start, _end in entry_body_segments(result.items, item, pages)):
                selected.append(item)
        if selected:
            return ScopeSelection(tuple(selected))
        if all(item.page is None for item in result.items):
            return ScopeSelection((), "structured sources need OCR or paged headword anchors for current-page mode")
        return ScopeSelection((), "no headword belongs to the current page")