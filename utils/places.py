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

# Canonical short names for some countries (to keep UI compact)
_COUNTRY_CANON_MAP = {
    # USA
    "соединенные штаты америки": "США",
    "соединённые штаты америки": "США",
    "соединенные штаты": "США",
    "соединённые штаты": "США",
    "united states of america": "США",
    "united states": "США",
    "u.s.a": "США",
    "usa": "США",
    "u.s.": "США",
    "us": "США",
}

def _normalize_country_name(name: str) -> str:
    """Normalize country names (compact canonical names where desired)."""
    nm = _norm_spaces(name or "")
    if not nm:
        return ""
    key = _cmp_key(nm)
    return _COUNTRY_CANON_MAP.get(key, nm)

# A small list for parsing inputs like "Россия Орёл" or "Madrid, Spain".
# (Not a full world database – just common countries for UX.)
_COMMON_COUNTRIES = {
    "россия",
    "сша",
    "украина",
    "казахстан",
    "беларусь",
    "польша",
    "германия",
    "франция",
    "италия",
    "испания",
    "великобритания",
    "англия",
    "uk",
    "united kingdom",
    "китай",
    "china",
    "япония",
    "japan",
    "южная корея",
    "корея",
    "south korea",
    "india",
    "индия",
    "турция",
    "turkey",
    "оаэ",
    "uae",
    "united arab emirates",
    "russia",
    "spain",
    "france",
    "italy",
    "germany",
    "portugal",
    "poland",
    "ukraine",
    "belarus",
    "kazakhstan",
    "united states",
    "united states of america",
}

# Disambiguation for famous cities when user types only the city in Latin letters.
# Example: "Madrid" can match "Madrid, Iowa, USA"; we prefer the world-famous one.
_FAMOUS_CITY_DEFAULT_COUNTRY = {
    "madrid": "Spain",
    "paris": "France",
    "rome": "Italy",
    "lisbon": "Portugal",
    "london": "United Kingdom",
    "barcelona": "Spain",
    "valencia": "Spain",
    "seville": "Spain",
    "berlin": "Germany",
    "vienna": "Austria",
    "prague": "Czechia",
    "warsaw": "Poland",
    "budapest": "Hungary",
    "moscow": "Russia",
    "saint petersburg": "Russia",
    "st petersburg": "Russia",
    "st. petersburg": "Russia",
    "petersburg": "Russia",
    # Note: dots are stripped by _cmp_key, so "st. petersburg" still matches.
}


def _is_latin_city_token(s: str) -> bool:
    s = _norm_spaces(s)
    if not s:
        return False
    # Accept only basic latin letters and dashes/spaces
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z\-\s]*", s))


def _looks_like_country(text: str) -> bool:
    t = _norm_spaces(text or "")
    if not t:
        return False
    key = _cmp_key(t)

    # Direct hits
    if key in _RU_COUNTRY_FIXES:
        return True
    if key in _COUNTRY_CANON_MAP:
        return True
    if key in _COMMON_COUNTRIES:
        return True

    # Canonicalization check (e.g. "Соединенные Штаты" -> "США")
    canon = _normalize_country_name(_title_case(t))
    if _cmp_key(canon) in ("россия", "сша"):
        return True

    return False

_CACHE: dict[tuple[str, str], tuple[float, bool, str]] = {}

_CACHE_CC: dict[tuple[str, str], tuple[float, bool, str, str, str, bool]] = {}
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

def _city_kind_rank(r: dict) -> int:
    """Prefer real cities over villages/hamlets when names are ambiguous."""
    at = (r.get("addresstype") or "").lower()
    # Highest priority first
    if at == "city":
        return 6
    if at in ("town", "borough"):
        return 5
    if at in ("municipality",):
        return 4
    if at in ("county", "district"):
        return 3
    if at in ("suburb", "locality"):
        return 2
    if at in ("village",):
        return 1
    if at in ("hamlet",):
        return 0
    # Unknown -> neutral
    return 2

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
                    # Treat rate limits / temporary errors as "service unavailable"
                    raise RuntimeError(f"Nominatim HTTP {resp.status}")
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

def _best_country_code(addr: dict) -> str:
    cc = (addr.get("country_code") or "").strip()
    return cc.upper() if cc else ""

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

    if kind == "country":
        canonical = _normalize_country_name(canonical)

    ok = best_score >= 0.72
    _CACHE[ck] = (now, ok, canonical)
    return ok, canonical, True


# --- New: validate_city_and_country ---

