import os
import json
import time
import threading
import requests
import re
import fitz
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImageReader

from ocr.ocr_utils import get_page_image, TextToBBoxMapper, BBoxMerger, ImageStitcher

# v2 API constants
_V2_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
_V2_DEFAULT_MODEL = "PaddleOCR-VL-1.6"

# All supported remote models
V2_MODELS = [
    "PaddleOCR-VL-1.6",
    "PaddleOCR-VL-1.5",
    "PP-OCRv5",
    "PP-StructureV3",
]

# ==========================================
# OCR Engine Registry
# ==========================================
import importlib.util

def _build_remote_label(model: str) -> str:
    return "远程 API"

_ENGINES = [
    {'id': 'remote', 'label': '远程 API', 'available': True},
]

# Detect PaddleOCR without importing it (avoids heavy startup cost)
if importlib.util.find_spec('paddleocr') is not None:
    _ENGINES.append({'id': 'local', 'label': 'PaddleOCR (Local)', 'available': True})


def refresh_remote_engine_label(model: str):
    """Update the remote engine label when the model config changes."""
    for e in _ENGINES:
        if e['id'] == 'remote':
            e['label'] = _build_remote_label(model)
            break


def get_available_engines():
    """Return list of available OCR engine dicts (id, label)."""
    return [e for e in _ENGINES if e['available']]



