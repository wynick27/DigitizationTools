import os
import json
import re
from PyQt6.QtWidgets import QMessageBox, QFileDialog, QProgressDialog
from PyQt6.QtCore import Qt

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
        
        full_text = ""
        for p in range(start, end + 1):
             data = self.mw.load_ocr_json(p)
             if data:
                 t = self._extract_text_from_ocr_data(data)
                 full_text += f"<{p}>\n{t}\n"
                 
        proj_name = self.mw.project_config.get("name", "project")
        filename, _ = QFileDialog.getSaveFileName(self.mw, "Export All OCR", f"{proj_name}_paddleocr.txt", "Text Files (*.txt)")
        if filename:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(full_text)
            QMessageBox.information(self.mw, "Done", f"Exported all OCR text to {filename}.")

    def export_all_markdown(self, with_images=False):
        ocr_dir = self.mw.project_config.get("ocr_json_path")
        if not ocr_dir or not os.path.exists(ocr_dir):
            QMessageBox.warning(self.mw, "Error", "OCR JSON path is invalid. Please check settings.")
            return
            
        proj_name = self.mw.project_config.get("name", "project")
        default_name = f"{proj_name}_paddleocr.md"
        out_md_path, _ = QFileDialog.getSaveFileName(self.mw, "Save Markdown", default_name, "Markdown (*.md)")
        if not out_md_path: return

        md_dir = os.path.dirname(out_md_path)
            
        start_p = self.mw.project_config.get("start_page", 1)
        end_p = self.mw.project_config.get("end_page", 1)
        
        md_texts = []
        image_tasks = []
        
        for p in range(start_p, end_p + 1):
            json_file = os.path.join(ocr_dir, f"page_{p}.json")
            if os.path.exists(json_file):
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    res = data.get("layoutParsingResults", [])
                    if res:
                        md_data = res[0].get("markdown", {})
                        md_text = md_data.get("text", "")
                        images_dict = md_data.get("images", {})
                        
                        if with_images and images_dict:
                            for img_key, img_url in images_dict.items():
                                if not img_url.startswith("http"): continue
                                img_filename = f"page_{p}_{os.path.basename(img_key)}"
                                local_rel_path = f"imgs/{img_filename}"
                                md_text = md_text.replace(f'src="{img_key}"', f'src="{local_rel_path}"')
                                md_text = md_text.replace(f"]({img_key})", f"]({local_rel_path})")
                                img_save_path = os.path.join(md_dir, "imgs", img_filename)
                                image_tasks.append((img_url, img_save_path))
                        
                        if md_text:
                            md_texts.append(md_text)
                except Exception as e:
                    print(f"Markdown parse error page {p}: {e}")

        if not md_texts:
            QMessageBox.warning(self.mw, "Warning", "No markdown content found in OCR results.")
            return

        try:
            with open(out_md_path, "w", encoding='utf-8') as f:
                f.write("\n\n---\n\n".join(md_texts))
        except Exception as e:
            QMessageBox.critical(self.mw, "Error", f"Failed to save Markdown: {e}")
            return
            
        if not with_images or not image_tasks:
            QMessageBox.information(self.mw, "Success", f"Exported Markdown to {out_md_path}")
            return
            
        from ocr.ocr_worker import MarkdownImageDownloader
        self.mw.md_img_worker = MarkdownImageDownloader(image_tasks)
        
        self.mw.progress_dlg = QProgressDialog("Downloading Images...", "Cancel", 0, len(image_tasks), self.mw)
        self.mw.progress_dlg.setWindowTitle("Exporting Images")
        self.mw.progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.mw.progress_dlg.setMinimumDuration(0)
        self.mw.progress_dlg.show()
        
        self.mw.md_img_worker.progress_val.connect(self.mw.progress_dlg.setValue)
        self.mw.md_img_worker.progress_text.connect(self.mw.progress_dlg.setLabelText)
        self.mw.md_img_worker.finished.connect(lambda s, c: self.on_md_img_finished(s, c, out_md_path))
        self.mw.progress_dlg.canceled.connect(self.mw.md_img_worker.stop)
        
        self.mw.md_img_worker.start()

    def on_md_img_finished(self, success, count, out_md_path):
        if hasattr(self.mw, 'progress_dlg'):
            self.mw.progress_dlg.close()
        if count > 0:
            msg = f"Exported Markdown and {count} images to {os.path.dirname(out_md_path)}"
            if success:
                QMessageBox.information(self.mw, "Success", msg)
            else:
                QMessageBox.warning(self.mw, "Incomplete", msg + "\n(Some images may have failed/cancelled)")
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
            self.mw.img_export_worker = ImageExportWorker(entries, self.mw.project_config, fmt, export_dir, side, force_overwrite)
            
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