async def validate_city_and_country_full(raw: str) -> tuple[bool, str, str, str, bool]:
    """Validate and normalize a city, and also infer the country and its ISO code.

    returns: (ok, canonical_city, canonical_country, canonical_country_code, used_geocoder)

    If the geocoder is unavailable, returns ok=True with normalized city and empty country/code
    (so UX doesn't break and we don't overwrite user's existing country with junk).
    """
    raw = _norm_spaces(raw)
    if not raw:
        return False, "", "", "", False

    if len(raw) > 64:
        return False, "", "", "", False

    if re.search(r"\d", raw):
        return False, "", "", "", False

    if re.search(r"[<>@#$/\\{}\[\]|~`^*=+]", raw):
        return False, "", "", "", False

    # If user typed a country name into the city field (e.g. "Россия"), reject it.
    # Otherwise Nominatim may return a country and we end up with "Россия, Россия".
    if "," not in raw and len(raw.split()) == 1 and _looks_like_country(raw):
        return False, _title_case(raw), _normalize_country_name(_title_case(raw)), "", False

    # Parse "city, country" / "country, city" / "country city" inputs
    city_hint = raw
    country_hint = ""

    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) >= 2:
            # Decide which side is country
            if _looks_like_country(parts[-1]):
                city_hint = parts[0]
                country_hint = parts[-1]
            elif _looks_like_country(parts[0]):
                country_hint = parts[0]
                city_hint = parts[-1]
            else:
                city_hint = parts[0]
                country_hint = parts[-1]
    else:
        tokens = raw.split()
        if len(tokens) >= 2 and _looks_like_country(tokens[0]):
            country_hint = tokens[0]
            city_hint = " ".join(tokens[1:])

    # If user typed only a famous city in Latin letters, add a country hint to avoid US small-town matches.
    if not country_hint and len(city_hint.split()) == 1 and _is_latin_city_token(city_hint):
        k = _cmp_key(city_hint)
        if k in _FAMOUS_CITY_DEFAULT_COUNTRY:
            country_hint = _FAMOUS_CITY_DEFAULT_COUNTRY[k]

    cmp_city = _cmp_key(city_hint)
    cmp_country_hint = _cmp_key(country_hint) if country_hint else ""

    # Quick fixes for popular typos
    if cmp_city in _RU_CITY_FIXES:
        city = _RU_CITY_FIXES[cmp_city]
        country = _RU_CITY_TO_COUNTRY.get(cmp_city, "")
        if not country and country_hint:
            country = _normalize_country_name(_title_case(country_hint))

        country_code = ""
        if _cmp_key(country) in ("россия", "росия"):
            country_code = "RU"
        elif _cmp_key(country) == "сша":
            country_code = "US"

        return True, city, country, country_code, False

    now = time.time()
    ck = (cmp_city, cmp_country_hint)
    if ck in _CACHE_CC:
        ts, ok, city, country, country_code, used_geo = _CACHE_CC[ck]
        if (now - ts) < _TTL:
            return ok, city, country, country_code, used_geo

    # Build a geocoder query that includes country hint if user provided it
    if country_hint:
        query = f"{_title_case(city_hint)}, {_title_case(country_hint)}"
    else:
        query = _title_case(city_hint)

    try:
        results = await _nominatim_search(query, limit=5)
    except Exception:
        # API down -> don't block the user, but don't overwrite country
        city_fallback = _title_case(city_hint)
        _CACHE_CC[ck] = (now, True, city_fallback, "", "", False)
        return True, city_fallback, "", "", False

    if not results:
        city_fallback = _title_case(city_hint)
        _CACHE_CC[ck] = (now, False, city_fallback, "", "", True)
        return False, city_fallback, "", "", True

    best_city = None
    best_country = None
    best_country_code = ""
    best_sim = 0.0
    best_rank = -1
    best_importance = -1.0

    for pass_no in (1, 2):
        for r in results:
            if pass_no == 1 and not _passes_kind_filter("city", r):
                continue

            addr = r.get("address") or {}
            cand_city = _best_city(addr)
            cand_country = _best_country(addr)
            cand_country_code = _best_country_code(addr)

            if not cand_city:
                cand_city = (r.get("display_name") or "").split(",", 1)[0].strip() or None

            if not cand_city:
                continue

            sim = difflib.SequenceMatcher(a=_cmp_key(city_hint), b=_cmp_key(cand_city)).ratio()
            rank = _city_kind_rank(r)
            try:
                imp = float(r.get("importance") or 0.0)
            except Exception:
                imp = 0.0

            # Lexicographic preference: higher name similarity, then "more city-like", then more important
            if (sim, rank, imp) > (best_sim, best_rank, best_importance):
                best_sim = sim
                best_rank = rank
                best_importance = imp
                best_city = cand_city
                best_country = cand_country
                best_country_code = cand_country_code

        if best_city is not None:
            break

    canonical_city = _title_case(best_city or city_hint)
    canonical_country = _normalize_country_name(_title_case(best_country or ""))
    canonical_country_code = (best_country_code or "").upper()

    # If user provided country hint and geocoder couldn't infer it, use the hint
    if country_hint and not canonical_country:
        canonical_country = _normalize_country_name(_title_case(country_hint))

    # Ё / common RU fixes
    if _cmp_key(canonical_city) == "орел":
        canonical_city = "Орёл"
        canonical_country = canonical_country or "Россия"
        canonical_country_code = canonical_country_code or "RU"

    if _cmp_key(canonical_country) in ("россия", "росия"):
        canonical_country = "Россия"
        canonical_country_code = canonical_country_code or "RU"

    canonical_country = _normalize_country_name(canonical_country)

    if _cmp_key(canonical_country) == "сша":
        canonical_country_code = canonical_country_code or "US"

    # Guard: avoid "Страна, Страна" in profile
    if canonical_country and _cmp_key(canonical_city) == _cmp_key(canonical_country):
        # If we have a meaningful city hint, prefer it
        if _cmp_key(city_hint) and _cmp_key(city_hint) != _cmp_key(canonical_country):
            canonical_city = _title_case(city_hint)
        else:
            canonical_country = ""
            canonical_country_code = ""

    ok = best_sim >= 0.72

    # Final sanity: if result still looks like a country (and user didn't provide a country hint), reject.
    # Example: user typed "Россия" as city.
    if not country_hint and _looks_like_country(canonical_city):
        ok = False

    _CACHE_CC[ck] = (now, ok, canonical_city, canonical_country, canonical_country_code, True)
    return ok, canonical_city, canonical_country, canonical_country_code, True


async def validate_city_and_country(raw: str) -> tuple[bool, str, str, bool]:
    """Backward-compatible wrapper.

    returns: (ok, canonical_city, canonical_country, used_geocoder)

    Use `validate_city_and_country_full()` to also get ISO country code.
    """
    ok, city, country, _country_code, used_geo = await validate_city_and_country_full(raw)
    return ok, city, country, used_geo