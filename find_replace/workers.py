import difflib
import hashlib
import threading
from collections import OrderedDict

from PyQt6.QtCore import Qt, QThread, pyqtSignal


def expand_custom_diff_format(format_text, old_segment, new_segment, old_match=None, new_match=None):
    r"""Expand custom diff replacement tokens.

    \1 and \2 mean the whole target/source diff segments. \1.N and \2.N
    reference capture group N from the old/new filter regex match. \\ emits a
    literal backslash.
    """
    result = []
    i = 0
    while i < len(format_text):
        ch = format_text[i]
        if ch != "\\":
            result.append(ch)
            i += 1
            continue

        if i + 1 >= len(format_text):
            result.append("\\")
            i += 1
            continue

        marker = format_text[i + 1]
        if marker == "\\":
            result.append("\\")
            i += 2
            continue

        if marker not in ("1", "2"):
            result.append("\\")
            result.append(marker)
            i += 2
            continue

        segment = old_segment if marker == "1" else new_segment
        match = old_match if marker == "1" else new_match
        i += 2

        if i < len(format_text) and format_text[i] == ".":
            j = i + 1
            while j < len(format_text) and format_text[j].isdigit():
                j += 1
            if j > i + 1:
                group_num = int(format_text[i + 1:j])
                try:
                    group_value = match.group(group_num) if match else ""
                except IndexError:
                    group_value = ""
                result.append(group_value or "")
                i = j
                continue

        result.append(segment)

    return "".join(result)


class ReviewDiffWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(list, str) # items, msg

    _opcode_cache = OrderedDict()
    _opcode_cache_lock = threading.Lock()
    _opcode_cache_limit = 256
    _opcode_cache_max_limit = 20000
    
    def __init__(self, pages, pages_left, pages_right, target_is_left, 
                 regex_old, regex_new, regex_scope,
                 check_insert, check_delete, check_replace,
                 custom_replace_format=None, scope_exclude=False):
        super().__init__()
        self.pages = pages
        self.pages_left = pages_left
        self.pages_right = pages_right
        self.target_is_left = target_is_left
        self.regex_old = regex_old
        self.regex_new = regex_new
        self.regex_scope = regex_scope
        self.scope_exclude = scope_exclude
        self.chk_insert = check_insert
        self.chk_delete = check_delete
        self.chk_replace = check_replace
        self.custom_replace_format = custom_replace_format

        # Global reviews commonly span thousands of pages. Keep enough entries
        # for both target directions so a sequential rerun does not thrash a
        # small LRU cache before reaching the pages cached most recently.
        self._ensure_cache_capacity(len(pages))
        
        self.is_running = True
        self.items = []
        self.cache_hits = 0

    @staticmethod
    def _text_cache_digest(text):
        return hashlib.blake2b(
            text.encode("utf-8", errors="surrogatepass"), digest_size=16
        ).digest()

    @classmethod
    def clear_diff_cache(cls):
        with cls._opcode_cache_lock:
            cls._opcode_cache.clear()

    @classmethod
    def _ensure_cache_capacity(cls, page_count):
        desired = max(256, page_count * 2)
        with cls._opcode_cache_lock:
            cls._opcode_cache_limit = max(
                cls._opcode_cache_limit,
                min(desired, cls._opcode_cache_max_limit),
            )

    @classmethod
    def _get_opcodes(cls, text_a, text_b):
        key = (
            len(text_a), cls._text_cache_digest(text_a),
            len(text_b), cls._text_cache_digest(text_b),
        )
        with cls._opcode_cache_lock:
            cached = cls._opcode_cache.get(key)
            if cached is not None:
                cls._opcode_cache.move_to_end(key)
                return cached, True

        opcodes = tuple(
            difflib.SequenceMatcher(None, text_a, text_b, autojunk=False).get_opcodes()
        )
        with cls._opcode_cache_lock:
            cls._opcode_cache[key] = opcodes
            cls._opcode_cache.move_to_end(key)
            while len(cls._opcode_cache) > cls._opcode_cache_limit:
                cls._opcode_cache.popitem(last=False)
        return opcodes, False
        
    def run(self):
        try:
            total = len(self.pages)
            for i, p in enumerate(self.pages):
                if self.isInterruptionRequested(): break
                
                self.progress.emit(i + 1)
                
                # Get Texts
                t_l = self.pages_left.get(p, "")
                t_r = self.pages_right.get(p, "")
                
                text_a = ""
                text_b = ""
                if self.target_is_left:
                     # Target=Left. Turn Left into Right.
                     text_a = t_l
                     text_b = t_r
                else:
                     # Target=Right. Turn Right into Left.
                     text_a = t_r
                     text_b = t_l
                
                self._generate_diff_items(p, text_a, text_b)
                
            self.finished.emit(self.items, "Done")
            
        except Exception as e:
            self.finished.emit([], str(e))
            
    def _generate_diff_items(self, page_num, text_a, text_b):
        opcodes, cache_hit = self._get_opcodes(text_a, text_b)
        if cache_hit:
            self.cache_hits += 1
        
        # Pre-compute valid scope spans if regex_scope is defined
        scope_spans = []
        if self.regex_scope:
            scope_spans = [m.span() for m in self.regex_scope.finditer(text_a)]
        
        import html
        
        for tag, i1, i2, j1, j2 in opcodes:
             if self.isInterruptionRequested(): break
             if tag == 'equal': continue
            
             # Check Types
             if tag == 'replace' and not self.chk_replace: continue
             if tag == 'delete' and not self.chk_delete: continue
             if tag == 'insert' and not self.chk_insert: continue
            
             old_segment = text_a[i1:i2]
             new_segment = text_b[j1:j2]
            
             old_match = None
             new_match = None

             # Regex Filters
             if self.regex_old and old_segment:
                 old_match = self.regex_old.search(old_segment)
                 if not old_match: continue
             if self.regex_old and not old_segment: continue
            
             if self.regex_new and new_segment:
                 new_match = self.regex_new.search(new_segment)
                 if not new_match: continue
             if self.regex_new and not new_segment: continue
            
             # Check Scope
             if self.regex_scope:
                 # Check if the diff overlap with any scope match
                 if i1 == i2:  # Insert: use its insertion point.
                     in_scope = any(start <= i1 < end for start, end in scope_spans)
                 else:
                     in_scope = any(i1 < end and start < i2 for start, end in scope_spans)
                 if self.scope_exclude and in_scope:
                     continue
                 if not self.scope_exclude and not in_scope:
                     continue
            
             # Context
             c_start = max(0, i1 - 10)
             c_end = min(len(text_a), i2 + 10)
             prefix = html.escape(text_a[c_start:i1])
             suffix = html.escape(text_a[i2:c_end])
             seg_old_esc = html.escape(old_segment)
             replacement_segment = new_segment
             if self.custom_replace_format is not None:
                 replacement_segment = expand_custom_diff_format(
                     self.custom_replace_format,
                     old_segment,
                     new_segment,
                     old_match,
                     new_match,
                 )

                 # A custom format can turn a source diff into a no-op. Such an
                 # item should not be offered for review or application.
                 if replacement_segment == old_segment:
                     continue

             seg_new_esc = html.escape(replacement_segment)
             
             style_del = "background-color:#ffcccc; text-decoration:line-through;"
             style_ins = "background-color:#ccffcc;"
            
             diff_html = ""
             if tag == 'replace':
                  diff_html = f"{prefix}<span style='{style_del}'>{seg_old_esc}</span> <span style='{style_ins}'>{seg_new_esc}</span>{suffix}"
             elif tag == 'delete':
                  diff_html = f"{prefix}<span style='{style_del}'>{seg_old_esc}</span>{suffix}"
             elif tag == 'insert':
                  diff_html = f"{prefix}<span style='{style_ins}'>{seg_new_esc}</span>{suffix}"
            
             # Line/Col Calculation
             line = text_a.count('\n', 0, i1) + 1
             last_nl = text_a.rfind('\n', 0, i1)
             if last_nl == -1: col = i1 + 1
             else: col = i1 - last_nl

             item_data = {
                'page_num': page_num,
                'line': line,
                'col': col,
                'span': (i1, i2), 
                'original': old_segment,
                'new': replacement_segment,
                'context_html': diff_html,
                'checked': True
             }
             self.items.append(item_data)

