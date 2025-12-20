from .ru import RU
from .en import EN

_DICTS = {"ru": RU, "en": EN}

def t(lang: str | None, key: str, **kwargs) -> str:
    lang = lang or "ru"
    d = _DICTS.get(lang, RU)

    s = d.get(key) or RU.get(key) or key  # fallback: ru -> key
    try:
        return s.format(**kwargs)
    except Exception:
        return s