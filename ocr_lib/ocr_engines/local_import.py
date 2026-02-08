import os
import tempfile
from .base import BaseOCREngine

# Try importing PaddleOCR
try:
    from paddleocr import PaddleOCRVL
    HAS_PADDLE = True
except ImportError:
    HAS_PADDLE = False

class LocalImportEngine(BaseOCREngine):
    def __init__(self, config=None):
        super().__init__(config)
        if not HAS_PADDLE:
            raise ImportError("PaddleOCR module not found. Please install paddleocr.")
        
        # Initialize PaddleOCR here? Or lazy load?
        # Initialization might be heavy. Lazy load might be better if not used immediately.
        # But for Worker, we assume it's ready.
        self.ocr = None

    def initialize(self):
        if not self.ocr:
            self.ocr = PaddleOCRVL()

    def process_image(self, image_bytes):
        self.initialize()
        
        # PaddleOCRVL typically expects a file path
        # Write bytes to temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
            temp.write(image_bytes)
            temp_path = temp.name
            
        try:
            res = self.ocr.predict(temp_path)
            # res structure from PaddleOCRVL: [results] or similar
            # Based on OCRWorker: res = ocr.predict(path); result = res[0]
            # We return standard list.
            if res:
                return res[0]
            return []
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
