import re

class TextStripper:
    """
    Helper class to strip markup (like markdown tags) from text 
    while maintaining a mapping back to the original text indices.
    """
    PATTERNS = {
        "html": r"<[^>]+>",
        "markdown": r"(\*\*|__)|(\*|_)|(`+)|(^#+\s)|(!?\[)|(\]\(.*?\))",
        "plain": None
    }

    def __init__(self, mode="plain", regex=None):
        if regex:
            self.regex = re.compile(regex) if isinstance(regex, str) else regex
        elif mode in self.PATTERNS and self.PATTERNS[mode]:
            self.regex = re.compile(self.PATTERNS[mode])
        else:
            self.regex = None

    def strip(self, text):
        """
        Strips the text using the regex pattern.
        Returns:
            clean_text (str): The text with matches removed.
            mapping (list): A list where mapping[i] is the index in original 
                            text corresponding to index i in clean_text.
            styles (list): A list of (start, length, style_type) tuples.
        """
        clean_text = ""
        mapping = []
        styles = [] # (start, length, type)
        
        if not self.regex:
            # Plain mode / No-op
            clean_text = text
            mapping = list(range(len(text) + 1))
            return clean_text, mapping, styles
            
        last_pos = 0
        active_styles = set() # {'bold', 'italic', 'header'}
        
        # Helper to determine tag type from match
        def interpret_match(match, mode):
            # Returns (action, style_type)
            # action: 'toggle', 'add', 'remove', 'none'
            text = match.group(0)
            
            if mode == 'html':
                tag_match = re.match(r"</?([a-zA-Z0-9]+)", text)
                if tag_match:
                    tag = tag_match.group(1).lower()
                    is_close = text.startswith("</")
                    
                    style_map = {'b': 'bold', 'strong': 'bold', 'i': 'italic', 'em': 'italic', 'h1': 'header_1', 'h2': 'header_2', 'h3': 'header_3'}
                    if tag in style_map:
                        return ('remove' if is_close else 'add', style_map[tag])
            
            elif mode == 'markdown':
                # Groups: 1=Bold, 2=Italic, 3=Link, 4=Image, 5=Code, 6=Header
                if match.group(1): return ('toggle', 'bold')
                if match.group(2): return ('toggle', 'italic')
                if match.group(6): return ('add', 'header_1') # Header usually applies to line, but strict toggle difficult
                # Link/Image/Code -> Just strip, no formatting for now?
                
            return ('none', None)

        mode = 'plain'
        if self.regex.pattern == self.PATTERNS['html']: mode = 'html'
        elif self.regex.pattern == self.PATTERNS['markdown']: mode = 'markdown'

        for match in self.regex.finditer(text):
            start, end = match.span()
            
            # Map content before match
            chunk = text[last_pos:start]
            if chunk:
                # Add content
                current_start = len(clean_text)
                clean_text += chunk
                
                # Update Mapping
                for i in range(len(chunk)):
                    mapping.append(last_pos + i)
                    
                # Apply Active Styles
                for style in active_styles:
                    styles.append((current_start, len(chunk), style))

            # Process Tag (Update Active Styles)
            action, style_type = interpret_match(match, mode)
            if style_type:
                if action == 'toggle':
                    if style_type in active_styles: active_styles.remove(style_type)
                    else: active_styles.add(style_type)
                elif action == 'add':
                    active_styles.add(style_type)
                elif action == 'remove':
                    if style_type in active_styles: active_styles.remove(style_type)
            
            # Handle special case: Markdown Header usually ends at newline?
            # Our regex `^#+\s` matches the start. We need to clear header at newline.
            # But the newline is CONTENT.
            # Complexity: Markdown headers are line-based.
            # Workaround: If we added 'header', we keep it until... well, forever?
            # Ideally we strip newlines? No, we keep structure.
            # Let's ignore complex header closing for now, or assume it toggles?
            # Markdown headers don't have closing tag usually.
            # Simpler: Don't format entire headers, just bold/italic.
            
            last_pos = end
            
        # Map remaining
        chunk = text[last_pos:]
        if chunk:
            current_start = len(clean_text)
            clean_text += chunk
            for i in range(len(chunk)):
                mapping.append(last_pos + i)
            for style in active_styles:
                styles.append((current_start, len(chunk), style))
                
        # Add end sentinel
        mapping.append(len(text))
        
        return clean_text, mapping, styles

    def apply_stripped_diff(self, original_text, new_stripped_text, mapping=None):
        """
        Apply changes made to stripped text back to the original text.
        Strategy: Use difflib to find changes between `strip(original)` and `new_stripped`.
        Map these changes back to original indices.
        
        CRITICAL: This method is "Gap-Aware". It assumes that any characters in `original_text`
        that were skipped by the stripper (gaps) should be PRESERVED, even if the surrounding
        content is deleted or replaced. This ensures markup tags (like ** or <b>) survive edits.
        """
        if mapping is None:
            old_stripped, generated_mapping, _ = self.strip(original_text)
            mapping = generated_mapping
        else:
            # If mapping provided, we might still need old_stripped. 
            # Ideally caller provides both or we re-strip. 
            # Re-strip is safer to ensure old_stripped matches mapping unless optimization.
            # Let's assume re-strip is cheap enough or mapping comes with it?
            # Actually, apply_stripped_diff usually called after we have stripped text.
            # But here it calculates diff between old_stripped and new_stripped.
            # So we DO need old_stripped.
            old_stripped, _, _ = self.strip(original_text)
            
        import difflib
        matcher = difflib.SequenceMatcher(None, old_stripped, new_stripped_text, autojunk=False)
        opcodes = matcher.get_opcodes()
        
        new_original = ""
        last_orig_pos = 0
        
        for tag, i1, i2, j1, j2 in opcodes:
            # i1 is start index in old_stripped
            # Start of this block in original is mapping[i1]
            start_o = mapping[i1]
            
            # 1. Catch up (Preserve leading gaps/tags before this block)
            if start_o > last_orig_pos:
                new_original += original_text[last_orig_pos:start_o]
            
            last_orig_pos = start_o
            
            # 2. Handle the Block
            if tag == 'equal':
                # Preserve entire range [mapping[i1] : mapping[i2]]
                # This treats content + internal/trailing gaps as a block
                end_o = mapping[i2]
                new_original += original_text[start_o:end_o]
                last_orig_pos = end_o
                
            elif tag == 'insert':
                # Insert new content
                new_original += new_stripped_text[j1:j2]
                # last_orig_pos remains start_o (we didn't consume anything)
                
            elif tag == 'delete':
                # Delete content, but PRESERVE GAPS (tags)
                # Iterate through deleted characters to find gaps between them
                for k in range(i1, i2):
                    # Character k corresponds to original range [mapping[k] : mapping[k]+1] (assuming chars are length 1??)
                    # WAIT. strip_text maps indices 1-to-1 for kept text.
                    # So char at old_stripped[k] is original_text[mapping[k]].
                    # It has length 1.
                    
                    # Range of char: mapping[k] to mapping[k]+1
                    # Range of gap after char: mapping[k]+1 to mapping[k+1]
                    
                    # We SKIP the char (Delete it)
                    # We KEEP the gap
                    
                    gap_start = mapping[k] + 1
                    gap_end = mapping[k+1]
                    
                    if gap_end > gap_start:
                        new_original += original_text[gap_start:gap_end]
                        
                # Update last_orig_pos to the end of the deleted block
                last_orig_pos = mapping[i2]
                
            elif tag == 'replace':
                # Insert New Content First
                new_original += new_stripped_text[j1:j2]
                
                # Then "Delete" old content (skipping chars, keeping gaps)
                for k in range(i1, i2):
                    gap_start = mapping[k] + 1
                    gap_end = mapping[k+1]
                    if gap_end > gap_start:
                        new_original += original_text[gap_start:gap_end]
                        
                last_orig_pos = mapping[i2]
                
        # 3. Trailing Gaps
        # After loop, last_orig_pos is at mapping[last_old_len].
        # We need to append any remaining suffix of original_text
        if last_orig_pos < len(original_text):
            new_original += original_text[last_orig_pos:]
            
        return new_original

    def map_opcodes(self, opcodes, map_a, map_b):
        """
        Map opcodes from stripped text diff back to original text indices.
        """
        mapped_opcodes = []
        for tag, i1, i2, j1, j2 in opcodes:
            # Map indices using the provided mappings
            # map_a corresponds to the first sequence (i indices)
            # map_b corresponds to the second sequence (j indices)
            
            new_i1 = map_a[i1]
            new_i2 = map_a[i2]
            new_j1 = map_b[j1]
            new_j2 = map_b[j2]
            
            mapped_opcodes.append((tag, new_i1, new_i2, new_j1, new_j2))
            
        return mapped_opcodes

