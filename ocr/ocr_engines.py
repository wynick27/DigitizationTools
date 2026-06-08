import json
import os
import re
import time
import base64
import hashlib
import io
import uuid
import zipfile
from dataclasses import dataclass

import requests


PADDLE_ENGINE_ID = "paddleocr"
LEGACY_PADDLE_ENGINE_ID = "remote"


@dataclass(frozen=True)
class OCREngineDef:
    engine_id: str
    label: str
    suffix: str


ENGINE_DEFS = {
    PADDLE_ENGINE_ID: OCREngineDef(PADDLE_ENGINE_ID, "PaddleOCR", "PaddleOCR"),
    "textin": OCREngineDef("textin", "Textin", "textin"),
    "mineru": OCREngineDef("mineru", "MinerU", "mineru"),
    "quark": OCREngineDef("quark", "Quark", "quark"),
    "chrome_lens": OCREngineDef("chrome_lens", "Chrome Lens", "chrome-lens"),
    "local": OCREngineDef("local", "PaddleOCR (Local)", "PaddleOCR-Local"),
}

TEXT_LABELS = {
    "text", "paragraph_title", "vertical_text", "title", "narrativetext", "listitem",
    "header", "footer", "pagenumber", "uncategorizedtext", "tables", "table",
    "formula", "code", "codesnippet",
}


def canonical_engine_id(engine_id: str) -> str:
    if engine_id == LEGACY_PADDLE_ENGINE_ID:
        return PADDLE_ENGINE_ID
    return engine_id or PADDLE_ENGINE_ID


def get_engine_def(engine_id: str) -> OCREngineDef:
    return ENGINE_DEFS.get(canonical_engine_id(engine_id), ENGINE_DEFS[PADDLE_ENGINE_ID])


def sanitize_suffix(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "ocr"


def paddle_suffix(model: str) -> str:
    return sanitize_suffix(model or "PaddleOCR")


def engine_suffix(engine_id: str, global_config: dict) -> str:
    engine_id = canonical_engine_id(engine_id)
    if engine_id == PADDLE_ENGINE_ID:
        return paddle_suffix(global_config.get("ocr_api_model", "PaddleOCR"))
    return get_engine_def(engine_id).suffix


def get_result_path(save_dir: str, real_page_num: int, engine_id: str, global_config: dict) -> str:
    suffix = engine_suffix(engine_id, global_config)
    return os.path.join(save_dir, f"page_{real_page_num}_{suffix}.json")


def get_legacy_result_paths(save_dir: str, real_page_num: int) -> list[str]:
    return [
        os.path.join(save_dir, f"page_{real_page_num}.json"),
        os.path.join(save_dir, f"{real_page_num}.json"),
    ]


def discover_ocr_results(save_dir: str, real_page_num: int) -> list[dict]:
    if not save_dir or not os.path.isdir(save_dir):
        return []

    results = []
    seen = set()
    for path in get_legacy_result_paths(save_dir, real_page_num):
        if os.path.exists(path):
            results.append({
                "label": "PaddleOCR",
                "engine_id": PADDLE_ENGINE_ID,
                "path": path,
                "legacy": True,
            })
            seen.add(os.path.normcase(os.path.abspath(path)))
            break

    patterns = [
        re.compile(rf"^page_{re.escape(str(real_page_num))}_(.+)\.json$", re.I),
        re.compile(rf"^{re.escape(str(real_page_num))}_(.+)\.json$", re.I),
    ]
    for filename in sorted(os.listdir(save_dir)):
        full_path = os.path.join(save_dir, filename)
        if not os.path.isfile(full_path):
            continue
        norm = os.path.normcase(os.path.abspath(full_path))
        if norm in seen:
            continue
        suffix = None
        for pattern in patterns:
            match = pattern.match(filename)
            if match:
                suffix = match.group(1)
                break
        if not suffix:
            continue
        engine_id = engine_id_from_suffix(suffix)
        results.append({
            "label": result_label_from_suffix(suffix),
            "engine_id": engine_id,
            "path": full_path,
            "legacy": False,
        })
        seen.add(norm)

    return results


def engine_id_from_suffix(suffix: str) -> str:
    low = suffix.lower()
    if "textin" in low:
        return "textin"
    if "mineru" in low:
        return "mineru"
    if "quark" in low:
        return "quark"
    if "chrome" in low or "lens" in low:
        return "chrome_lens"
    if "paddle" in low:
        return PADDLE_ENGINE_ID
    return low


def result_label_from_suffix(suffix: str) -> str:
    engine_id = engine_id_from_suffix(suffix)
    if engine_id in ENGINE_DEFS:
        return ENGINE_DEFS[engine_id].label
    return suffix


def excluded_labels(global_config: dict) -> set[str]:
    raw = global_config.get("ocr_excluded_labels", "image,table,formula")
    return {x.strip().lower() for x in re.split(r"[,，\s]+", raw or "") if x.strip()}


def should_keep_label(label: str, global_config: dict) -> bool:
    label = (label or "text").lower()
    excluded = excluded_labels(global_config)
    return label not in excluded


def points_to_bbox(points, page_size=None):
    if not points:
        return []
    if len(points) == 4 and all(isinstance(x, (int, float)) for x in points):
        return list(points)
    nums = []
    if len(points) == 8:
        nums = list(points)
    elif all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in points):
        nums = [v for p in points for v in p[:2]]
    if len(nums) < 8:
        return []
    xs = nums[0::2]
    ys = nums[1::2]
    if page_size and max(xs + ys) <= 1.5:
        width, height = page_size
        xs = [x * width for x in xs]
        ys = [y * height for y in ys]
    return [min(xs), min(ys), max(xs), max(ys)]


