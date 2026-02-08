import os
import json
import difflib
from PyQt6.QtGui import QImage, QPainter, QColor
from ocr_lib.core.image_utils import get_page_image

class TextToBBoxMapper:
    def __init__(self, ocr_json_dir, page_offset):
        self.ocr_json_dir = ocr_json_dir
        self.page_offset = page_offset
        self.cache = {} # page_num -> list of {text, bbox, label}

    def load_page_data(self, page_num):
        if page_num in self.cache: return self.cache[page_num]
        
        real_page_num = page_num + self.page_offset
        candidates = [f"page_{real_page_num}.json", f"{real_page_num}.json"]
        data = []
        
        f_path = None
        for n in candidates:
             p = os.path.join(self.ocr_json_dir, n)
             if os.path.exists(p):
                 f_path = p
                 break
        
        if f_path:
            try:
                with open(f_path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                    
                if isinstance(raw, list):
                    for item in raw:
                         if len(item) == 2:
                             pts = item[0]
                             xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                             bbox = [min(xs), min(ys), max(xs), max(ys)]
                             txt = item[1][0]
                             data.append({"text": txt, "bbox": bbox, "block_label": "text"})
                elif isinstance(raw, dict):
                     blocks = []
                     if "fullContent" in raw:
                         blocks = raw.get("fullContent", {}).get("prunedResult", {}).get("parsing_res_list", [])
                     if not blocks and "layoutParsingResults" in raw:
                         blocks = raw.get("layoutParsingResults", [{}])[0].get("prunedResult", {}).get("parsing_res_list", [])
                     
                     for b in blocks:
                         if b.get('block_label') in ['text','paragraph_title','vertical_text']:
                             data.append({
                                 "text": b.get('block_content'),
                                 "bbox": b.get('block_bbox'),
                                 "block_label": b.get('block_label')
                             })
            except: pass
            
        self.cache[page_num] = data
        return data

    def get_page_text_map(self, page_num):
        blocks = self.load_page_data(page_num)
        full_text = ""
        char_map = []
        
        for i, b in enumerate(blocks):
            txt = b['text']
            full_text += txt
            char_map.extend([i] * len(txt))
            
        return blocks, full_text, char_map

    def find_bboxes(self, entry_text, page_nums):
        result_boxes = [] 
        matched_block_indices = set()
        
        clean_entry = entry_text.strip()
        if not clean_entry: return []
        
        for page_num in page_nums:
            blocks, page_text, char_map = self.get_page_text_map(page_num)
            if not page_text: continue
            
            matcher = difflib.SequenceMatcher(None, clean_entry, page_text, autojunk=False)
            
            for i, j, n in matcher.get_matching_blocks():
                if n == 0: continue
                # Filter noise
                if n < 2:
                     chunk = clean_entry[i:i+n]
                     if len(chunk.encode('utf-8')) == len(chunk): continue
                
                affected = char_map[j : j+n]
                for block_idx in affected:
                    key = (page_num, block_idx)
                    if key not in matched_block_indices:
                        matched_block_indices.add(key)
                        b = blocks[block_idx]
                        result_boxes.append({
                            "bbox": b['bbox'],
                            "page": page_num,
                            "label": b.get('block_label', 'text'),
                            "sort_key": (page_num, block_idx)
                        })
                        
        result_boxes.sort(key=lambda x: x["sort_key"])
        return result_boxes

class BBoxMerger:
    def merge(self, boxes):
        if not boxes: return []
        
        by_page = {}
        for b in boxes:
            p = b['page']
            if p not in by_page: by_page[p] = []
            by_page[p].append(b)
            
        final_merged = []
        
        for p in sorted(by_page.keys()):
            page_boxes = by_page[p]
            if not page_boxes: continue
            
            is_vertical = any(b['label'] == 'vertical_text' for b in page_boxes)
            
            if is_vertical:
                merged = self._merge_vertical(page_boxes)
            else:
                merged = self._merge_horizontal(page_boxes)
            final_merged.extend(merged)
            
        return final_merged

    def _merge_horizontal(self, boxes):
        clean_boxes = []
        for b in boxes:
            bx = b['bbox']
            x, y, x2, y2 = bx[0], bx[1], bx[2], bx[3]
            clean_boxes.append({'x':x, 'y':y, 'w':x2-x, 'h':y2-y, 'r':x2, 'b':y2, 'page':b['page']})
            
        merged = []
        if not clean_boxes: return []
        
        curr = clean_boxes[0]
        
        for i in range(1, len(clean_boxes)):
            nex = clean_boxes[i]
            
            x_diff = abs(curr['x'] - nex['x'])
            y_gap = nex['y'] - curr['b']
            
            if x_diff < 50 and y_gap < 50:
                new_x = min(curr['x'], nex['x'])
                new_y = min(curr['y'], nex['y'])
                new_r = max(curr['r'], nex['r'])
                new_b = max(curr['b'], nex['b'])
                curr = {'x':new_x, 'y':new_y, 'w':new_r-new_x, 'h':new_b-new_y, 'r':new_r, 'b':new_b, 'page':curr['page']}
            else:
                merged.append(curr)
                curr = nex
        merged.append(curr)
        return merged

    def _merge_vertical(self, boxes):
        clean_boxes = []
        for b in boxes:
            bx = b['bbox']
            x, y, x2, y2 = bx[0], bx[1], bx[2], bx[3]
            clean_boxes.append({'x':x, 'y':y, 'w':x2-x, 'h':y2-y, 'r':x2, 'b':y2, 'page':b['page']})
            
        merged = []
        if not clean_boxes: return []
        
        curr = clean_boxes[0]
        
        for i in range(1, len(clean_boxes)):
            nex = clean_boxes[i]
            x_center_1 = curr['x'] + curr['w']/2
            x_center_2 = nex['x'] + nex['w']/2
            
            if abs(x_center_1 - x_center_2) < 20:
                 new_y = min(curr['y'], nex['y'])
                 new_b = max(curr['b'], nex['b'])
                 new_x = min(curr['x'], nex['x'])
                 new_r = max(curr['r'], nex['r'])
                 curr = {'x':new_x, 'y':new_y, 'w':new_r-new_x, 'h':new_b-new_y, 'r':new_r, 'b':new_b, 'page':curr['page']}
            else:
                 merged.append(curr)
                 curr = nex
                 
        merged.append(curr)
        return merged

class ImageStitcher:
    def predict_size(self, boxes, is_vertical_text):
        if not boxes: return 0, 0
        padding = 10
        if is_vertical_text:
            max_h = max(b['h'] for b in boxes)
            total_w = sum(b['w'] for b in boxes) + padding * (len(boxes)-1)
            return int(total_w + 20), int(max_h + 20)
        else:
            max_w = max(b['w'] for b in boxes)
            total_h = sum(b['h'] for b in boxes) + padding * (len(boxes)-1)
            return int(max_w + 20), int(total_h + 20)

    def stitch(self, boxes, doc, img_dir, page_offset, is_vertical_text):
        slices = []
        for b in boxes:
            page_num = b['page']
            real_p = page_num + page_offset
            
            img_qt = None
            
            try:
                img_bytes = get_page_image(doc, img_dir, real_p)
                
                if img_bytes:
                    full_img = QImage()
                    full_img.loadFromData(img_bytes)
                    
                    if not full_img.isNull():
                        x, y, w, h = int(b['x']), int(b['y']), int(b['w']), int(b['h'])
                        img_qt = full_img.copy(x, y, w, h)
                        
            except Exception as e:
                print(f"Stitch crop error: {e}")
                pass
            
            if img_qt and not img_qt.isNull():
                slices.append(img_qt)
            
        if not slices: return None
        
        padding = 10
        
        if is_vertical_text:
            # Stitch Right-to-Left (Horizontal Stack)
            max_h = max(s.height() for s in slices)
            total_w = sum(s.width() for s in slices) + padding * (len(slices)-1)
            
            final_img = QImage(total_w + 20, max_h + 20, QImage.Format.Format_RGB888)
            final_img.fill(QColor("white"))
            
            painter = QPainter(final_img)
            painter.translate(10, 10)
            
            # Draw from right to left? 
            # Usual vertical text flow is Right to Left columns.
            # But the 'boxes' are sorted by Page then Block.
            # BBoxMerger sorted them too.
            # If we just append them left-to-right, it might be reverse order?
            # User requirement: "Stitch into one picture".
            # Vertical text columns: 1st column is rightmost.
            # If the boxes are ordered 1, 2, 3 (Reading order), then 1 is Rightmost, 2 is Left of 1.
            # So we should draw:
            # Box 1 -> At X = (TotalW - w1).
            # Box 2 -> At X = (TotalW - w1 - padding - w2).
            # ...
            # Let's assume boxes are in Reading Order (Right to Left).
            
            current_x = total_w
            
            for s in slices:
                w = s.width()
                current_x -= w
                painter.drawImage(int(current_x), 0, s)
                current_x -= padding
                
            painter.end()
            return final_img
            
        else:
            # Stitch Top-to-Bottom (Vertical Stack)
            max_w = max(s.width() for s in slices)
            total_h = sum(s.height() for s in slices) + padding * (len(slices)-1)
            
            final_img = QImage(max_w + 20, total_h + 20, QImage.Format.Format_RGB888)
            final_img.fill(QColor("white"))
            
            painter = QPainter(final_img)
            painter.translate(10, 10)
            
            current_y = 0
            for s in slices:
                painter.drawImage(0, int(current_y), s)
                current_y += s.height() + padding
                
            painter.end()
            return final_img
