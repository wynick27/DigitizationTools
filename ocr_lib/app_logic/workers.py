import os
import json
import difflib
from PyQt6.QtCore import QThread, pyqtSignal

from ocr_lib.ocr_engines import get_engine
from ocr_lib.core.image_utils import get_page_image
# I should extract get_page_image to ocr_lib/core/image_utils.py first? 
# Yes. But for now I'll duplicate or import if possible.
# Actually I should have extracted get_page_image.

class OCRWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str) # success, message
    
    def __init__(self, mode, page_list, project_config, global_config, engine_type="remote"):
        super().__init__()
        self.mode = mode # 'single' or 'batch'
        self.page_list = page_list # list of page numbers (int)
        self.project_config = project_config
        self.global_config = global_config
        self.engine_type = engine_type
        self.pdf_path = project_config.get('pdf_path')
        self._is_running = True

    def run(self):
        import fitz # PyMuPDF
        
        doc = None
        if self.pdf_path and os.path.exists(self.pdf_path):
            try:
                doc = fitz.open(self.pdf_path)
            except: pass
            
        save_dir = self.project_config.get("ocr_json_path", "ocr_results")
        if not os.path.exists(save_dir): 
            try: os.makedirs(save_dir)
            except: pass
            
        # Initialize Engine
        try:
            engine = get_engine(self.engine_type, self.global_config)
        except Exception as e:
            self.finished.emit(False, str(e))
            return
            
        total = len(self.page_list)
        success_count = 0
        
        for i, page_num in enumerate(self.page_list):
            if not self._is_running: break
            
            try:
                self.progress.emit(f"Processing page {page_num} ({i+1}/{total})...")
                real_page_num = page_num + self.project_config.get("page_offset", 0)
                
                # Get Image
                img_dir = self.project_config.get('image_dir')
                img_bytes = get_page_image(doc, img_dir, real_page_num)
                            
                if not img_bytes:
                    if self.mode == 'single':
                        raise Exception(f"No image found for page {page_num}")
                    else:
                        continue 
                
                # Process
                result = engine.process_image(img_bytes)
                
                # Save
                json_path = os.path.join(save_dir, f"page_{real_page_num}.json")
                with open(json_path, "w", encoding='utf8') as json_file:
                    json.dump(result, json_file, ensure_ascii=False, indent=2)
                    
                success_count += 1
                
            except Exception as e:
                print(f"OCR Error page {page_num}: {e}")
                if self.mode == 'single':
                    self.finished.emit(False, str(e))
                    if doc: doc.close()
                    return
                # Batch continues
                
        if doc: doc.close()
        self.finished.emit(True, f"Batch OCR Done. {success_count}/{total} processed.")

    def stop(self):
        self._is_running = False

from ocr_lib.core.text_utils import TextStripper

class DiffWorker(QThread):
    result_ready = pyqtSignal(list, list, list) # opcodes, ocr_opcodes, stripped_opcodes

    def __init__(self, text_l, text_r, ocr_text_full, need_ocr_map, ignore_tags=False, mode_l="plain", mode_r="plain"):
        super().__init__()
        self.text_l = text_l
        self.text_r = text_r
        self.ocr_text_full = ocr_text_full
        self.need_ocr_map = need_ocr_map
        self.ignore_tags = ignore_tags
        self.mode_l = mode_l
        self.mode_r = mode_r
        
        # Instantiate strippers based on modes
        self.stripper_l = TextStripper(mode=mode_l)
        self.stripper_r = TextStripper(mode=mode_r)
    
    def run(self):
        # Main Diff
        if self.ignore_tags:
            # Strip using mode-specific strippers
            s_l, m_l, _ = self.stripper_l.strip(self.text_l)
            s_r, m_r, _ = self.stripper_r.strip(self.text_r)
            
            # Diff Stripped
            matcher = difflib.SequenceMatcher(None, s_l, s_r, autojunk=False)
            raw_opcodes = matcher.get_opcodes()
            
            # Map back
            # Note: map_opcodes is a utility in TextStripper class, but instance method?
            # It usually doesn't depend on instance state. It uses the maps passed in.
            # We can use either instance.
            opcodes = self.stripper_l.map_opcodes(raw_opcodes, m_l, m_r)
        else:
            matcher = difflib.SequenceMatcher(None, self.text_l, self.text_r, autojunk=False)
            opcodes = matcher.get_opcodes()
        
        # OCR Mapping Diff
        ocr_opcodes = []
        if self.need_ocr_map and self.ocr_text_full:
             m2 = difflib.SequenceMatcher(None, self.text_l, self.ocr_text_full, autojunk=False)
             ocr_opcodes = m2.get_opcodes()
             
        # Emit: (opcodes_for_source, ocr_opcodes, opcodes_for_stripped)
        # We need to change signature or just valid raw_opcodes if ignore_tags.
        # If not ignore_tags, raw_opcodes == opcodes.
        if not self.ignore_tags:
            raw_opcodes = opcodes
            
        self.result_ready.emit(opcodes, ocr_opcodes, raw_opcodes)