def normalize_ocr_result(raw, engine_id: str, global_config: dict | None = None) -> list[dict]:
    global_config = global_config or {}
    engine_id = canonical_engine_id(engine_id)

    if isinstance(raw, dict) and isinstance(raw.get("__normalized_blocks"), list):
        if engine_id == "mineru":
            for block in raw["__normalized_blocks"]:
                if isinstance(block, dict) and block.get("bbox"):
                    block.setdefault("bbox_coordinate_type", "mineru_page_1000")
        if engine_id == "textin":
            return raw["__normalized_blocks"]
        return raw["__normalized_blocks"]

    if isinstance(raw, list):
        return normalize_paddle(raw, global_config)

    if not isinstance(raw, dict):
        return []

    if engine_id == "textin":
        return normalize_textin(raw, global_config)
    if engine_id == "mineru":
        return normalize_mineru(raw, global_config)
    if engine_id == "quark":
        return normalize_quark(raw, global_config)
    if engine_id == "chrome_lens":
        return normalize_chrome_lens(raw, global_config)
    return normalize_paddle(raw, global_config)


def normalize_paddle(raw, global_config):
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, list) and len(item) == 2:
                pts = item[0]
                text = item[1][0] if item[1] else ""
                out.append({"text": text, "bbox": points_to_bbox(pts), "block_label": "text"})
        return out

    data = raw
    if "fullContent" in data:
        data = data["fullContent"]
    if "layoutParsingResults" in data:
        data = data.get("layoutParsingResults", [{}])[0]
    blocks = data.get("prunedResult", {}).get("parsing_res_list", [])
    for block in blocks:
        label = block.get("block_label") or "text"
        text = block.get("block_content") or block.get("text") or ""
        if text and should_keep_label(label, global_config):
            out.append({"block_label": label, "text": text, "bbox": block.get("block_bbox", [])})
    return out


def normalize_textin(raw, global_config):
    out = []
    data = raw.get("data", raw)
    pages = data.get("pages") or raw.get("pages") or []
    page_size = None
    if pages:
        p0 = pages[0]
        page_size = (p0.get("width") or p0.get("page_width") or 1, p0.get("height") or p0.get("page_height") or 1)
    elements = data.get("elements") or data.get("result", {}).get("elements") or raw.get("elements") or []
    id_to_element = {element.get("element_id"): element for element in elements if isinstance(element, dict)}
    ordered_elements = []
    seen_ids = set()
    for page in pages:
        for element_id in page.get("element_ids") or []:
            element = id_to_element.get(element_id)
            if element is not None:
                ordered_elements.append(element)
                seen_ids.add(element_id)
    if ordered_elements:
        ordered_elements.extend(
            element for element in elements
            if not element.get("element_id") or element.get("element_id") not in seen_ids
        )
        elements = ordered_elements
    for element in elements:
        label = element.get("type") or element.get("category") or "text"
        text = element.get("text") or element.get("content") or ""
        if text and should_keep_label(label, global_config):
            bbox = points_to_bbox(element.get("coordinates") or element.get("position") or element.get("bbox"), page_size)
            sub_items = []
            for char in element.get("char_details") or element.get("chars") or []:
                char_text = char.get("char") or char.get("text") or ""
                char_bbox = points_to_bbox(char.get("coordinates") or char.get("position") or char.get("bbox"), page_size)
                idx = char.get("index")
                if idx is None:
                    idx = text.find(char_text) if char_text else -1
                if idx is None or idx < 0:
                    idx = len(sub_items)
                if char_text and char_bbox:
                    sub_items.append({
                        "level": "char",
                        "text": char_text,
                        "start": int(idx),
                        "end": int(idx) + len(char_text),
                        "bbox": char_bbox,
                    })
            block = {"block_label": label, "text": text, "bbox": bbox}
            if sub_items:
                block["sub_items"] = sub_items
                block["granularity"] = "char"
            out.append(block)
    return out


