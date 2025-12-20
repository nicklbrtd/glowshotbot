import re
import difflib
import aiohttp
import asyncio
import time

# супер-частые фиксы (не база мира, а просто “популярные опечатки”)
_RU_CITY_FIXES = {
    "орел": "Орёл",
    "орёл": "Орёл",
    "orel": "Орёл",
    "oryol": "Орёл",
}

_RU_CITY_TO_COUNTRY = {
    "орел": "Россия",
    "орёл": "Россия",
    "orel": "Россия",
    "oryol": "Россия",
}

_RU_COUNTRY_FIXES = {
    "росия": "Россия",
    "россия": "Россия",
    "russia": "Россия",
    "rusia": "Россия",
}

_CACHE: dict[tuple[str, str], tuple[float, bool, str]] = {}

_CACHE_CC: dict[tuple[str], tuple[float, bool, str, str, bool]] = {}
_TTL = 60 * 60 * 24 * 7  # 7 дней

# чтобы не долбить Nominatim параллельно
_SEM = asyncio.Semaphore(2)

# Nominatim usage: be polite (global rate limit) and provide a real contact in User-Agent.
# Please replace CONTACT with your real support link/email.
_NOMINATIM_USER_AGENT = "GlowShotBot/1.0 (contact: t.me/nyqlbrtd)"
_NOMINATIM_MIN_INTERVAL_SEC = 1.0  # ~1 req/sec across the whole process
_NOMINATIM_LOCK = asyncio.Lock()
_LAST_NOMINATIM_TS = 0.0

# Kind filters (to avoid validating a city as a country and vice versa)
_CITY_ADDRESSTYPES = {
    "city",
    "town",
    "village",
    "hamlet",
    "municipality",
    "county",
    "locality",
    "suburb",
    "borough",
    "district",
}
_COUNTRY_ADDRESSTYPES = {"country"}


def _passes_kind_filter(kind: str, r: dict) -> bool:
    at = (r.get("addresstype") or "").lower()
    if not at:
        # If addresstype is missing, don't block the candidate.
        return True
    if kind == "city":
        return at in _CITY_ADDRESSTYPES
    if kind == "country":
        return at in _COUNTRY_ADDRESSTYPES
    return True

def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _cmp_key(s: str) -> str:
    s = _norm_spaces(s).lower()
    s = s.replace("ё", "е")
    s = re.sub(r"[^a-zа-я0-9\s-]", "", s)
    return s

def _title_case(s: str) -> str:
    s = _norm_spaces(s)
    parts = []
    for token in s.split(" "):
        if "-" in token:
            sub = [t[:1].upper() + t[1:].lower() if t else "" for t in token.split("-")]
            parts.append("-".join(sub))
        else:
            parts.append(token[:1].upper() + token[1:].lower() if token else "")
    return " ".join(parts)

async def _nominatim_search(q: str, limit: int = 5) -> list[dict]:
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "format": "jsonv2",
        "q": q,
        "addressdetails": 1,
        "limit": str(limit),
        "accept-language": "ru,en",
    }
    headers = {"User-Agent": _NOMINATIM_USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=6)

    global _LAST_NOMINATIM_TS

    async with _SEM:
        # Global rate limit (~1 request/sec for the whole process)
        async with _NOMINATIM_LOCK:
            now = time.monotonic()
            delta = now - _LAST_NOMINATIM_TS
            if delta < _NOMINATIM_MIN_INTERVAL_SEC:
                await asyncio.sleep(_NOMINATIM_MIN_INTERVAL_SEC - delta)
            _LAST_NOMINATIM_TS = time.monotonic()

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                return await resp.json()

def _best_city(addr: dict) -> str | None:
    for k in ("city", "town", "village", "hamlet", "municipality", "county"):
        v = addr.get(k)
        if v:
            return str(v)
    return None

def _best_country(addr: dict) -> str | None:
    v = addr.get("country")
    return str(v) if v else None

