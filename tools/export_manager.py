import os
import json
import re
import base64
import shutil
from PyQt6.QtWidgets import (
    QMessageBox, QFileDialog, QProgressDialog, QDialog, QVBoxLayout, QFormLayout,
    QComboBox, QDialogButtonBox, QLabel, QApplication, QRadioButton, QLineEdit, QHBoxLayout, QWidget,
)
import requests
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from ocr.ocr_engines import (
    sort_ocr_results_by_priority,
    canonical_engine_id,
    ENGINE_DEFS,
    PADDLE_ENGINE_ID,
    engine_id_from_suffix,
    result_label_from_suffix,
    canonical_ocr_category, excluded_categories, normalize_ocr_result,
)
from lang.i18n import text_from_config


def export_text(parent, key):
    return text_from_config(getattr(parent, "global_config", {}), key)


def parse_export_pages(spec, start_page, end_page):
    pages = set()
    normalized = (spec or "").replace("，", ",").replace("–", "-").replace("—", "-")
    for token in normalized.split(","):
        token = token.strip()
        if not token:
            continue
        if re.fullmatch(r"[0-9]+", token):
            pages.add(int(token))
            continue
        match = re.fullmatch(r"([0-9]+)[ ]*-[ ]*([0-9]+)", token)
        if not match:
            raise ValueError(f"无法识别的页码：{token}")
        first, last = map(int, match.groups())
        if first > last:
            raise ValueError(f"页码范围起点大于终点：{token}")
        pages.update(range(first, last + 1))
    if not pages:
        raise ValueError("请输入至少一个页码。")
    outside = sorted(page for page in pages if page < start_page or page > end_page)
    if outside:
        raise ValueError(
            f"项目页码范围为 {start_page}-{end_page}，以下页码无效："
            + ", ".join(map(str, outside))
        )
    return sorted(pages)


class OcrTextExportDialog(QDialog):
    def __init__(self, parent, sources, start_page, end_page):
        super().__init__(parent)
        self.parent_window = parent
        self.start_page = int(start_page)
        self.end_page = int(end_page)
        self._selected_pages = list(range(self.start_page, self.end_page + 1))
        self.setWindowTitle(export_text(parent, "dlg_export_ocr_text"))
        self.resize(460, 240)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(export_text(parent, "export_mode_priority"), "priority")
        self.mode_combo.addItem(export_text(parent, "export_mode_engine"), "engine")
        self.mode_combo.addItem(export_text(parent, "export_mode_all"), "all")
        form.addRow(export_text(parent, "export_mode"), self.mode_combo)

        self.engine_combo = QComboBox()
        for engine_id, label in sources:
            self.engine_combo.addItem(label, engine_id)
        form.addRow(export_text(parent, "export_engine"), self.engine_combo)

        scope_widget = QWidget()
        scope_layout = QVBoxLayout(scope_widget)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        self.all_pages_radio = QRadioButton(
            f"{export_text(parent, 'export_pages_all')} ({self.start_page}-{self.end_page})"
        )
        self.custom_pages_radio = QRadioButton(export_text(parent, "export_pages_custom"))
        self.page_range_edit = QLineEdit()
        self.page_range_edit.setPlaceholderText("1-10,11-14")
        self.all_pages_radio.setChecked(True)
        custom_row = QHBoxLayout()
        custom_row.addWidget(self.custom_pages_radio)
        custom_row.addWidget(self.page_range_edit, 1)
        scope_layout.addWidget(self.all_pages_radio)
        scope_layout.addLayout(custom_row)
        form.addRow(export_text(parent, "export_page_scope"), scope_widget)

        layout.addLayout(form)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.mode_combo.currentIndexChanged.connect(self._update_state)
        self.all_pages_radio.toggled.connect(self._update_state)
        self.custom_pages_radio.toggled.connect(self._update_state)
        self.page_range_edit.textEdited.connect(
            lambda: self.custom_pages_radio.setChecked(True)
        )
        self._update_state()

    def _update_state(self):
        mode = self.mode_combo.currentData()
        self.engine_combo.setEnabled(mode == "engine")
        self.page_range_edit.setEnabled(self.custom_pages_radio.isChecked())
        if mode == "priority":
            self.hint.setText(export_text(self.parent_window, "export_hint_priority"))
        elif mode == "engine":
            self.hint.setText(export_text(self.parent_window, "export_hint_engine"))
        else:
            self.hint.setText(export_text(self.parent_window, "export_hint_all"))

    def accept(self):
        if self.custom_pages_radio.isChecked():
            try:
                self._selected_pages = parse_export_pages(
                    self.page_range_edit.text(),
                    self.start_page,
                    self.end_page,
                )
            except ValueError as exc:
                QMessageBox.warning(self, export_text(self.parent_window, "export_invalid_pages"), str(exc))
                return
        else:
            self._selected_pages = list(range(self.start_page, self.end_page + 1))
        super().accept()

    def selected_mode(self):
        return self.mode_combo.currentData()

    def selected_engine(self):
        return self.engine_combo.currentData()

    def selected_pages(self):
        return list(self._selected_pages)

