from lang.en import TEXTS as EN_TEXTS
from lang.zh import TEXTS as ZH_TEXTS


TEXTS = {
    "zh": ZH_TEXTS,
    "en": EN_TEXTS,
}


def t(lang_code, key):
    lang = lang_code if lang_code in TEXTS else "zh"
    return TEXTS.get(lang, ZH_TEXTS).get(key, EN_TEXTS.get(key, key))


def text_from_config(config, key):
    lang = (config or {}).get("ui_lang", "zh")
    return t(lang, key)