async def validate_place(kind: str, raw: str) -> tuple[bool, str, bool]:
    """
    kind: 'city' or 'country'
    returns: (ok, canonical, used_geocoder)

    Если геокодер недоступен -> ok=True, canonical=нормализованный ввод (чтобы UX не ломался)
    """
    raw = _norm_spaces(raw)
    if not raw:
        return False, "", False

    if kind not in ("city", "country"):
        return False, "", False

    # Keep it sane: too long or has clearly unsafe/special symbols -> reject
    if len(raw) > 64:
        return False, "", False

    if re.search(r"\d", raw):
        return False, "", False

    if re.search(r"[<>@#$/\\{}\[\]|~`^*=+]", raw):
        return False, "", False

    cmp = _cmp_key(raw)

    if kind == "city" and cmp in _RU_CITY_FIXES:
        return True, _RU_CITY_FIXES[cmp], False
    if kind == "country" and cmp in _RU_COUNTRY_FIXES:
        return True, _RU_COUNTRY_FIXES[cmp], False

    now = time.time()
    ck = (kind, cmp)
    if ck in _CACHE:
        ts, ok, canon = _CACHE[ck]
        if (now - ts) < _TTL:
            return ok, canon, False

    query = _title_case(raw)

    try:
        results = await _nominatim_search(query, limit=5)
    except Exception:
        # API лежит -> не душим юзера
        _CACHE[ck] = (now, True, query)
        return True, query, False

    if not results:
        _CACHE[ck] = (now, False, query)
        return False, query, True

    best_name = None
    best_score = 0.0

    # Two-pass: first try strict kind filter, then fallback to any result if nothing matches.
    for pass_no in (1, 2):
        for r in results:
            if pass_no == 1 and not _passes_kind_filter(kind, r):
                continue

            addr = r.get("address") or {}
            cand = _best_city(addr) if kind == "city" else _best_country(addr)
            if not cand:
                cand = (r.get("display_name") or "").split(",", 1)[0].strip() or None
            if not cand:
                continue

            score = difflib.SequenceMatcher(a=_cmp_key(raw), b=_cmp_key(cand)).ratio()
            if score > best_score:
                best_score = score
                best_name = cand

        if best_name is not None:
            break

    canonical = _title_case(best_name or query)

    # спец-правка Ё
    if kind == "city" and _cmp_key(canonical) == "орел":
        canonical = "Орёл"
    if kind == "country" and _cmp_key(canonical) in ("россия", "росия"):
        canonical = "Россия"

    ok = best_score >= 0.72
    _CACHE[ck] = (now, ok, canonical)
    return ok, canonical, True


# --- New: validate_city_and_country ---

async def validate_city_and_country(raw: str) -> tuple[bool, str, str, bool]:
    """Validate and normalize a city, and also infer the country.

    returns: (ok, canonical_city, canonical_country, used_geocoder)

    If the geocoder is unavailable, returns ok=True with normalized city and empty country
    (so UX doesn't break and we don't overwrite user's existing country with junk).
    """
    raw = _norm_spaces(raw)
    if not raw:
        return False, "", "", False

    if len(raw) > 64:
        return False, "", "", False

    if re.search(r"\d", raw):
        return False, "", "", False

    if re.search(r"[<>@#$/\\{}\[\]|~`^*=+]", raw):
        return False, "", "", False

    cmp = _cmp_key(raw)

    # Quick fixes for popular typos
    if cmp in _RU_CITY_FIXES:
        city = _RU_CITY_FIXES[cmp]
        country = _RU_CITY_TO_COUNTRY.get(cmp, "")
        return True, city, country, False

    now = time.time()
    ck = (cmp,)
    if ck in _CACHE_CC:
        ts, ok, city, country, used_geo = _CACHE_CC[ck]
        if (now - ts) < _TTL:
            return ok, city, country, used_geo

    query = _title_case(raw)

    try:
        results = await _nominatim_search(query, limit=5)
    except Exception:
        # API down -> don't block the user, but don't overwrite country
        _CACHE_CC[ck] = (now, True, query, "", False)
        return True, query, "", False

    if not results:
        _CACHE_CC[ck] = (now, False, query, "", True)
        return False, query, "", True

    best_city = None
    best_country = None
    best_score = 0.0

    for pass_no in (1, 2):
        for r in results:
            if pass_no == 1 and not _passes_kind_filter("city", r):
                continue

            addr = r.get("address") or {}
            cand_city = _best_city(addr)
            cand_country = _best_country(addr)

            if not cand_city:
                cand_city = (r.get("display_name") or "").split(",", 1)[0].strip() or None

            if not cand_city:
                continue

            score = difflib.SequenceMatcher(a=_cmp_key(raw), b=_cmp_key(cand_city)).ratio()
            if score > best_score:
                best_score = score
                best_city = cand_city
                best_country = cand_country

        if best_city is not None:
            break

    canonical_city = _title_case(best_city or query)
    canonical_country = _title_case(best_country or "")

    # Ё / common RU fixes
    if _cmp_key(canonical_city) == "орел":
        canonical_city = "Орёл"
        canonical_country = canonical_country or "Россия"

    if _cmp_key(canonical_country) in ("россия", "росия"):
        canonical_country = "Россия"

    ok = best_score >= 0.72
    _CACHE_CC[ck] = (now, ok, canonical_city, canonical_country, True)
    return ok, canonical_city, canonical_country, True