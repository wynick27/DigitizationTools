import requests
import base64
from .base import BaseOCREngine

class RemoteAPIEngine(BaseOCREngine):
    def __init__(self, config=None):
        super().__init__(config)
        self.api_url = self.config.get("ocr_api_url", "")
        self.token = self.config.get("ocr_api_token", "")
        
    def process_image(self, image_bytes):
        if not self.api_url or not self.token:
            raise ValueError("Missing API URL or Token for Remote OCR")
            
        file_data = base64.b64encode(image_bytes).decode("ascii")
        headers = {
            "Authorization": f"token {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "file": file_data,
            "fileType": 1,
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }

        response = requests.post(self.api_url, json=payload, headers=headers)
        
        if response.status_code != 200:
            raise Exception(f"API Error {response.status_code}: {response.text}")

        # Return 'result' field as list
        return response.json().get("result", [])