class OCRWorker(QThread):
    progress = pyqtSignal(str)
    page_done = pyqtSignal(int, int)  # (completed_count, total_count)
    finished = pyqtSignal(bool, str)  # success, message

    def __init__(self, mode, page_list, project_config, global_config, engine="remote"):
        super().__init__()
        self.mode = mode  # 'single' or 'batch'
        self.page_list = page_list  # list of page numbers (int)
        self.project_config = project_config
        self.global_config = global_config
        self.engine = engine
        self.pdf_path = project_config.get('pdf_path')
        self._is_running = True
        self._executor = None  # ThreadPoolExecutor reference for shutdown

    # ------------------------------------------------------------------
    # Main thread entry point
    # ------------------------------------------------------------------
    def run(self):
        doc = None
        if self.pdf_path and os.path.exists(self.pdf_path):
            try:
                doc = fitz.open(self.pdf_path)
            except:
                pass

        token = self.global_config.get("ocr_api_token", "")
        model = self.global_config.get("ocr_api_model", _V2_DEFAULT_MODEL)
        retry_count = int(self.global_config.get("ocr_retry_count", 3))
        concurrent = int(self.global_config.get("ocr_concurrent_tasks", 2))

        save_dir = self.project_config.get("ocr_json_path", "ocr_results")
        if not os.path.exists(save_dir):
            try:
                os.makedirs(save_dir)
            except:
                pass

        total = len(self.page_list)
        success_count = 0

        # ------ Local engine: simple serial loop ------
        if self.engine == "local":
            for i, page_num in enumerate(self.page_list):
                if not self._is_running:
                    break
                try:
                    self.progress.emit(f"Processing page {page_num} ({i+1}/{total})...")
                    real_page_num = page_num + self.project_config.get("page_offset", 0)
                    img_bytes = get_page_image(doc, self.project_config.get('image_dir'), real_page_num)
                    if not img_bytes:
                        if self.mode == 'single':
                            raise Exception(f"No image found for page {page_num}")
                        else:
                            continue

                    if not any(e['id'] == 'local' for e in _ENGINES):
                        raise Exception("Local OCR module not loaded.")
                    from paddleocr import PaddleOCRVL
                    temp_path = os.path.join(save_dir, f"temp_{real_page_num}.thumb")
                    with open(temp_path, "wb") as f:
                        f.write(img_bytes)
                    try:
                        ocr = PaddleOCRVL()
                        res = ocr.predict(temp_path)
                        result = res[0] if res else []
                    finally:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)

                    json_path = os.path.join(save_dir, f"page_{real_page_num}.json")
                    with open(json_path, "w", encoding='utf8') as jf:
                        json.dump(result, jf, ensure_ascii=False, indent=2)
                    success_count += 1
                    self.page_done.emit(i + 1, total)

                except Exception as e:
                    print(f"OCR Error page {page_num}: {e}")
                    if self.mode == 'single':
                        self.finished.emit(False, str(e))
                        if doc:
                            doc.close()
                        return

        # ------ Remote engine: concurrent with retry ------
        else:
            # Pre-load all images on the main doc thread (fitz is not thread-safe)
            page_images = {}  # page_num -> img_bytes
            for page_num in self.page_list:
                real_page_num = page_num + self.project_config.get("page_offset", 0)
                img_bytes = get_page_image(doc, self.project_config.get('image_dir'), real_page_num)
                page_images[page_num] = img_bytes

            counter_lock = threading.Lock()
            done_count = 0

            def process_page(page_num):
                """Worker function executed in thread pool."""
                real_page_num = page_num + self.project_config.get("page_offset", 0)
                img_bytes = page_images.get(page_num)

                if not img_bytes:
                    if self.mode == 'single':
                        raise Exception(f"No image found for page {page_num}")
                    return False, page_num, "no image"

                last_exc = None
                for attempt in range(retry_count):
                    if not self._is_running:
                        return False, page_num, "cancelled"
                    try:
                        result = self._run_v2_remote(img_bytes, token, model, page_num)
                        json_path = os.path.join(save_dir, f"page_{real_page_num}.json")
                        with open(json_path, "w", encoding='utf8') as jf:
                            json.dump(result, jf, ensure_ascii=False, indent=2)
                        return True, page_num, None
                    except Exception as e:
                        last_exc = e
                        if attempt < retry_count - 1:
                            wait = 2 ** attempt  # 1s, 2s, 4s …
                            self.progress.emit(
                                f"Page {page_num}: attempt {attempt+1} failed ({e}), "
                                f"retrying in {wait}s..."
                            )
                            # Interruptible sleep
                            for _ in range(wait * 2):
                                if not self._is_running:
                                    return False, page_num, "cancelled"
                                time.sleep(0.5)

                return False, page_num, str(last_exc)

            # Run concurrent tasks
            effective_workers = min(concurrent, total) if total > 0 else 1
            self._executor = ThreadPoolExecutor(max_workers=effective_workers)
            futures = {
                self._executor.submit(process_page, pn): pn
                for pn in self.page_list
            }

            try:
                for future in as_completed(futures):
                    if not self._is_running:
                        break
                    pn = futures[future]
                    try:
                        ok, page_num, err = future.result()
                    except Exception as exc:
                        ok, page_num, err = False, pn, str(exc)

                    with counter_lock:
                        done_count += 1
                        current_done = done_count

                    if ok:
                        success_count += 1
                        self.page_done.emit(current_done, total)
                        self.progress.emit(
                            f"Page {page_num} done ✓ ({current_done}/{total})"
                        )
                    else:
                        self.page_done.emit(current_done, total)
                        if err == "cancelled":
                            self.progress.emit(f"Page {page_num} cancelled.")
                        else:
                            self.progress.emit(
                                f"Page {page_num} FAILED after {retry_count} attempts: {err}"
                            )
                            if self.mode == 'single':
                                self._executor.shutdown(wait=False)
                                self.finished.emit(False, err or "Remote OCR failed")
                                if doc:
                                    doc.close()
                                return
            finally:
                self._executor.shutdown(wait=False)
                self._executor = None

        if doc:
            doc.close()
        self.finished.emit(True, f"Batch OCR Done. {success_count}/{total} processed.")

    # ------------------------------------------------------------------
    # v2 Async Job API
    # ------------------------------------------------------------------
    def _run_v2_remote(self, img_bytes: bytes, token: str, model: str, page_num) -> dict:
        """Submit image to PaddleOCR v2 async job API and wait for result."""
        headers = {"Authorization": f"bearer {token}"}
        optional_payload = {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }
        data = {
            "model": model,
            "optionalPayload": json.dumps(optional_payload),
        }
        files = {"file": (f"page_{page_num}.jpg", img_bytes, "image/jpeg")}

        job_resp = requests.post(_V2_JOB_URL, headers=headers, data=data, files=files)
        if job_resp.status_code != 200:
            raise Exception(f"v2 job submit failed ({job_resp.status_code}): {job_resp.text}")

        job_id = job_resp.json()["data"]["jobId"]
        self.progress.emit(f"Page {page_num}: job {job_id} submitted, polling...")

        # Poll until done
        poll_headers = {"Authorization": f"bearer {token}"}
        jsonl_url = None
        while self._is_running:
            poll_resp = requests.get(f"{_V2_JOB_URL}/{job_id}", headers=poll_headers)
            if poll_resp.status_code != 200:
                raise Exception(f"v2 poll failed ({poll_resp.status_code}): {poll_resp.text}")

            poll_data = poll_resp.json()["data"]
            state = poll_data["state"]

            if state == "done":
                jsonl_url = poll_data["resultUrl"]["jsonUrl"]
                break
            elif state == "failed":
                raise Exception(f"v2 job failed: {poll_data.get('errorMsg', 'unknown')}")
            elif state == "running":
                try:
                    ep = poll_data["extractProgress"]
                    self.progress.emit(
                        f"Page {page_num}: running ({ep['extractedPages']}/{ep['totalPages']})..."
                    )
                except (KeyError, TypeError):
                    self.progress.emit(f"Page {page_num}: running...")
            else:
                self.progress.emit(f"Page {page_num}: {state}...")

            time.sleep(3)

        if not self._is_running:
            raise Exception("OCR job cancelled by user")

        # Download JSONL result
        jsonl_resp = requests.get(jsonl_url)
        jsonl_resp.raise_for_status()

        result = None
        for line in jsonl_resp.text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            result = parsed.get("result", parsed)
            break

        return result

    # ------------------------------------------------------------------
    def stop(self):
        self._is_running = False
        if self._executor:
            self._executor.shutdown(wait=False)