def normalize_mineru(raw, global_config):
    out = []
    content_list = raw.get("content_list") or raw.get("data", {}).get("content_list") or []
    for item in content_list:
        label = item.get("type") or item.get("category") or "text"
        text = item.get("text") or item.get("content") or ""
        if text and should_keep_label(label, global_config):
            out.append({
                "block_label": label,
                "text": text,
                "bbox": item.get("bbox", []),
                "bbox_coordinate_type": "mineru_page_1000",
            })
    if not out:
        md = raw.get("markdown") or raw.get("data", {}).get("markdown") or ""
        if md and should_keep_label("text", global_config):
            out.append({"block_label": "text", "text": md, "bbox": []})
    return out


def normalize_generic(raw, global_config, default_label="text"):
    out = []

    def walk(obj):
        if isinstance(obj, dict):
            text = obj.get("text") or obj.get("content") or obj.get("words")
            label = obj.get("type") or obj.get("label") or obj.get("category") or default_label
            if isinstance(text, str) and text.strip() and should_keep_label(label, global_config):
                out.append({"block_label": label, "text": text, "bbox": obj.get("bbox") or obj.get("box") or obj.get("position") or []})
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(raw)
    return out


def normalize_quark(raw, global_config):
    out = []
    for page in raw.get("OcrInfo", []) or raw.get("data", {}).get("OcrInfo", []):
        details = page.get("Detail") or []
        if details:
            for item in details:
                label = item.get("Type") or "text"
                text = item.get("Value") or ""
                if text and should_keep_label(label, global_config):
                    out.append({
                        "block_label": label,
                        "text": text,
                        "bbox": points_to_bbox(item.get("Position")),
                    })
        else:
            text = page.get("Text") or ""
            if text and should_keep_label("text", global_config):
                out.append({"block_label": "text", "text": text, "bbox": []})
    if out:
        return out
    return normalize_generic(raw, global_config, "quark")


def geometry_to_bbox(geometry, page_size=None):
    if not isinstance(geometry, dict):
        return []
    cx = geometry.get("center_x")
    cy = geometry.get("center_y")
    width = geometry.get("width")
    height = geometry.get("height")
    if None in (cx, cy, width, height):
        return []
    x1 = cx - width / 2
    y1 = cy - height / 2
    x2 = cx + width / 2
    y2 = cy + height / 2
    if geometry.get("coordinate_type") == "NORMALIZED" or max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.5:
        if page_size:
            img_w, img_h = page_size
            return [x1 * img_w, y1 * img_h, x2 * img_w, y2 * img_h]
        return [x1, y1, x2, y2]
    return [x1, y1, x2, y2]


