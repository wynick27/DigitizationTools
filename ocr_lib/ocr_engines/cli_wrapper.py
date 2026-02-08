import subprocess
import os
import tempfile
import json
from .base import BaseOCREngine

class CLIEngine(BaseOCREngine):
    def __init__(self, config=None):
        super().__init__(config)
        self.cli_path = self.config.get("ocr_cli_path", "")
        self.cli_prompt = self.config.get("ocr_cli_prompt", "")
        
    def process_image(self, image_bytes):
        if not self.cli_path:
            raise ValueError("CLI Path not configured")
            
        # Example CLI implementation (placeholder)
        # Usage: cli_exe <image_path> <prompt> -> stdout JSON
        
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
            temp.write(image_bytes)
            temp_path = temp.name
            
        try:
            # Construct command
            # This is generic, user might need to adjust formatting
            cmd = [self.cli_path, temp_path]
            if self.cli_prompt:
                cmd.append(self.cli_prompt)
                
            # Run
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            output = result.stdout
            
            # Parse output (Assume JSON)
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                # Fallback: treat as raw text?
                # For consistency, return a simple block
                return [{"text": output, "bbox": [0,0,0,0], "block_label": "text"}]
                
        except subprocess.CalledProcessError as e:
            raise Exception(f"CLI Error: {e.stderr}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
