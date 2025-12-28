from utils.i18n.ru import RU
from utils.i18n.en import EN

I18N = {
    "ru": RU,
    "en": EN,
}

def t(key: str, lang: str = "ru", **fmt) -> str:
    d = I18N.get(lang) or RU
    text = d.get(key) or RU.get(key) or key
    if fmt:
        try:
            return text.format(**fmt)
        except Exception:
            return text
    return text