def normalize_chrome_lens(raw, global_config):
    detailed_blocks = raw.get("detailed_blocks") or []
    if detailed_blocks:
        out = []
        for block in detailed_blocks:
            block_text = block.get("text") or ""
            block_bbox = geometry_to_bbox(block.get("geometry"))
            sub_items = []
            cursor = 0
            text_parts = []
            for line in block.get("lines") or []:
                line_text = line.get("text") or ""
                line_start = cursor
                if line_text:
                    text_parts.append(line_text)
                    cursor += len(line_text)
                    line_end = cursor
                    sub_items.append({
                        "level": "line",
                        "text": line_text,
                        "start": line_start,
                        "end": line_end,
                        "bbox": geometry_to_bbox(line.get("geometry")),
                    })

                    search_from = line_start
                    for word in line.get("words") or []:
                        word_text = word.get("text") or word.get("word") or ""
                        if not word_text:
                            continue
                        word_start = block_text.find(word_text, search_from)
                        if word_start < 0:
                            word_start = line_text.find(word_text)
                            if word_start >= 0:
                                word_start += line_start
                        if word_start < 0:
                            word_start = search_from
                        word_end = word_start + len(word_text)
                        sub_items.append({
                            "level": "char" if len(word_text) == 1 else "word",
                            "text": word_text,
                            "start": word_start,
                            "end": word_end,
                            "bbox": geometry_to_bbox(word.get("geometry")),
                        })
                        search_from = word_end
                if line != (block.get("lines") or [])[-1]:
                    text_parts.append("\n")
                    cursor += 1
            text = "".join(text_parts).strip() or block_text
            if text and should_keep_label("text", global_config):
                out.append({
                    "block_label": "text",
                    "text": text,
                    "bbox": block_bbox,
                    "sub_items": sub_items,
                    "granularity": "line_word",
                    "bbox_coordinate_type": "normalized",
                })
        return out

    words = raw.get("word_data") or []
    if not words:
        return normalize_generic(raw, global_config, "text")

    text_parts = []
    sub_items = []
    cursor = 0
    for item in words:
        word = item.get("word") or ""
        sep = item.get("separator") or ""
        if not word and not sep:
            continue
        start = cursor
        text_parts.append(word)
        cursor += len(word)
        end = cursor
        bbox = geometry_to_bbox(item.get("geometry"))
        if word:
            sub_items.append({
                "level": "char" if len(word) == 1 else "word",
                "text": word,
                "start": start,
                "end": end,
                "bbox": bbox,
            })
        if sep:
            text_parts.append(sep)
            cursor += len(sep)

    full_text = "".join(text_parts).rstrip()
    bboxes = [sub["bbox"] for sub in sub_items if len(sub.get("bbox", [])) == 4]
    bbox = [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ] if bboxes else []
    return [{
        "block_label": "text",
        "text": full_text,
        "bbox": bbox,
        "sub_items": sub_items,
        "granularity": "char",
        "bbox_coordinate_type": "normalized",
    }]


def run_textin(img_bytes: bytes, config: dict) -> dict:
    app_id = config.get("app_id", "")
    secret_code = config.get("secret_code", "")
    if not app_id or not secret_code:
        raise Exception("Textin app_id/secret_code is not configured.")
    capabilities = {
        "include_hierarchy": bool(config.get("include_hierarchy", True)),
        "include_inline_objects": bool(config.get("include_inline_objects", False)),
        "include_table_structure": bool(config.get("include_table_structure", True)),
        "include_char_details": bool(config.get("include_char_details", False)),
        "include_image_data": bool(config.get("include_image_data", False)),
        "pages": bool(config.get("pages", True)),
    }
    parse_config = {
        "capabilities": capabilities,
        "config": {
            "table": {
                "table_view": config.get("table_view", "html"),
            },
            "engine_params": {
                "crop_dewarp": bool(config.get("crop_dewarp", False)),
                "formula_level": int(config.get("formula_level", 0)),
                "recognize_chemical": bool(config.get("recognize_chemical", False)),
            },
        },
    }
    password = str(config.get("password", "")).strip()
    if password:
        parse_config.setdefault("document", {})["password"] = password
    page_range = str(config.get("page_range", "")).strip()
    if page_range:
        parse_config["scope"] = {"pages": page_range}
    if config.get("title_tree"):
        parse_config["capabilities"]["title_tree"] = True
    if config.get("remove_watermark"):
        parse_config["config"]["remove_watermark"] = True
    if config.get("force_engine"):
        parse_config["config"]["force_engine"] = config.get("force_engine")
    if config.get("parse_mode") and config.get("parse_mode") != "auto":
        parse_config["config"]["parse_mode"] = config.get("parse_mode")
    if config.get("include_image_data"):
        parse_config["config"]["image"] = {
            "image_output_type": config.get("image_output_type", "url")
        }
    headers = {"x-ti-app-id": app_id, "x-ti-secret-code": secret_code}
    files = {"file": ("page.jpg", img_bytes)}
    data = {"config": json.dumps(parse_config, ensure_ascii=False)}
    resp = requests.post(
        config.get("endpoint", "https://api.textin.com/api/v1/xparse/parse/sync"),
        headers=headers,
        files=files,
        data=data,
        timeout=int(config.get("timeout", 120)),
    )
    resp.raise_for_status()
    return resp.json()


def _compact_error_payload(payload, limit=800):
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(payload)
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._-]+", r"\1***", text)
    return text if len(text) <= limit else text[:limit] + "..."