class ImageExportWorker(QThread):
    progress = pyqtSignal(str)
    progress_val = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(self, entries, project_config, fmt, export_dir, side, force_overwrite=False):
        super().__init__()
        self.entries = entries
        self.project_config = project_config
        self.fmt = fmt
        self.export_dir = export_dir
        self.side = side
        self.force_overwrite = force_overwrite

        self.is_running = True

        self.mapper = TextToBBoxMapper(project_config.get('ocr_json_path', 'ocr_results'), project_config.get('page_offset', 0))
        self.merger = BBoxMerger()
        self.stitcher = ImageStitcher()

    def run(self):
        try:
            self.progress.emit("Initializing export...")

            out_img_dir = os.path.join(self.export_dir, "output_slices")
            if not os.path.exists(out_img_dir):
                os.makedirs(out_img_dir)

            doc = None
            pdf_path = self.project_config.get('pdf_path', '')
            if pdf_path and os.path.exists(pdf_path):
                try:
                    doc = fitz.open(pdf_path)
                except:
                    pass

            total = len(self.entries)

            for i, entry in enumerate(self.entries):
                if not self.is_running:
                    break

                headword = entry['headword']
                safe_hw = re.sub(r'[\\/*?:"<>|]', '_', headword)
                safe_hw = safe_hw.strip()[:50]

                if i % 10 == 0 or i == total - 1:
                    self.progress.emit(f"Processing ({i+1}/{total}): {headword}...")
                    self.progress_val.emit(i + 1)

                bboxes = self.mapper.find_bboxes(entry['text'], entry['pages'])

                if bboxes:
                    merged = self.merger.merge(bboxes)
                    is_vertical = any(b['label'] == 'vertical_text' for b in bboxes)

                    pred_w, pred_h = self.stitcher.predict_size(merged, is_vertical)

                    if pred_w > 65500 or pred_h > 65500:
                        self.progress.emit(f"SKIPPED {headword}: Size {pred_w}x{pred_h} > 65500")
                        continue

                    page_num = entry['pages'][0] if entry['pages'] else 0
                    page_idx = entry.get('page_index', 0)
                    img_filename = f"{page_num}_{page_idx}.jpg"
                    save_path = os.path.join(out_img_dir, img_filename)

                    should_stitch = True
                    if not self.force_overwrite and os.path.exists(save_path):
                        try:
                            reader = QImageReader(save_path)
                            size = reader.size()
                            if size.isValid():
                                diff_w = abs(size.width() - pred_w)
                                diff_h = abs(size.height() - pred_h)
                                if diff_w < 5 and diff_h < 5:
                                    should_stitch = False
                        except Exception:
                            pass

                    if should_stitch:
                        stitched_img = self.stitcher.stitch(
                            merged, doc, self.project_config.get('image_dir'),
                            self.project_config.get('page_offset', 0), is_vertical
                        )
                        if stitched_img:
                            stitched_img.save(save_path)

                    entry['image_path'] = img_filename

            self.progress.emit("Saving index file...")

            project_name = self.project_config.get("name", "project")
            base_name = f"{project_name}_{self.side}.{self.fmt if self.fmt == 'json' else 'txt'}"
            final_path = os.path.join(self.export_dir, base_name)

            with open(final_path, 'w', encoding='utf-8') as f:
                if self.fmt == 'json':
                    json.dump(self.entries, f, ensure_ascii=False, indent=2)
                elif self.fmt == 'mdx':
                    for e in self.entries:
                        f.write(f"{e['headword']}\n")
                        f.write(f"{e['text']}\n".replace('\n', '<br>\n'))
                        if 'image_path' in e:
                            f.write(f'<img src="{e["image_path"]}" />\n')
                        f.write("</>\n")

            self.finished.emit(True, f"Export success! Saved to {final_path}")

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished.emit(False, str(e))
        finally:
            if doc:
                doc.close()

    def stop(self):
        self.is_running = False
