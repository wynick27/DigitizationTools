import os
import json
import base64
import requests
import re
import fitz
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImageReader

from ocr.ocr_utils import get_page_image, TextToBBoxMapper, BBoxMerger, ImageStitcher

# Try importing local OCR
HAS_LOCAL_OCR = False
try:
    from paddleocr import PaddleOCRVL
    HAS_LOCAL_OCR = True
    print("Local PaddleOCR detected.")
except ImportError:
    print("PaddleOCR not found. Local OCR disabled.")


class OCRWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str) # success, message
    
    def __init__(self, mode, page_list, project_config, global_config, engine="remote"):
        super().__init__()
        self.mode = mode # 'single' or 'batch'
        self.page_list = page_list # list of page numbers (int)
        self.project_config = project_config
        self.global_config = global_config
        self.engine = engine
        self.pdf_path = project_config.get('pdf_path')
        self._is_running = True

    def run(self):
        doc = None
        if self.pdf_path and os.path.exists(self.pdf_path):
            try:
                doc = fitz.open(self.pdf_path)
            except: pass
            
        api_url = self.global_config.get("ocr_api_url")
        token = self.global_config.get("ocr_api_token")
        
        save_dir = self.project_config.get("ocr_json_path", "ocr_results")
        if not os.path.exists(save_dir): 
            try: os.makedirs(save_dir)
            except: pass
            
        total = len(self.page_list)
        success_count = 0
        
        for i, page_num in enumerate(self.page_list):
            if not self._is_running: break
            
            try:
                self.progress.emit(f"Processing page {page_num} ({i+1}/{total})...")
                real_page_num = page_num + self.project_config.get("page_offset", 0)
                
                img_dir = self.project_config.get('image_dir')
                img_bytes = get_page_image(doc, img_dir, real_page_num)
                            
                if not img_bytes:
                    if self.mode == 'single':
                        raise Exception(f"No image found for page {page_num}")
                    else:
                        continue 
                
                result = None
                
                if self.engine == "local":
                    if not HAS_LOCAL_OCR:
                        raise Exception("Local OCR module not loaded.")
                    
                    temp_path = os.path.join(save_dir, f"temp_{real_page_num}.thumb")
                    with open(temp_path, "wb") as f:
                        f.write(img_bytes)
                        
                    try:
                        ocr = PaddleOCRVL() 
                        res = ocr.predict(temp_path)
                        result = res[0] if res else []
                    finally:
                         if os.path.exists(temp_path): os.remove(temp_path)
                         
                else:
                    file_data = base64.b64encode(img_bytes).decode("ascii")
                    headers = {
                        "Authorization": f"token {token}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "file": file_data,
                        "fileType": 1,
                        "useDocOrientationClassify": False,
                        "useDocUnwarping": False,
                        "useChartRecognition": False,
                    }
    
                    response = requests.post(api_url, json=payload, headers=headers)
                    
                    if response.status_code != 200:
                        if self.mode == 'single':
                             raise Exception(f"Remote Error: {response.text}")
                        else:
                             print(f"Page {page_num} Error: {response.text}")
                             continue
    
                    result = response.json().get("result")
                
                json_path = os.path.join(save_dir, f"page_{real_page_num}.json")
                with open(json_path, "w", encoding='utf8') as json_file:
                    json.dump(result, json_file, ensure_ascii=False, indent=2)
                    
                success_count += 1
                
            except Exception as e:
                print(f"OCR Error page {page_num}: {e}")
                if self.mode == 'single':
                    self.finished.emit(False, str(e))
                    return
                
        if doc: doc.close()
        self.finished.emit(True, f"Batch OCR Done. {success_count}/{total} processed.")

    def stop(self):
        self._is_running = False


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
                try: doc = fitz.open(pdf_path)
                except: pass
                
            total = len(self.entries)
            
            for i, entry in enumerate(self.entries):
                if not self.is_running: break
                
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
                        except Exception as e:
                             pass
                    
                    if should_stitch:
                        stitched_img = self.stitcher.stitch(merged, doc, self.project_config.get('image_dir'), 
                                                            self.project_config.get('page_offset', 0), is_vertical)
                        
                        if stitched_img:
                            stitched_img.save(save_path)
                    
                    rel_path = img_filename
                    entry['image_path'] = rel_path
                        
            self.progress.emit("Saving index file...")
            
            project_name = self.project_config.get("name", "project")
            base_name = f"{project_name}_{self.side}.{self.fmt if self.fmt=='json' else 'txt'}"
            final_path = os.path.join(self.export_dir, base_name)
            
            with open(final_path, 'w', encoding='utf-8') as f:
                if self.fmt == 'json':
                    json.dump(self.entries, f, ensure_ascii=False, indent=2)
                elif self.fmt == 'mdx':
                    for e in self.entries:
                        f.write(f"{e['headword']}\n")
                        f.write(f"{e['text']}\n".replace('\n','<br>\n'))
                        if 'image_path' in e:
                            f.write(f'<img src="{e["image_path"]}" />\n')
                        f.write("</>\n")
            
            self.finished.emit(True, f"Export success! Saved to {final_path}")
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished.emit(False, str(e))
        finally:
            if doc: doc.close()

    def stop(self):
        self.is_running = False