def run_mineru_agent(img_bytes: bytes, config: dict, filename="page.jpg", progress=None) -> dict:
    token = config.get("token", "")
    if not token:
        raise Exception("MinerU token is not configured.")
    if config.get("api_mode", "v4") == "agent":
        return run_mineru_agent_legacy(img_bytes, config, filename)

    create_url = config.get("endpoint", "https://mineru.net/api/v4/file-urls/batch")
    poll_template = config.get("poll_endpoint", "https://mineru.net/api/v4/extract-results/batch/{batch_id}")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    file_item = {"name": filename, "data_id": config.get("data_id", filename)}
    if "is_ocr" in config:
        file_item["is_ocr"] = bool(config.get("is_ocr", True))
    if config.get("page_ranges"):
        file_item["page_ranges"] = str(config["page_ranges"])

    payload = {
        "files": [file_item],
        "model_version": config.get("model_version", "vlm"),
        "enable_table": bool(config.get("enable_table", True)),
        "enable_formula": bool(config.get("enable_formula", True)),
        "language": config.get("language", "ch"),
    }
    if config.get("extra_formats"):
        payload["extra_formats"] = [x.strip() for x in str(config["extra_formats"]).split(",") if x.strip()]
    if "no_cache" in config:
        payload["no_cache"] = bool(config.get("no_cache", False))
    if config.get("cache_tolerance"):
        payload["cache_tolerance"] = int(config.get("cache_tolerance", 900))

    if progress:
        progress(f"MinerU: requesting upload URL for {filename}...")
    resp = requests.post(create_url, headers=headers, json=payload, timeout=int(config.get("timeout", 60)))
    if resp.status_code != 200:
        raise Exception(f"MinerU upload-url HTTP {resp.status_code}: {resp.text[:800]}")
    result = resp.json()
    if result.get("code") not in (0, None):
        raise Exception(f"MinerU upload-url request failed: {_compact_error_payload(result)}")
    data = result.get("data", {})
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    if not batch_id or not urls:
        raise Exception(f"MinerU did not return upload URL: {_compact_error_payload(result)}")

    if progress:
        progress(f"MinerU: batch {batch_id} created, uploading {filename}...")
    put_resp = requests.put(
        urls[0],
        data=img_bytes,
        timeout=int(config.get("timeout", 120)),
    )
    if put_resp.status_code not in (200, 201, 204):
        raise Exception(f"MinerU upload HTTP {put_resp.status_code}: {put_resp.text[:800]}")

    poll_url = poll_template.format(batch_id=batch_id)
    last_state = ""
    last_polled = None
    processing_states = {"waiting-file", "pending", "running", "converting"}
    for attempt in range(int(config.get("poll_count", 60))):
        time.sleep(float(config.get("poll_interval", 2)))
        poll = requests.get(poll_url, headers=headers, timeout=int(config.get("timeout", 60)))
        if poll.status_code != 200:
            raise Exception(f"MinerU poll HTTP {poll.status_code} for batch {batch_id}: {poll.text[:800]}")
        polled = poll.json()
        last_polled = polled
        if polled.get("code") not in (0, None):
            raise Exception(f"MinerU poll failed for batch {batch_id}: {_compact_error_payload(polled)}")
        pdata = polled.get("data", {}) if isinstance(polled, dict) else {}
        extract_results = pdata.get("extract_result") or pdata.get("extract_results") or []
        if not extract_results:
            if progress and attempt == 0:
                progress(f"MinerU: batch {batch_id} waiting for result...")
            continue
        item = extract_results[0]
        state = item.get("state") or item.get("status") or ""
        if progress and state != last_state:
            progress(f"MinerU: batch {batch_id} state={state or 'unknown'}")
            last_state = state
        if state == "done":
            return _download_mineru_zip_result(item.get("full_zip_url"), polled, config)
        if state == "failed":
            err = item.get("err_msg") or item.get("error_msg") or item.get("message") or "MinerU failed"
            raise Exception(f"MinerU batch {batch_id} failed: {err}; item={_compact_error_payload(item)}")
        if state and state not in processing_states:
            if progress:
                progress(f"MinerU: batch {batch_id} unexpected state={state}, keep polling...")
    raise Exception(f"MinerU polling timed out for batch {batch_id}. Last response: {_compact_error_payload(last_polled)}")


