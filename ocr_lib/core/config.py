import os
import json

DEFAULT_GLOBAL_CONFIG = {
    "ocr_api_url": "",
    "ocr_api_token": "",
    "ocr_engine": "remote", # remote, local, cli
    "ocr_cli_path": "",
    "ocr_cli_prompt": "",
    "find_history": [],
    "replace_history": []
}

DEFAULT_PROJECT_CONFIG = {
    "name": "Default Project",
    "pdf_path": "",
    "image_dir": "",
    "start_page": 1,
    "end_page": 1,
    "page_offset": 0,
    "text_path_left": "",
    "text_path_right": "", # Second version text
    "ocr_json_path": "ocr_results",       # OCR Data Dir
    "regex_left": r"^\*\*(.*?)\*\*",
    "regex_right": r"^([a-zA-Z]*?)",
    "regex_group_left": 0,
    "regex_group_right": 0,
    "use_pdf_render": False,
    # New settings for Ignore Tags / Diff
    "ignore_tags_in_diff": False,
}

class ConfigManager:
    def __init__(self, filepath="config.json"):
        self.filepath = filepath
        self.data = {
            "global": DEFAULT_GLOBAL_CONFIG.copy(),
            "projects": [DEFAULT_PROJECT_CONFIG.copy()],
            "active_project": "Default Project"
        }
        self.load()

    def load(self):
        if not os.path.exists(self.filepath):
            return

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                
            # Migration Logic: Check if it's flat (old style)
            if "projects" not in loaded:
                print("Migrating legacy config to new structure...")
                # It's a flat config, migrate to Default Project
                new_project = DEFAULT_PROJECT_CONFIG.copy()
                # Copy known project keys
                for key in new_project:
                    if key in loaded:
                        new_project[key] = loaded[key]
                new_project["name"] = "Default Project"
                
                # Copy known global keys
                if "ocr_api_url" in loaded:
                    self.data["global"]["ocr_api_url"] = loaded["ocr_api_url"]
                if "ocr_api_token" in loaded:
                    self.data["global"]["ocr_api_token"] = loaded["ocr_api_token"]
                    
                self.data["projects"] = [new_project]
            else:
                self.data = loaded
                # Ensure structure integrity
                if "global" not in self.data: 
                    self.data["global"] = DEFAULT_GLOBAL_CONFIG.copy()
                if "projects" not in self.data: 
                    self.data["projects"] = [DEFAULT_PROJECT_CONFIG.copy()]
                if "active_project" not in self.data:
                    self.data["active_project"] = self.data["projects"][0]["name"]
                    
        except Exception as e:
            print(f"Config load error: {e}")

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Config save error: {e}")

    def get_global(self):
        return self.data["global"]

    def get_projects(self):
        return self.data["projects"]

    def get_project(self, name):
        for p in self.data["projects"]:
            if p["name"] == name:
                return p
        return None

    def get_active_project(self):
        name = self.data.get("active_project")
        p = self.get_project(name)
        if p: return p
        # Fallback
        if self.data["projects"]:
            self.data["active_project"] = self.data["projects"][0]["name"]
            return self.data["projects"][0]
        return DEFAULT_PROJECT_CONFIG.copy()

    def set_active_project(self, name):
        if self.get_project(name):
            self.data["active_project"] = name
            self.save()

    def create_project(self, name):
        if self.get_project(name): return False
        new_p = DEFAULT_PROJECT_CONFIG.copy()
        new_p["name"] = name
        self.data["projects"].append(new_p)
        self.save()
        return True
        
    def delete_project(self, name):
        # Don't delete if it's the only one
        if len(self.data["projects"]) <= 1: return False
        
        self.data["projects"] = [p for p in self.data["projects"] if p["name"] != name]
        
        # Reset active if needed
        if self.data["active_project"] == name:
            self.data["active_project"] = self.data["projects"][0]["name"]
        
        self.save()
        return True
