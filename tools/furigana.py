import difflib
import re
from itertools import combinations

# ==========================================
# Backend flags
# ==========================================
HAS_KAKASI = False
HAS_JMDICT = False

try:
    import pykakasi
    kks = pykakasi.kakasi()
    HAS_KAKASI = True
    print("pykakasi loaded for Furigana.")
except ImportError:
    print("pykakasi not found.")

try:
    from jamdict import Jamdict
    jam = Jamdict()
    HAS_JMDICT = True
    print("jamdict loaded for Furigana (JMDict splitting).")
except (ImportError, RuntimeError, Exception) as e:
    print(f"JMDict-based splitting disabled: {e}")

HAS_FURIGANA = HAS_KAKASI

# ==========================================
# Katakana -> Hiragana conversion
# ==========================================
kana_from = (
    "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホ"
    "マミムメモヤユヨラリルレロワヲンヴガギグゲゴザジズゼゾ"
    "ダヂヅデドバビブベボパピプペポァィゥェォャュョッー"
)
kana_to = (
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほ"
    "まみむめもやゆよらりるれろわをんゔがぎぐげござじずぜぞ"
    "だぢづでどばびぶべぼぱぴぷぺぽぁぃぅぇぉゃゅょっー"
)
K2H = str.maketrans(kana_from, kana_to)

def kata_to_hira(s: str) -> str:
    return s.translate(K2H)


def hira_to_kata(s: str) -> str:
    result = []
    for ch in s:
        code = ord(ch)
        if 0x3041 <= code <= 0x3096:
            result.append(chr(code + 0x60))
        else:
            result.append(ch)
    return "".join(result)


def _display_reading(reading: str, kana_type: str = "hiragana") -> str:
    if kana_type == "katakana":
        return hira_to_kata(reading)
    return reading


def _format_ruby(surface: str, reading: str, left_marker: str = "[", right_marker: str = "]") -> str:
    return f"{surface}{left_marker}{reading}{right_marker}"


# ==========================================
# JMDict-based furigana splitting
# ==========================================

# Rendaku (連濁) tables for matching variant readings
RENDAKU = {
    'か': 'が', 'き': 'ぎ', 'く': 'ぐ', 'け': 'げ', 'こ': 'ご',
    'さ': 'ざ', 'し': 'じ', 'す': 'ず', 'せ': 'ぜ', 'そ': 'ぞ',
    'た': 'だ', 'ち': 'ぢ', 'つ': 'づ', 'て': 'で', 'と': 'ど',
    'は': 'ば', 'ひ': 'び', 'ふ': 'ぶ', 'へ': 'べ', 'ほ': 'ぼ',
    'ば': 'ぱ', 'び': 'ぴ', 'ぶ': 'ぷ', 'べ': 'ぺ', 'ぼ': 'ぽ',
}
RENDAKU_1 = {'は': 'ぱ', 'ひ': 'ぴ', 'ふ': 'ぷ', 'へ': 'ぺ', 'ほ': 'ぽ'}

def _variants(rds):
    """Generate rendaku variants for a list of readings."""
    vs = set()
    for rd in rds:
        if rd and rd[0] in RENDAKU:
            vs.add(RENDAKU[rd[0]] + rd[1:])
        if rd and rd[0] in RENDAKU_1:
            vs.add(RENDAKU_1[rd[0]] + rd[1:])
        if rd and (rd.endswith("く") or rd.endswith('つ')):
            vs.add(rd[:-1] + "っ")
    return vs


def _is_illegal_kana_start(kana_piece):
    """Check if a kana piece starts with a character that cannot begin a reading."""
    if not kana_piece:
        return True
    # Small kana cannot start a reading segment
    return kana_piece[0] in "ゃゅょぁぃぅぇぉ"


def _split_kana_candidates(kana: str, num_parts: int):
    """Generate all valid ways to split a kana string into num_parts pieces."""
    if num_parts <= 0:
        return []
    if num_parts == 1:
        return [[kana]]
    results = []
    for indices in combinations(range(1, len(kana)), num_parts - 1):
        parts = []
        last = 0
        for idx in indices:
            parts.append(kana[last:idx])
            last = idx
        parts.append(kana[last:])

        # Filter out invalid splits (small kana at start of non-first segment)
        if any(_is_illegal_kana_start(p) for p in parts[1:]):
            continue

        results.append(parts)
    return results