def _download_mineru_zip_result(zip_url, polled_result, config):
    if not zip_url:
        return polled_result
    zip_resp = requests.get(zip_url, timeout=int(config.get("timeout", 120)))
    zip_resp.raise_for_status()
    payload = {"mineru_result": polled_result, "content_list": [], "markdown": ""}
    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if lower.endswith("_content_list.json") or lower.endswith("content_list.json"):
                with zf.open(name) as f:
                    payload["content_list"] = json.loads(f.read().decode("utf-8"))
            elif lower.endswith("full.md"):
                with zf.open(name) as f:
                    payload["markdown"] = f.read().decode("utf-8")
    return payload


def run_mineru_agent_legacy(img_bytes: bytes, config: dict, filename="page.jpg") -> dict:
    create_url = config.get("endpoint", "https://mineru.net/api/v1/agent/parse/file")
    payload = {
        "file_name": filename,
        "language": config.get("language", "ch"),
        "enable_table": bool(config.get("enable_table", True)),
        "is_ocr": bool(config.get("is_ocr", True)),
        "enable_formula": bool(config.get("enable_formula", True)),
    }
    resp = requests.post(create_url, json=payload, timeout=int(config.get("timeout", 60)))
    resp.raise_for_status()
    result = resp.json()
    data = result.get("data", {})
    task_id = data.get("task_id")
    file_url = data.get("file_url")
    if not task_id or not file_url:
        raise Exception(f"MinerU did not return upload URL: {result}")
    put_resp = requests.put(file_url, data=img_bytes, timeout=int(config.get("timeout", 120)))
    put_resp.raise_for_status()
    poll_url = config.get("poll_endpoint", "https://mineru.net/api/v1/agent/parse/{task_id}").format(task_id=task_id)
    for _ in range(int(config.get("poll_count", 60))):
        time.sleep(float(config.get("poll_interval", 2)))
        poll = requests.get(poll_url, timeout=int(config.get("timeout", 60)))
        poll.raise_for_status()
        polled = poll.json()
        pdata = polled.get("data", {})
        state = pdata.get("state")
        if state == "done":
            markdown = ""
            if pdata.get("markdown_url"):
                md_resp = requests.get(pdata["markdown_url"], timeout=int(config.get("timeout", 60)))
                md_resp.raise_for_status()
                markdown = md_resp.text
            polled["markdown"] = markdown
            return polled
        if state == "failed":
            raise Exception(pdata.get("err_msg") or "MinerU failed")
    raise Exception("MinerU polling timed out.")


def run_quark(img_bytes: bytes, config: dict) -> dict:
    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    if not client_id or not client_secret:
        raise Exception("Quark client_id/client_secret is not configured.")

    business = "vision"
    sign_method = config.get("sign_method", "SHA3-256")
    sign_nonce = uuid.uuid4().hex
    timestamp = int(time.time() * 1000)
    raw_sign = f"{client_id}_{business}_{sign_method}_{sign_nonce}_{timestamp}_{client_secret}".encode("utf-8")
    method = sign_method.lower().replace("_", "-")
    if method == "sha256":
        signature = hashlib.sha256(raw_sign).hexdigest()
    elif method == "sha1":
        signature = hashlib.sha1(raw_sign).hexdigest()
    elif method == "md5":
        signature = hashlib.md5(raw_sign).hexdigest()
    elif method == "sha3-256":
        signature = hashlib.sha3_256(raw_sign).hexdigest()
    else:
        raise Exception(f"Unsupported Quark sign method: {sign_method}")

    input_configs = json.dumps({
        "function_option": config.get("function_option", "RecognizeGeneralDocument")
    }, ensure_ascii=False)
    output_configs = json.dumps({
        "need_return_image": "True" if bool(config.get("need_return_image", True)) else "False"
    }, ensure_ascii=False)
    payload = {
        "dataBase64": base64.b64encode(img_bytes).decode("ascii"),
        "dataType": "image",
        "serviceOption": config.get("service_option", "ocr"),
        "inputConfigs": input_configs,
        "outputConfigs": output_configs,
        "reqId": uuid.uuid4().hex,
        "clientId": client_id,
        "signMethod": sign_method,
        "signNonce": sign_nonce,
        "timestamp": timestamp,
        "signature": signature,
    }
    resp = requests.post(
        config.get("endpoint", "https://scan-business.quark.cn/vision"),
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=int(config.get("timeout", 120)),
    )
    resp.raise_for_status()
    result = resp.json()
    code = result.get("code")
    if code and code != "00000":
        raise Exception(f"Quark OCR failed ({code}): {result}")
    return result
