import os
import fitz
from PyQt6.QtWidgets import (QDialog, QFormLayout, QLineEdit, QPushButton, 
                             QHBoxLayout, QSpinBox, QDialogButtonBox, 
                             QFileDialog, QMessageBox, QComboBox)

class SplitPdfDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("拆分PDF")
        self.resize(500, 250)
        self.init_ui()
        
    def init_ui(self):
        layout = QFormLayout(self)
        
        self.txt_pdf = QLineEdit()
        btn_browse_pdf = QPushButton("...")
        btn_browse_pdf.setFixedWidth(30)
        btn_browse_pdf.clicked.connect(self.browse_pdf)
        h_pdf = QHBoxLayout()
        h_pdf.addWidget(self.txt_pdf)
        h_pdf.addWidget(btn_browse_pdf)
        layout.addRow("输入PDF:", h_pdf)
        
        self.txt_out = QLineEdit()
        btn_browse_out = QPushButton("...")
        btn_browse_out.setFixedWidth(30)
        btn_browse_out.clicked.connect(self.browse_out)
        h_out = QHBoxLayout()
        h_out.addWidget(self.txt_out)
        h_out.addWidget(btn_browse_out)
        layout.addRow("输出目录:", h_out)
        
        self.txt_ranges = QLineEdit()
        self.txt_ranges.setPlaceholderText("可选，例如: 1-10, 15, 20-30 (留空表示全部页面)")
        layout.addRow("页码范围:", self.txt_ranges)
        
        self.spin_split = QSpinBox()
        self.spin_split.setMinimum(1)
        self.spin_split.setMaximum(9999)
        self.spin_split.setValue(10)
        layout.addRow("每次拆分包含页数:", self.spin_split)
        
        self.txt_format = QLineEdit()
        self.txt_format.setText("{orig_name}_{part_num}")
        self.txt_format.setPlaceholderText("支持变量: {orig_name}, {part_num}, {part_start}, {part_end}")
        layout.addRow("输出文件名格式:", self.txt_format)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.run_split)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)
        
    def browse_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择PDF", "", "PDF (*.pdf)")
        if path: 
            self.txt_pdf.setText(path)
            if not self.txt_out.text():
                self.txt_out.setText(os.path.dirname(path))
            
    def browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d: self.txt_out.setText(d)
        
    def run_split(self):
        pdf_path = self.txt_pdf.text()
        out_dir = self.txt_out.text()
        if not os.path.exists(pdf_path): 
            return QMessageBox.warning(self, "Error", "PDF 文件不存在")
        if not os.path.isdir(out_dir):
            return QMessageBox.warning(self, "Error", "输出目录无效")
            
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"无法打开PDF: {e}")
            return
            
        ranges_text = self.txt_ranges.text().strip()
        chunk_size = self.spin_split.value()
        fmt = self.txt_format.text().strip()
        if not fmt: fmt = "{orig_name}_{part_num}"
        
        pages_to_extract = set()
        if ranges_text:
            for part in ranges_text.split(','):
                part = part.strip()
                if not part: continue
                if '-' in part:
                    try:
                        s, e = map(int, part.split('-'))
                        pages_to_extract.update(range(s, e+1))
                    except: pass
                else:
                    try: pages_to_extract.add(int(part))
                    except: pass
        else:
            pages_to_extract = set(range(1, len(doc) + 1))
            
        if not pages_to_extract:
            doc.close()
            QMessageBox.warning(self, "Error", "页码范围无效或为空")
            return
            
        pages = sorted(list(pages_to_extract))
        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
        
        chunks = [pages[i:i + chunk_size] for i in range(0, len(pages), chunk_size)]
        for i, chunk in enumerate(chunks):
            new_doc = fitz.open()
            for p_num in chunk:
                if 1 <= p_num <= len(doc):
                    new_doc.insert_pdf(doc, from_page=p_num-1, to_page=p_num-1)
            if len(new_doc) > 0:
                out_name = fmt.replace("{orig_name}", base_name) \
                              .replace("{part_num}", str(i + 1)) \
                              .replace("{part_start}", str(chunk[0])) \
                              .replace("{part_end}", str(chunk[-1]))
                out_path = os.path.join(out_dir, f"{out_name}.pdf")
                new_doc.save(out_path)
            new_doc.close()
            
        doc.close()
        QMessageBox.information(self, "Success", f"拆分完成，生成了 {len(chunks)} 个文件。")
        self.accept()


class ExportPdfImageDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出PDF图片")
        self.resize(500, 200)
        self.init_ui()
        
    def init_ui(self):
        layout = QFormLayout(self)
        
        self.txt_pdf = QLineEdit()
        btn_browse_pdf = QPushButton("...")
        btn_browse_pdf.setFixedWidth(30)
        btn_browse_pdf.clicked.connect(self.browse_pdf)
        h_pdf = QHBoxLayout()
        h_pdf.addWidget(self.txt_pdf)
        h_pdf.addWidget(btn_browse_pdf)
        layout.addRow("输入PDF:", h_pdf)
        
        self.txt_ranges = QLineEdit()
        self.txt_ranges.setPlaceholderText("例如: 1-10, 15, 20-30")
        layout.addRow("页码范围:", self.txt_ranges)
        
        self.txt_out = QLineEdit()
        btn_browse_out = QPushButton("...")
        btn_browse_out.setFixedWidth(30)
        btn_browse_out.clicked.connect(self.browse_out)
        h_out = QHBoxLayout()
        h_out.addWidget(self.txt_out)
        h_out.addWidget(btn_browse_out)
        layout.addRow("输出目录:", h_out)
        
        self.combo_ext = QComboBox()
        self.combo_ext.addItems([".jpg", ".png"])
        layout.addRow("图片格式:", self.combo_ext)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.run_export)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)
        
    def browse_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择PDF", "", "PDF (*.pdf)")
        if path: 
            self.txt_pdf.setText(path)
            if not self.txt_out.text():
                self.txt_out.setText(os.path.dirname(path))
        
    def browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d: self.txt_out.setText(d)
        
    def run_export(self):
        pdf_path = self.txt_pdf.text()
        out_dir = self.txt_out.text()
        if not os.path.exists(pdf_path) or not os.path.exists(out_dir):
            return QMessageBox.warning(self, "Error", "路径无效")
            
        ranges_text = self.txt_ranges.text()
        ext = self.combo_ext.currentText()
        
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            return QMessageBox.critical(self, "Error", str(e))
            
        pages_to_extract = set()
        if ranges_text:
            for part in ranges_text.split(','):
                part = part.strip()
                if not part: continue
                if '-' in part:
                    try:
                        s, e = map(int, part.split('-'))
                        pages_to_extract.update(range(s, e+1))
                    except: pass
                else:
                    try: pages_to_extract.add(int(part))
                    except: pass
        else:
            pages_to_extract = set(range(1, len(doc) + 1))
                
        if not pages_to_extract:
            doc.close()
            return QMessageBox.warning(self, "Error", "页码范围无效")
            
        pages = sorted(list(pages_to_extract))
            
        count = 0
        for p_num in pages:
            if 1 <= p_num <= len(doc):
                page = doc[p_num-1]
                pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5))
                out_path = os.path.join(out_dir, f"page_{p_num}{ext}")
                pix.save(out_path)
                count += 1
                
        doc.close()
        QMessageBox.information(self, "Success", f"导出完成，共 {count} 张图片。")
        self.accept()