class MarkdownImageDownloader(QThread):
    progress_val = pyqtSignal(int)
    progress_text = pyqtSignal(str)
    finished = pyqtSignal(bool, int)

    def __init__(self, tasks):
        super().__init__()
        self.tasks = tasks
        self.is_running = True

    def run(self):
        count = 0
        success = True
        total = len(self.tasks)
        for i, (url, save_path) in enumerate(self.tasks):
            if not self.is_running:
                success = False
                break

            filename = os.path.basename(save_path)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            if os.path.exists(save_path):
                self.progress_text.emit(f"Skipping (Exists) [{i+1}/{total}]: {filename}")
                count += 1
                self.progress_val.emit(i + 1)
                continue

            self.progress_text.emit(f"Downloading [{i+1}/{total}]: {filename}")
            try:
                if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                    _, payload = url.split(",", 1)
                    content = base64.b64decode(payload)
                elif isinstance(url, str) and os.path.isfile(url):
                    shutil.copyfile(url, save_path)
                    count += 1
                    self.progress_val.emit(i + 1)
                    continue
                elif isinstance(url, str) and url.startswith(("http://", "https://")):
                    response = requests.get(
                        url,
                        timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    response.raise_for_status()
                    content = response.content
                else:
                    raise ValueError("Unsupported image source")

                with open(save_path, "wb") as image_file:
                    image_file.write(content)
                count += 1
            except Exception as exc:
                success = False
                print(f"Failed to download image: {url} -> {exc}")
            self.progress_val.emit(i + 1)

        self.finished.emit(success, count)
    def stop(self):
        self.is_running = False


class ExportParser:
    def __init__(self, pages_dict: dict, regex_str: str, group_id: int = 0):
        self.pages_dict = pages_dict # {page_num: text}
        self.group_id = group_id
        if regex_str:
            try:
                self.regex = re.compile(regex_str)
            except:
                self.regex = None
        else:
            self.regex = None
        
    def parse(self):
        entries = []
        if not self.pages_dict or not self.regex: 
            return entries
            
        sorted_pages = sorted(self.pages_dict.keys())
        current_entry = None
        
        for page_num in sorted_pages:
            page_text = self.pages_dict[page_num]
            lines = page_text.split('\n')
            
            page_headword_indices = []
            for i, line in enumerate(lines):
                m = self.regex.search(line)
                if m:
                    page_headword_indices.append((i, m))
                    
            if not page_headword_indices:
                if current_entry:
                    self._append_text_to_entry(current_entry, lines, page_num)
                continue
                
            prev_line_idx = 0
            
            first_hw_line_idx = page_headword_indices[0][0]
            if first_hw_line_idx > 0:
                pre_text_lines = lines[0:first_hw_line_idx]
                if current_entry:
                    self._append_text_to_entry(current_entry, pre_text_lines, page_num)
                
            for k, (line_idx, match) in enumerate(page_headword_indices):
                try:
                    headword = match.group(self.group_id)
                except IndexError:
                    headword = match.group(0)
                    
                content_lines = []
                line_content = lines[line_idx]
                content_lines.append(line_content)
                
                start_next = line_idx + 1
                end_next = len(lines) 
                
                if k < len(page_headword_indices) - 1:
                    end_next = page_headword_indices[k+1][0]
                    
                content_lines.extend(lines[start_next:end_next])
                
                current_entry = {
                    "headword": headword,
                    "text": "", 
                    "pages": [page_num],
                    "page_index": k + 1
                }
                entries.append(current_entry)
                
                self._append_text_to_entry(current_entry, content_lines, page_num)
             
        return entries

    def _append_text_to_entry(self, entry, lines, page_num):
        valid_lines = [l.strip() for l in lines if l.strip()]
        if not valid_lines: return
        
        if page_num not in entry["pages"]:
            entry["pages"].append(page_num)
            
        text_chunk = "\n".join(valid_lines)
        
        if not entry["text"]:
            entry["text"] = text_chunk
        else:
            prev_text = entry["text"]
            if prev_text.endswith('-'):
                entry["text"] = prev_text[:-1] + text_chunk
            else:
                last_char = prev_text[-1]
                is_cjk = ('\u4e00' <= last_char <= '\u9fff')
                
                if is_cjk:
                    entry["text"] = prev_text + text_chunk
                else:
                    entry["text"] = prev_text + " " + text_chunk


class ExportManager:
    def __init__(self, main_window):
        self.mw = main_window

    def _extract_text_from_ocr_data(self, ocr_data):
        txt = []
        for item in ocr_data:
             if isinstance(item, dict): txt.append(item.get('text', ''))
             elif isinstance(item, list): txt.append(item[1][0])
        return "\n".join(txt)

    def _make_progress(self, title, label, maximum):
        dlg = QProgressDialog(label, "Cancel", 0, max(1, maximum), self.mw)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()
        QApplication.processEvents()
        return dlg

    def _index_ocr_result_filenames(self, ocr_dir):
        indexed = {}
        if not ocr_dir or not os.path.isdir(ocr_dir):
            return indexed
        page_pattern = re.compile(r"^page_(\d+)(?:_(.+))?\.json$", re.I)
        bare_pattern = re.compile(r"^(\d+)(?:_(.+))?\.json$", re.I)
        for filename in sorted(os.listdir(ocr_dir)):
            full_path = os.path.join(ocr_dir, filename)
            if not os.path.isfile(full_path):
                continue
            match = page_pattern.match(filename) or bare_pattern.match(filename)
            if not match:
                continue
            real_page = int(match.group(1))
            suffix = match.group(2)
            if suffix:
                info = {
                    "label": result_label_from_suffix(suffix),
                    "engine_id": engine_id_from_suffix(suffix),
                    "path": full_path,
                    "legacy": False,
                }
            else:
                info = {
                    "label": "PaddleOCR",
                    "engine_id": PADDLE_ENGINE_ID,
                    "path": full_path,
                    "legacy": True,
                }
                existing = indexed.setdefault(real_page, [])
                legacy_idx = next((i for i, item in enumerate(existing) if item.get("legacy")), None)
                if legacy_idx is not None:
                    old_name = os.path.basename(existing[legacy_idx].get("path", ""))
                    if filename.lower().startswith("page_") and not old_name.lower().startswith("page_"):
                        existing[legacy_idx] = info
                    continue
                existing.append(info)
                continue
            indexed.setdefault(real_page, []).append(info)
        return indexed

    def _scan_ocr_sources(self, start, end):
        ocr_dir = self.mw.project_config.get("ocr_json_path")
        indexed = self._index_ocr_result_filenames(ocr_dir)
        sources = {}
        pages = {}
        for p in range(start, end + 1):
            real_page = p + self.mw.project_config.get('page_offset', 0)
            results = sort_ocr_results_by_priority(indexed.get(real_page, []), self.mw.global_config)
            pages[p] = results
            for info in results:
                engine_id = canonical_engine_id(info.get("engine_id", ""))
                if engine_id not in sources:
                    sources[engine_id] = info.get("label") or ENGINE_DEFS.get(engine_id, ENGINE_DEFS["paddleocr"]).label
        priority = self.mw.global_config.get("ocr_result_priority") or []
        order = {canonical_engine_id(str(engine_id)): idx for idx, engine_id in enumerate(priority)}
        return pages, sorted(sources.items(), key=lambda item: (order.get(item[0], len(order)), item[1]))

    def _make_ocr_text_for_pages(self, page_results, engine_id=None, progress=None, progress_offset=0, finish_progress=True):
        chunks = []
        used = {}
        for idx, (p, results) in enumerate(page_results.items(), start=1):
            if progress:
                if progress.wasCanceled():
                    break
                progress.setLabelText(f"Reading OCR text page {p}...")
                progress.setValue(progress_offset + idx - 1)
                QApplication.processEvents()
            selected = None
            if engine_id:
                wanted = canonical_engine_id(engine_id)
                for info in results:
                    if canonical_engine_id(info.get("engine_id", "")) == wanted:
                        selected = info
                        break
            elif results:
                selected = results[0]
            if not selected:
                continue

            data = self.mw.load_ocr_json(p, selected)
            if not data:
                continue
            text = self._extract_text_from_ocr_data(data)
            if not text.strip():
                continue
            source_label = selected.get("label") or selected.get("engine_id") or "OCR"
            used[source_label] = used.get(source_label, 0) + 1
            chunks.append(f"<{p}>\n{text}\n")
        if progress and finish_progress:
            progress.setValue(progress.maximum())
            QApplication.processEvents()
        canceled = bool(progress and progress.wasCanceled())
        return "".join(chunks), used, canceled

    def _safe_filename_part(self, text):
        return re.sub(r'[\\/*?:"<>|]+', '_', text).strip() or "ocr"

    @staticmethod
    def _find_markdown_text(payload):
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key.lower() in {"markdown", "markdown_text", "markdowntext"}:
                    if isinstance(value, str):
                        return value
                    if isinstance(value, dict) and isinstance(value.get("text"), str):
                        return value["text"]
            for value in payload.values():
                found = ExportManager._find_markdown_text(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = ExportManager._find_markdown_text(value)
                if found:
                    return found
        return ""

    @staticmethod
    def _collect_returned_images(payload):
        images = {}
        image_words = ("image", "img", "picture", "figure")
        image_extensions = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")

        def add_image(reference, value, mime="image/png"):
            if not isinstance(value, str):
                return
            source = value.strip()
            key_text = str(reference or f"image_{len(images) + 1}")
            if source.startswith("data:image"):
                images.setdefault(key_text, source)
            elif source.startswith(("http://", "https://")):
                images.setdefault(key_text, source)
            elif os.path.isfile(source):
                images.setdefault(key_text, os.path.abspath(source))
            elif len(source) > 128:
                try:
                    base64.b64decode(source[: min(len(source), 4096)], validate=False)
                    images.setdefault(key_text, f"data:{mime};base64,{source}")
                except Exception:
                    pass

        def walk(value, path="", image_context=False):
            if isinstance(value, dict):
                mime = value.get("mime_type") or value.get("mimeType") or "image/png"
                reference = (
                    value.get("path")
                    or value.get("name")
                    or value.get("filename")
                    or value.get("element_id")
                    or value.get("id")
                    or path
                )
                for key, child in value.items():
                    lower_key = str(key).lower()
                    child_path = f"{path}.{key}" if path else str(key)
                    key_is_image = any(word in lower_key for word in image_words)
                    if isinstance(child, str):
                        looks_like_file = child.lower().split("?", 1)[0].endswith(image_extensions)
                        if key_is_image or image_context or looks_like_file:
                            add_image(reference or child_path, child, mime)
                    else:
                        walk(child, child_path, image_context or key_is_image)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]", image_context)

        walk(payload)
        return images
    def _extract_markdown_payload_by_engine(self, raw, engine_id):
        engine_id = canonical_engine_id(engine_id)
        if (
            isinstance(raw, dict)
            and "raw" in raw
            and ("__engine" in raw or "__model" in raw)
        ):
            engine_id = canonical_engine_id(raw.get("__engine") or engine_id)
            raw = raw.get("raw")

        if engine_id == "chrome_lens":
            return "", {}

        images = self._collect_returned_images(raw)

        if engine_id == "textin":
            data = raw.get("data", raw) if isinstance(raw, dict) else {}
            md_text = self._find_markdown_text(data)
            for element in data.get("elements", []) if isinstance(data, dict) else []:
                if not isinstance(element, dict) or str(element.get("type", "")).lower() != "image":
                    continue
                image_data = element.get("image_data") or {}
                image_url = image_data.get("image_url")
                image_b64 = image_data.get("base64")
                image_ref = element.get("element_id") or f"textin_image_{len(images) + 1}"
                if image_url:
                    images[image_ref] = image_url
                elif image_b64:
                    mime = image_data.get("mime_type") or "image/png"
                    images[image_ref] = f"data:{mime};base64,{image_b64}"
            return md_text, images

        if engine_id == "mineru":
            if isinstance(raw, dict):
                images.update(raw.get("images", {}) or {})
            return self._find_markdown_text(raw), images

        if engine_id == "quark":
            return self._find_markdown_text(raw), images

        if not isinstance(raw, dict):
            return "", images
        results = raw.get("layoutParsingResults") or []
        if results and isinstance(results[0], dict):
            md_data = results[0].get("markdown") or {}
            if isinstance(md_data, dict):
                images.update(md_data.get("images", {}) or {})
                return md_data.get("text", ""), images
            if isinstance(md_data, str):
                return md_data, images
        return self._find_markdown_text(raw), images
    def _image_extension_from_url(self, url):
        if isinstance(url, str) and url.startswith("data:image"):
            match = re.match(r"data:image/([^;]+);", url)
            if match:
                ext = match.group(1).lower()
                return "jpg" if ext == "jpeg" else ext
        name = os.path.basename(str(url).split("?", 1)[0])
        ext = os.path.splitext(name)[1].lstrip(".")
        return ext or "png"

    def _replace_markdown_image_refs(self, md_text, page_num, images_dict, md_dir, image_tasks):
        candidates = []
        for img_key, value in (images_dict or {}).items():
            img_url = value
            if isinstance(value, dict):
                img_url = value.get("url") or value.get("image_url") or value.get("data")
                if not img_url and value.get("base64"):
                    mime = value.get("mime_type") or "image/png"
                    img_url = f"data:{mime};base64,{value['base64']}"
            if isinstance(img_url, str) and (img_url.startswith(("http://", "https://", "data:")) or os.path.isfile(img_url)):
                candidates.append((str(img_key), img_url))

        candidates.sort(
            key=lambda item: 0
            if str(item[0]) in md_text or (len(str(item[1])) < 2048 and str(item[1]) in md_text)
            else 1
        )
        unique_candidates = []
        seen_sources = set()
        for candidate in candidates:
            if candidate[1] in seen_sources:
                continue
            seen_sources.add(candidate[1])
            unique_candidates.append(candidate)
        candidates = unique_candidates

        known_urls = {url for _, url in candidates}
        direct_urls = re.findall(
            r"""!\[[^\]]*\]\((https?://[^)\s]+)\)|<img\b[^>]*\bsrc=["'](https?://[^"']+)["']""",
            md_text,
            flags=re.IGNORECASE,
        )
        for markdown_url, html_url in direct_urls:
            url = markdown_url or html_url
            if url and url not in known_urls:
                candidates.append((url, url))
                known_urls.add(url)

        scheduled = {(url, path) for url, path in image_tasks}
        appended_refs = []
        for img_key, img_url in candidates:
            key_name = os.path.basename(str(img_key).split("?", 1)[0]) or f"image_{len(image_tasks) + 1}"
            if "." not in key_name:
                key_name = f"{key_name}.{self._image_extension_from_url(img_url)}"
            img_filename = f"page_{page_num}_{self._safe_filename_part(key_name)}"
            local_rel_path = f"imgs/{img_filename}"
            referenced = any(old and old in md_text for old in {str(img_key), img_url})
            for old in {str(img_key), img_url}:
                md_text = md_text.replace(f'src="{old}"', f'src="{local_rel_path}"')
                md_text = md_text.replace(f"src='{old}'", f"src='{local_rel_path}'")
                md_text = md_text.replace(f"]({old})", f"]({local_rel_path})")
            task = (img_url, os.path.join(md_dir, "imgs", img_filename))
            if task not in scheduled:
                image_tasks.append(task)
                scheduled.add(task)
            if not referenced:
                appended_refs.append(f"![]({local_rel_path})")
        if appended_refs:
            separator = chr(10) + chr(10)
            md_text = md_text.rstrip() + separator + separator.join(appended_refs)
        return md_text
    @staticmethod
    def _remove_markdown_tables(md_text):
        lines = md_text.splitlines()
        remove = set()
        separator = re.compile(
            r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
        )
        for index, line in enumerate(lines):
            if not separator.match(line):
                continue
            remove.add(index)
            if index > 0 and "|" in lines[index - 1]:
                remove.add(index - 1)
            cursor = index + 1
            while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
                remove.add(cursor)
                cursor += 1
        return chr(10).join(line for index, line in enumerate(lines) if index not in remove)

    def _filter_markdown_categories(self, md_text, raw, engine_id, images):
        excluded = excluded_categories(self.mw.global_config)
        if not excluded:
            return md_text, images

        engine_id = canonical_engine_id(engine_id)
        all_categories_config = dict(self.mw.global_config)
        all_categories_config["ocr_excluded_categories"] = []
        blocks = normalize_ocr_result(raw, engine_id, all_categories_config)

        excluded_lines = set()
        for block in blocks:
            label = block.get("block_label") or block.get("label") or block.get("type") or "text"
            category = block.get("ocr_category") or canonical_ocr_category(label, engine_id)
            if category not in excluded:
                continue
            block_text = str(block.get("text") or "").strip()
            if not block_text:
                continue
            if chr(10) in block_text and len(block_text) >= 8:
                md_text = md_text.replace(block_text, "")
            else:
                excluded_lines.add(block_text)

        if excluded_lines:
            kept_lines = []
            prefix = re.compile(r"^\s{0,3}(?:#{1,6}|>|[-*+])\s*")
            for line in md_text.splitlines():
                visible_line = prefix.sub("", line).strip()
                visible_line = re.sub(r"<[^>]+>", "", visible_line).strip()
                if visible_line not in excluded_lines:
                    kept_lines.append(line)
            md_text = chr(10).join(kept_lines)

        if "header" in excluded:
            md_text = re.sub(
                r"<header\b[^>]*>.*?</header>",
                "",
                md_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if "footer" in excluded:
            md_text = re.sub(
                r"<footer\b[^>]*>.*?</footer>",
                "",
                md_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if {"image", "chart"} & excluded:
            md_text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md_text)
            md_text = re.sub(r"<img\b[^>]*>", "", md_text, flags=re.IGNORECASE)
            md_text = re.sub(
                r"<figure\b[^>]*>.*?</figure>",
                "",
                md_text,
                flags=re.IGNORECASE | re.DOTALL,
            )

        if "table" in excluded:
            md_text = re.sub(
                r"<table\b[^>]*>.*?</table>",
                "",
                md_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            md_text = self._remove_markdown_tables(md_text)

        if "formula" in excluded:
            md_text = re.sub(r"\$\$.*?\$\$", "", md_text, flags=re.DOTALL)
            md_text = re.sub(r"\\\[.*?\\\]", "", md_text, flags=re.DOTALL)
            md_text = re.sub(r"\\\(.*?\\\)", "", md_text, flags=re.DOTALL)
            md_text = re.sub(r"(?<!\\)\$(?!\$).*?(?<!\\)\$", "", md_text)

        if "image" in excluded:
            images = {}
        else:
            excluded_tokens = set()
            if "chart" in excluded:
                excluded_tokens.update(("chart", "graph", "diagram"))
            if "table" in excluded:
                excluded_tokens.update(("table", "grid"))
            if "formula" in excluded:
                excluded_tokens.update(("formula", "equation"))
            if excluded_tokens:
                images = {
                    key: value
                    for key, value in (images or {}).items()
                    if not any(token in str(key).lower() for token in excluded_tokens)
                }

        md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip()
        return md_text, images
    def _markdown_from_ocr_result(self, page_num, result_info, with_images, md_dir, image_tasks):
        try:
            with open(result_info["path"], 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except Exception as e:
            print(f"Markdown parse error page {page_num}: {e}")
            return ""

        engine_id = result_info.get("engine_id", "paddleocr")
        md_text, images_dict = self._extract_markdown_payload_by_engine(raw, engine_id)
        md_text, images_dict = self._filter_markdown_categories(
            md_text,
            raw,
            engine_id,
            images_dict,
        )
        if not md_text:
            data = self.mw.load_ocr_json(page_num, result_info)
            md_text = self._extract_text_from_ocr_data(data) if data else ""
        if not md_text.strip():
            return ""

        if with_images:
            md_text = self._replace_markdown_image_refs(md_text, page_num, images_dict, md_dir, image_tasks)
        return md_text

    def _make_markdown_for_pages(self, page_results, engine_id=None, with_images=False, md_dir="", progress=None, progress_offset=0, finish_progress=True):
        md_texts = []
        image_tasks = []
        used = {}
        for idx, (p, results) in enumerate(page_results.items(), start=1):
            if progress:
                if progress.wasCanceled():
                    break
                progress.setLabelText(f"Reading OCR markdown page {p}...")
                progress.setValue(progress_offset + idx - 1)
                QApplication.processEvents()
            selected = None
            if engine_id:
                wanted = canonical_engine_id(engine_id)
                for info in results:
                    if canonical_engine_id(info.get("engine_id", "")) == wanted:
                        selected = info
                        break
            elif results:
                selected = results[0]
            if not selected:
                continue
            md_text = self._markdown_from_ocr_result(p, selected, with_images, md_dir, image_tasks)
            if not md_text:
                continue
            label = selected.get("label") or selected.get("engine_id") or "OCR"
            used[label] = used.get(label, 0) + 1
            md_texts.append(f"<{p}>\n{md_text}")
        if progress and finish_progress:
            progress.setValue(progress.maximum())
            QApplication.processEvents()
        canceled = bool(progress and progress.wasCanceled())
        return "\n\n".join(md_texts), image_tasks, used, canceled

    def export_slices(self):
        pg = self.mw.get_current_page()
        data = self.mw.load_ocr_json(pg)
        if not data:
            QMessageBox.warning(self.mw, "Error", f"No OCR data for page {pg}")
            return
            
        doc = None
        if self.mw.project_config.get('pdf_path') and os.path.exists(self.mw.project_config.get('pdf_path')):
            import fitz
            try: doc = fitz.open(self.mw.project_config.get('pdf_path'))
            except: pass
            
        from ocr.ocr_utils import get_page_image, cut_image
        real_page = pg + self.mw.project_config.get('page_offset', 0)
        img_bytes = get_page_image(doc, self.mw.project_config.get('image_dir'), real_page)
        if doc: doc.close()
        
        if not img_bytes:
            QMessageBox.warning(self.mw, "Error", "Could not load image.")
            return
            
        default_dir = self.mw.project_config.get("export_dir", "")
        export_dir = QFileDialog.getExistingDirectory(self.mw, "Select slice export directory", default_dir)
        if not export_dir: return
        
        count = 0
        try:
            for item in data:
                if isinstance(item, dict) and 'bbox' in item:
                    box = item['bbox']
                    slice_img = cut_image(img_bytes, box)
                    if slice_img:
                        txt = item.get('text', f'slice_{count}')
                        safe_txt = re.sub(r'[\\/*?:"<>|]', '_', txt)[:30]
                        path = os.path.join(export_dir, f"{safe_txt}.jpg")
                        slice_img.save(path)
                        count += 1
            if count > 0:
                QMessageBox.information(self.mw, "Success", f"Exported {count} slices.")
            else:
                 QMessageBox.warning(self.mw, "Warning", "No bbox slices found.")
        except Exception as e:
            QMessageBox.warning(self.mw, "Error", f"Export slice failed: {e}")

    def export_ocr_dict_current(self):
        pg = self.mw.get_current_page()
        data = self.mw.load_ocr_json(pg)
        if not data: return
        
        text = self._extract_text_from_ocr_data(data)
        if not text.strip(): return
        
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QHBoxLayout, QPushButton
        from PyQt6.QtGui import QFont
        d = QDialog(self.mw)
        d.setWindowTitle(f"OCR Text - Page {pg}")
        d.resize(500, 600)
        l = QVBoxLayout(d)
        
        te = QTextEdit()
        te.setReadOnly(True)
        te.setPlainText(text)
        te.setFont(QFont("Consolas", 11))
        l.addWidget(te)
        
        hb = QHBoxLayout()
        b1 = QPushButton("Copy to Clipboard")
        b1.clicked.connect(lambda: self.mw.clipboard.setText(text))
        b2 = QPushButton("Close")
        b2.clicked.connect(d.accept)
        hb.addWidget(b1)
        hb.addWidget(b2)
        l.addLayout(hb)
        d.exec()

    def export_all_ocr_txt(self):
        start = self.mw.project_config.get("start_page", 1)
        end = self.mw.project_config.get("end_page", 1)
        page_results, sources = self._scan_ocr_sources(start, end)
        if not any(page_results.values()):
            QMessageBox.warning(self.mw, "Warning", "No OCR results found.")
            return

        dlg = OcrTextExportDialog(self.mw, sources, start, end)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected_pages = set(dlg.selected_pages())
        page_results = {
            page: results
            for page, results in page_results.items()
            if page in selected_pages
        }
        if not any(page_results.values()):
            QMessageBox.warning(self.mw, "Warning", "所选页码没有 OCR 结果。")
            return
        proj_name = self.mw.project_config.get("name", "project")
        mode = dlg.selected_mode()

        if mode == "all":
            export_dir = QFileDialog.getExistingDirectory(
                self.mw,
                "Select OCR text export directory",
                self.mw.project_config.get("export_dir", ""),
            )
            if not export_dir:
                return
            written = []
            progress = self._make_progress("Export OCR Text", "Reading OCR text...", len(sources) * len(page_results))
            try:
                offset = 0
                for engine_id, label in sources:
                    text, used, canceled = self._make_ocr_text_for_pages(
                        page_results,
                        engine_id,
                        progress=progress,
                        progress_offset=offset,
                        finish_progress=False,
                    )
                    offset += len(page_results)
                    progress.setValue(min(offset, progress.maximum()))
                    if canceled or progress.wasCanceled():
                        break
                    if not text.strip():
                        continue
                    filename = os.path.join(export_dir, f"{proj_name}_{self._safe_filename_part(label)}.txt")
                    with open(filename, 'w', encoding='utf-8') as f:
                        f.write(text)
                    written.append(os.path.basename(filename))
            finally:
                progress.close()
            if written:
                QMessageBox.information(self.mw, "Done", "Exported OCR text files:\n" + "\n".join(written))
            else:
                QMessageBox.warning(self.mw, "Warning", "No text content found in OCR results.")
            return

        engine_id = dlg.selected_engine() if mode == "engine" else None
        progress = self._make_progress("Export OCR Text", "Reading OCR text...", len(page_results))
        try:
            full_text, used, canceled = self._make_ocr_text_for_pages(page_results, engine_id, progress=progress)
        finally:
            progress.close()
        if canceled:
            return
        if not full_text.strip():
            QMessageBox.warning(self.mw, "Warning", "No text content found for the selected OCR source.")
            return

        if mode == "engine":
            label = dict(sources).get(engine_id, engine_id or "ocr")
            default_name = f"{proj_name}_{self._safe_filename_part(label)}.txt"
        else:
            labels = list(used.keys())
            suffix = "mixed_ocr" if len(labels) > 1 else self._safe_filename_part(labels[0] if labels else "ocr")
            default_name = f"{proj_name}_{suffix}.txt"

        filename, _ = QFileDialog.getSaveFileName(self.mw, "Export OCR", default_name, "Text Files (*.txt)")
        if not filename:
            return
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(full_text)
        source_summary = ", ".join(f"{label}: {count}" for label, count in sorted(used.items()))
        QMessageBox.information(self.mw, "Done", f"Exported OCR text to {filename}.\nSources: {source_summary}")

    def export_all_markdown(self, with_images=False):
        ocr_dir = self.mw.project_config.get("ocr_json_path")
        if not ocr_dir or not os.path.exists(ocr_dir):
            QMessageBox.warning(self.mw, "Error", "OCR JSON path is invalid. Please check settings.")
            return

        start_p = self.mw.project_config.get("start_page", 1)
        end_p = self.mw.project_config.get("end_page", 1)
        page_results, sources = self._scan_ocr_sources(start_p, end_p)
        if not any(page_results.values()):
            QMessageBox.warning(self.mw, "Warning", "No OCR results found.")
            return

        dlg = OcrTextExportDialog(self.mw, sources, start_p, end_p)
        dlg.setWindowTitle(export_text(self.mw, "dlg_export_ocr_markdown"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected_pages = set(dlg.selected_pages())
        page_results = {
            page: results
            for page, results in page_results.items()
            if page in selected_pages
        }
        if not any(page_results.values()):
            QMessageBox.warning(self.mw, "Warning", "所选页码没有 OCR 结果。")
            return
        proj_name = self.mw.project_config.get("name", "project")
        mode = dlg.selected_mode()

        if mode == "all":
            export_dir = QFileDialog.getExistingDirectory(
                self.mw,
                "Select Markdown export directory",
                self.mw.project_config.get("export_dir", ""),
            )
            if not export_dir:
                return
            written = []
            all_image_tasks = []
            progress = self._make_progress("Export OCR Markdown", "Reading OCR markdown...", len(sources) * len(page_results))
            try:
                offset = 0
                for engine_id, label in sources:
                    md_text, image_tasks, used, canceled = self._make_markdown_for_pages(
                        page_results,
                        engine_id,
                        with_images,
                        export_dir,
                        progress=progress,
                        progress_offset=offset,
                        finish_progress=False,
                    )
                    offset += len(page_results)
                    progress.setValue(min(offset, progress.maximum()))
                    if canceled or progress.wasCanceled():
                        break
                    if not md_text.strip():
                        continue
                    filename = os.path.join(export_dir, f"{proj_name}_{self._safe_filename_part(label)}.md")
                    try:
                        with open(filename, "w", encoding='utf-8') as f:
                            f.write(md_text)
                        written.append(os.path.basename(filename))
                        all_image_tasks.extend(image_tasks)
                    except Exception as e:
                        QMessageBox.critical(self.mw, "Error", f"Failed to save Markdown: {e}")
                        return
            finally:
                progress.close()
            if not written:
                QMessageBox.warning(self.mw, "Warning", "No markdown content found in OCR results.")
                return
            if with_images and all_image_tasks:
                self._start_markdown_image_download(all_image_tasks, export_dir)
            else:
                QMessageBox.information(self.mw, "Success", "Exported Markdown files:\n" + "\n".join(written))
            return

        engine_id = dlg.selected_engine() if mode == "engine" else None
        if mode == "engine":
            label = dict(sources).get(engine_id, engine_id or "ocr")
            default_name = f"{proj_name}_{self._safe_filename_part(label)}.md"
        else:
            default_name = f"{proj_name}_mixed_ocr.md"

        out_md_path, _ = QFileDialog.getSaveFileName(self.mw, "Save Markdown", default_name, "Markdown (*.md)")
        if not out_md_path:
            return

        md_dir = os.path.dirname(out_md_path)
        progress = self._make_progress("Export OCR Markdown", "Reading OCR markdown...", len(page_results))
        try:
            md_text, image_tasks, used, canceled = self._make_markdown_for_pages(
                page_results,
                engine_id,
                with_images,
                md_dir,
                progress=progress,
            )
        finally:
            progress.close()
        if canceled:
            return
        if not md_text.strip():
            QMessageBox.warning(self.mw, "Warning", "No markdown content found in OCR results.")
            return

        try:
            with open(out_md_path, "w", encoding='utf-8') as f:
                f.write(md_text)
        except Exception as e:
            QMessageBox.critical(self.mw, "Error", f"Failed to save Markdown: {e}")
            return

        if with_images and image_tasks:
            self._start_markdown_image_download(image_tasks, out_md_path)
        else:
            source_summary = ", ".join(f"{label}: {count}" for label, count in sorted(used.items()))
            QMessageBox.information(self.mw, "Success", f"Exported Markdown to {out_md_path}\nSources: {source_summary}")

    def _start_markdown_image_download(self, image_tasks, out_path):
        self.mw.md_img_worker = MarkdownImageDownloader(image_tasks)

        self.mw.progress_dlg = QProgressDialog("Downloading Images...", "Cancel", 0, len(image_tasks), self.mw)
        self.mw.progress_dlg.setWindowTitle("Exporting Images")
        self.mw.progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.mw.progress_dlg.setMinimumDuration(0)
        self.mw.progress_dlg.show()

        self.mw.md_img_worker.progress_val.connect(self.mw.progress_dlg.setValue)
        self.mw.md_img_worker.progress_text.connect(self.mw.progress_dlg.setLabelText)
        self.mw.md_img_worker.finished.connect(lambda s, c: self.on_md_img_finished(s, c, out_path))
        self.mw.progress_dlg.canceled.connect(self.mw.md_img_worker.stop)

        self.mw.md_img_worker.start()

    def on_md_img_finished(self, success, count, out_md_path):
        if hasattr(self.mw, "progress_dlg"):
            self.mw.progress_dlg.close()
        target_dir = out_md_path if os.path.isdir(out_md_path) else os.path.dirname(out_md_path)
        msg = f"Exported Markdown and {count} images to {target_dir}"
        if success:
            QMessageBox.information(self.mw, "Success", msg)
        else:
            QMessageBox.warning(
                self.mw,
                "Incomplete",
                msg + chr(10) + "(Some images failed to download or the operation was cancelled.)",
            )
        self.mw.md_img_worker = None
    def _confirm_overwrite_if_exists(self, filepath):
        if os.path.exists(filepath):
            return QMessageBox.question(self.mw, "Overwrite", f"File '{filepath}' already exists.\nOverwrite?", 
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        return True

    def export_parsed(self, side, fmt):
        pages = self.mw.pages_left if side == 'left' else self.mw.pages_right_text
        if not pages:
            QMessageBox.warning(self.mw, "Error", f"No data for {side} side.")
            return
            
        reg = self.mw.project_config.get("regex_left" if side == 'left' else "regex_right")
        if not reg:
            QMessageBox.warning(self.mw, "Error", f"No regex configured for {side} side.")
            return
            
        grp = self.mw.project_config.get("regex_group_left" if side == 'left' else "regex_group_right", 0)
            
        try:
            parser = ExportParser(pages, reg, grp)
            entries = parser.parse()
            
            if not entries:
                QMessageBox.warning(self.mw, "Result", "No entries parsing found (check regex).")
                return
                
            project_name = self.mw.project_config.get("name", "project")
            base_name = f"{project_name}_{side}.{fmt if fmt=='json' else 'txt'}"
            
            export_dir = self.mw.project_config.get("export_dir")
            filename = ""
            
            if export_dir and os.path.exists(export_dir):
                filename = os.path.join(export_dir, base_name)
                if not self._confirm_overwrite_if_exists(filename): return
            else:
                filename, _ = QFileDialog.getSaveFileName(self.mw, f"Export {side.upper()} {fmt.upper()}", base_name, 
                                                          f"{fmt.upper()} (*.{fmt if fmt=='json' else 'txt'})")
            
            if not filename: return

            with open(filename, 'w', encoding='utf-8') as f:
                if fmt == 'json':
                    json.dump(entries, f, ensure_ascii=False, indent=2)
                elif fmt == 'mdx':
                    for e in entries:
                        f.write(f"{e['headword']}\n")
                        f.write(f"{e['text']}\n".replace('\n','<br>\n'))
                        f.write("</>\n")
                        
            QMessageBox.information(self.mw, "Success", f"Exported {len(entries)} entries to {filename}")

        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self.mw, "Error", str(e))

    def export_parsed_with_images(self, side, fmt):
        export_dir = self.mw.project_config.get("export_dir")
        if not export_dir or not os.path.exists(export_dir):
            export_dir = QFileDialog.getExistingDirectory(self.mw, "Select Export Directory")
            if not export_dir: return
            
            self.mw.project_config["export_dir"] = export_dir
            self.mw.config_manager.save()
            
        force_overwrite = self.mw.action_force_recreate.isChecked()
        
        pages = self.mw.pages_left if side == 'left' else self.mw.pages_right_text
        if not pages:
            QMessageBox.warning(self.mw, "Error", f"No data for {side} side.")
            return

        reg = self.mw.project_config.get("regex_left" if side == 'left' else "regex_right")
        if not reg:
            QMessageBox.warning(self.mw, "Error", f"No regex configured for {side} side.")
            return
            
        grp = self.mw.project_config.get("regex_group_left" if side == 'left' else "regex_group_right", 0)
        
        try:
            parser = ExportParser(pages, reg, grp)
            entries = parser.parse()
            
            if not entries:
                QMessageBox.warning(self.mw, "Result", "No entries parsing found.")
                return
            
            from ocr.ocr_worker import ImageExportWorker
            worker_config = dict(self.mw.project_config)
            worker_config["_image_export_source_path"] = (
                self.mw.project_config.get("text_path_left", "")
                if side == "left"
                else self.mw.get_current_right_text_path()
            )
            self.mw.img_export_worker = ImageExportWorker(
                entries, worker_config, fmt, export_dir, side, force_overwrite
            )
            
            self.mw.progress_dlg = QProgressDialog("Initializing...", "Cancel", 0, len(entries), self.mw)
            self.mw.progress_dlg.setWindowTitle("Exporting Images")
            self.mw.progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
            self.mw.progress_dlg.setMinimumDuration(0)
            self.mw.progress_dlg.show()
            
            self.mw.img_export_worker.progress.connect(self.mw.progress_dlg.setLabelText)
            self.mw.img_export_worker.progress_val.connect(self.mw.progress_dlg.setValue)
            self.mw.img_export_worker.finished.connect(self.on_img_export_finished)
            self.mw.progress_dlg.canceled.connect(self.mw.img_export_worker.stop)
            
            self.mw.img_export_worker.start()
            
        except Exception as e:
            QMessageBox.critical(self.mw, "Error", str(e))

    def on_img_export_finished(self, success, msg):
        if hasattr(self.mw, 'progress_dlg'):
            self.mw.progress_dlg.close()
        if success:
            QMessageBox.information(self.mw, "Success", msg)
        else:
            QMessageBox.warning(self.mw, "Error", f"Export stopped/failed: {msg}")
        self.mw.img_export_worker = None
