import os
import fitz
from PyQt6.QtGui import QImage, QPixmap

def get_page_image(doc, img_dir, real_page_num):
    """
    Get image bytes for a page.
    Priority:
    1. Extract from PDF (Raw or Rendered)
    2. Load from Image Directory (page_N.jpg)
    """
    if doc:
        try:
             if 0 < real_page_num <= len(doc):
                 page = doc[real_page_num - 1]
                 # Try Extract Raw? 
                 # Consistent logic: Render High DPI
                 pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0))
                 return pix.tobytes("png")
        except: pass

    if img_dir and os.path.exists(img_dir):
        names = [f"page_{real_page_num}", f"{real_page_num}"]
        exts = [".jpg", ".png", ".jpeg"]
        for n in names:
            for e in exts:
                p = os.path.join(img_dir, n + e)
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        return f.read()
    return None

def get_page_pixmap(doc, img_dir, page_num, offset=0):
    """
    Get QPixmap for UI display.
    """
    real_page_num = page_num + offset
    img_bytes = get_page_image(doc, img_dir, real_page_num)
    if img_bytes:
        img = QImage.fromData(img_bytes)
        return QPixmap.fromImage(img)
    return None
