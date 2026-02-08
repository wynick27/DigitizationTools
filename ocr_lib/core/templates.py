import json
import os

class TemplateManager:
    def __init__(self, filename="replace_templates.json"):
        self.filename = filename
        self.templates = {} # {name: [rules]}
        self.load()
        
    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    self.templates = json.load(f)
            except: self.templates = {}
        else:
            self.templates = {}
            
    def save(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.templates, f, ensure_ascii=False, indent=2)
        except: pass
        
    def get_template_names(self):
        return sorted(list(self.templates.keys()))
        
    def get_rules(self, name):
        return self.templates.get(name, [])
        
    def set_template(self, name, rules):
        self.templates[name] = rules
        self.save()

    def delete_template(self, name):
        if name in self.templates:
            del self.templates[name]
            self.save()
