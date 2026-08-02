import os
import re
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QListWidget, 
                             QPushButton, QFormLayout, QComboBox, 
                             QDialogButtonBox, QFileDialog, QMessageBox)

PAGE_PATTERN = re.compile(r"<(\d+)>")

def read_text_to_pages(file_path: str) -> dict[int, str]:
    pages = {}
    if not file_path or not os.path.exists(file_path):
        return pages
    lower = file_path.lower()
    if lower.endswith((".json", ".mdx", ".mdx.txt")):
        return pages
    try:
        with open(file_path, "r", encoding="utf-8-sig") as stream:
            source = stream.read()

        current_page = None
        current_content = []
        found_page_marker = False
        for line in source.splitlines():
            match = PAGE_PATTERN.fullmatch(line.strip())
            if match:
                found_page_marker = True
                if current_page is not None:
                    pages[current_page] = "\n".join(current_content)
                current_page = int(match.group(1))
                current_content = []
            elif current_page is not None:
                current_content.append(line)
        if current_page is not None:
            pages[current_page] = "\n".join(current_content)

        if found_page_marker:
            return pages

        return {1: source}
    except Exception as exc:
        print(f"Read error {file_path}: {exc}")
        return {}
def write_pages_to_file(pages: dict[int, str], file_path: str):
    try:
        existing_has_markers = False
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8-sig") as source:
                existing_has_markers = any(
                    PAGE_PATTERN.fullmatch(line.strip())
                    for line in source
                )
        single_document = set(pages) == {1} and not existing_has_markers
        with open(file_path, "w", encoding="utf-8", newline="\n") as stream:
            if single_document:
                stream.write(pages[1])
            else:
                for page in sorted(pages):
                    stream.write(f"<{page}>\n")
                    stream.write(pages[page])
                    if not pages[page].endswith("\n"):
                        stream.write("\n")
        print(f"Saved to {file_path}")
        return True
    except Exception as exc:
        print(f"Save error: {exc}")
        return False
class MergeTextDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("合并文本文件")
        self.resize(600, 300)
        self.files = []
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        h_list = QHBoxLayout()
        self.list_files = QListWidget()
        h_list.addWidget(self.list_files)
        
        v_btns = QVBoxLayout()
        btn_add = QPushButton("添加文件")
        btn_add.clicked.connect(self.add_files)
        btn_remove = QPushButton("移除选中")
        btn_remove.clicked.connect(self.remove_files)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.clear_files)
        v_btns.addWidget(btn_add)
        v_btns.addWidget(btn_remove)
        v_btns.addWidget(btn_clear)
        v_btns.addStretch()
        h_list.addLayout(v_btns)
        layout.addLayout(h_list)
        
        form = QFormLayout()
        self.combo_conflict = QComboBox()
        self.combo_conflict.addItems(["保留第一份 (Keep First)", "保留最后一份 (Keep Last)", "合并内容 (Merge All)"])
        form.addRow("重复页码策略:", self.combo_conflict)
        layout.addLayout(form)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.run_merge)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        
    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文本文件", "", "Text Files (*.txt)")
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.list_files.addItem(os.path.basename(p))
                
    def remove_files(self):
        for item in self.list_files.selectedItems():
            idx = self.list_files.row(item)
            self.list_files.takeItem(idx)
            del self.files[idx]
            
    def clear_files(self):
        self.list_files.clear()
        self.files.clear()
        
    def run_merge(self):
        if not self.files: return
        
        out_path, _ = QFileDialog.getSaveFileName(self, "保存合并文件", "", "Text Files (*.txt)")
        if not out_path: return
        
        strategy = self.combo_conflict.currentIndex()
        
        merged_pages = {}
        for fpath in self.files:
            pages = read_text_to_pages(fpath)
            for p_num, content in pages.items():
                if p_num in merged_pages:
                    if strategy == 0: continue
                    elif strategy == 1: merged_pages[p_num] = content
                    elif strategy == 2: merged_pages[p_num] += "\n\n" + content
                else:
                    merged_pages[p_num] = content
                    
        write_pages_to_file(merged_pages, out_path)
        QMessageBox.information(self, "Success", f"合并完成，共 {len(merged_pages)} 页。")
