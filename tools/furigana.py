import difflib

HAS_KAKASI = False
try:
    import pykakasi
    kks = pykakasi.kakasi()
    HAS_KAKASI = True
    print("pykakasi loaded for Furigana.")
except ImportError:
    print("pykakasi not found. Furigana disabled.")

def generate_furigana_string(text: str) -> str:
    if not HAS_KAKASI:
        return text
        
    result = []
    for item in kks.convert(text):
        orig = item['orig']
        hira = item['hira']
        
        if orig == hira or orig == item['kana']:
            result.append(orig)
            continue
            
        # Handle interleaved okurigana (e.g., 振り込む -> 振[ふ]り込[こ]む)
        # Using SequenceMatcher to align the Kanji+Kana string with the pure Kana string
        matcher = difflib.SequenceMatcher(None, orig, hira)
        
        item_res = ""
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                # Exact match (usually kana)
                item_res += orig[i1:i2]
            elif tag == 'replace' or tag == 'delete' or tag == 'insert':
                # Kanji or changed part
                orig_part = orig[i1:i2]
                hira_part = hira[j1:j2]
                if orig_part:
                    item_res += f"{orig_part}[{hira_part}]"
                else:
                    item_res += hira_part
                    
        result.append(item_res)
        
    return "".join(result)