def _get_kanji_readings(char: str):
    """Look up all possible readings (kun + on) for a single kanji from JMDict."""
    if not HAS_JMDICT:
        return []
    if char in kana_from or char in kana_to:
        return [char]
    res = jam.lookup(char, strict_lookup=True, lookup_chars=True, lookup_ne=False)
    rdgs = []
    for cnode in res.chars:
        for rm_group in cnode.rm_groups:
            for kun_reading in rm_group.kun_readings:
                rdgs.append(re.sub(r'\..+|\-', '', kun_reading.value))
            for on_reading in rm_group.on_readings:
                rdgs.append(kata_to_hira(on_reading.value))
    return list({r for r in rdgs if r})


def _split_furigana_jmdict(surface: str, kana: str):
    """
    Split a word's furigana reading into per-character assignments using JMDict.
    
    Returns a list of (char, reading) tuples, or None if splitting fails.
    E.g. ("振込", "ふりこ") -> [("振", "ふ"), ("り", "り"), ("込", "こ")]
    """
    chars = re.findall(r'.[ゃゅょぁぃぅぇぉ]?', surface)
    if not chars:
        return None

    best = None
    best_score = -1

    for parts in _split_kana_candidates(kana, len(chars)):
        matched = []
        score = 0
        for ch, kana_piece in zip(chars, parts):
            if len(ch) == 1 and '\u4e00' <= ch[0] <= '\u9fff':
                possible = _get_kanji_readings(ch)
            else:
                possible = [kata_to_hira(ch)]
            if kana_piece in possible or kana_piece in _variants(possible):
                matched.append((ch, kana_piece))
                score += 1
            else:
                matched.append((ch, kana_piece))

        # Perfect match — return immediately
        if score == len(chars):
            return matched

        # Track best partial match
        if score > best_score:
            best = matched
            best_score = score

    return best if best_score > 0 else None


# ==========================================
# Regex-based fallback splitting
# ==========================================

def _split_furigana_regex(
    orig: str,
    hira: str,
    left_marker: str = "[",
    right_marker: str = "]",
    kana_type: str = "hiragana",
) -> str:
    """
    Regex fallback to split interleaved okurigana (e.g. 言い返す -> 言[い]い返[かえ]す).
    Replaces SequenceMatcher which had bugs matching identical kana.
    """
    pattern = ""
    blocks = []
    chunks = re.split(r'([ぁ-んァ-ンー]+)', orig)
    for chunk in chunks:
        if not chunk: continue
        if re.match(r'^[ぁ-んァ-ンー]+$', chunk):
            pattern += re.escape(kata_to_hira(chunk))
            blocks.append(('kana', chunk))
        else:
            pattern += r'(.+)'
            blocks.append(('kanji', chunk))
            
    pattern = '^' + pattern + '$'
    match = re.match(pattern, hira)
    if match:
        groups = match.groups()
        group_idx = 0
        res = ""
        for b_type, text in blocks:
            if b_type == 'kana':
                res += text
            else:
                reading = groups[group_idx]
                group_idx += 1
                res += _format_ruby(text, _display_reading(reading, kana_type), left_marker, right_marker)
        return res
    
    # If regex fails for some reason, return the whole thing
    return _format_ruby(orig, _display_reading(hira, kana_type), left_marker, right_marker)


# ==========================================
# Public API
# ==========================================

def generate_furigana_string(
    text: str,
    left_marker: str = "[",
    right_marker: str = "]",
    kana_type: str = "hiragana",
    use_jmdict_split: bool = True,
) -> str:
    """
    Add furigana annotations to Japanese text using pykakasi for tokenization.
    
    Strategy:
    - Uses pykakasi to convert sentences and word tokenization.
    - If JMDict is configured, attempts JMDict-based per-character reading splitting.
    - Otherwise uses Regex-based interleaved okurigana mapping logic.
    
    Output format defaults to text[reading].
    """
    if not HAS_KAKASI:
        return text
        
    result = []
    for item in kks.convert(text):
        orig = item['orig']
        hira = item['hira']
        
        # If identical or has no kanji, no furigana needed
        if orig == hira or orig == item['kana'] or not re.search(r'[\u4e00-\u9fff]', orig):
            result.append(orig)
            continue
            
        # 1) Try JMDict per-character split
        parts = None
        if use_jmdict_split and HAS_JMDICT:
            parts = _split_furigana_jmdict(orig, hira)
            
        if parts:
            item_res = ""
            for surface, reading in parts:
                if kata_to_hira(surface) == reading:
                    item_res += surface
                else:
                    item_res += _format_ruby(
                        surface,
                        _display_reading(reading, kana_type),
                        left_marker,
                        right_marker,
                    )
            result.append(item_res)
        # 2) Fallback to robust Regex block split
        else:
            result.append(_split_furigana_regex(orig, hira, left_marker, right_marker, kana_type))
            
    return "".join(result)
