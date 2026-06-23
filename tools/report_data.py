import csv
import json
import re
from pathlib import Path


REPORT_FORMAT = "digitization-tools-report"
REPORT_VERSION = 1

TYPE_KEYS = ("issue_type", "classification", "status", "kind", "type", "问题类型")
PAGE_KEYS = ("page", "page_number", "page_no", "页码")
LINE_KEYS = ("line", "line_number", "line_no", "行号")
HEADWORD_KEYS = ("txt_head", "headword", "word", "entry", "词头", "json_head")
SIDE_KEYS = ("side", "text_side", "pane")
SEARCH_KEYS = (
    "search_text",
    "context",
    "line_context",
    "txt_line",
    "txt_head",
    "best_txt_candidate",
    "txt_preview",
    "headword",
    "json_example",
    "json_content",
    "old_txt_example",
    "new_txt_example",
    "old",
    "new",
)


def _first_value(row, keys, default=""):
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return value
    return default


def _as_int(value):
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def _clean_cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _make_summary(row):
    preferred = (
        "message",
        "detail",
        "missing_keywords",
        "json_example",
        "json_content",
        "json_minor_outline",
        "column_text",
        "old",
        "new",
    )
    parts = []
    for key in preferred:
        value = _clean_cell(row.get(key))
        if value:
            parts.append(f"{key}: {value}")
        if len(parts) == 2:
            break
    if not parts:
        ignored = set(TYPE_KEYS + PAGE_KEYS + LINE_KEYS + HEADWORD_KEYS + SIDE_KEYS)
        for key, raw_value in row.items():
            value = _clean_cell(raw_value)
            if key not in ignored and value:
                parts.append(f"{key}: {value}")
            if len(parts) == 2:
                break
    return " | ".join(parts)[:500]


def normalize_report_row(row, source_file="", source_row=0):
    original = dict(row or {})
    source_data = original.get("data") if isinstance(original.get("data"), dict) else original
    issue_type = _clean_cell(_first_value(original, TYPE_KEYS, "未分类"))
    side = _clean_cell(_first_value(original, SIDE_KEYS, "left")).lower()
    if side not in {"left", "right"}:
        side = "left"

    candidates = []
    existing_candidates = original.get("search_candidates")
    if isinstance(existing_candidates, list):
        candidates.extend(_clean_cell(value) for value in existing_candidates)
    for key in SEARCH_KEYS:
        value = _clean_cell(original.get(key) or source_data.get(key))
        if value and value not in candidates:
            candidates.append(value)

    headword = _clean_cell(_first_value(original, HEADWORD_KEYS))
    is_example_report = (
        _clean_cell(original.get("source_file")) == "example_match_report.tsv"
        or source_file == "example_match_report.tsv"
        or bool(source_data.get("json_example"))
    )
    if headword:
        if is_example_report:
            candidates = [value for value in candidates if value != headword]
            candidates.append(headword)
        elif headword not in candidates:
            candidates.insert(0, headword)

    return {
        "page": _as_int(_first_value(original, PAGE_KEYS)),
        "line": _as_int(_first_value(original, LINE_KEYS)),
        "issue_type": issue_type or "未分类",
        "headword": headword,
        "summary": _clean_cell(original.get("summary")) or _make_summary(original),
        "side": side,
        "search_candidates": candidates,
        "source_file": _clean_cell(original.get("source_file")) or source_file,
        "source_row": _as_int(original.get("source_row")) or source_row or 0,
        "data": source_data,
    }


def _read_delimited(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if Path(path).suffix.lower() == ".tsv" else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delimiter))


def _read_json(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("issues", "rows", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    raise ValueError("JSON report must contain an object or an array.")


def load_report_file(path):
    path = str(path)
    suffix = Path(path).suffix.lower()
    raw_rows = _read_json(path) if suffix in {".json", ".jsonl"} else _read_delimited(path)
    source_name = Path(path).name
    return [
        normalize_report_row(row, source_name, index)
        for index, row in enumerate(raw_rows, start=2 if suffix not in {".json", ".jsonl"} else 1)
        if isinstance(row, dict)
    ]


def load_report_paths(paths):
    rows = []
    for path in paths:
        rows.extend(load_report_file(path))
    return rows


def write_tool_report(path, rows, source_files=None):
    issues = []
    for row in rows:
        issue = dict(row)
        issues.append(issue)
    payload = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "source_files": sorted(set(source_files or [row.get("source_file", "") for row in rows]) - {""}),
        "issues": issues,
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def parse_page_ranges(text):
    text = (text or "").strip()
    if not text:
        return None
    pages = set()
    for part in re.split(r"[,，\s]+", text):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*[-－—]\s*(\d+))?", part)
        if not match:
            raise ValueError(part)
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            start, end = end, start
        pages.update(range(start, end + 1))
    return pages
