from __future__ import annotations

from typing import Any
import math
import os
from datetime import datetime

from database_results import (
    PERIOD_DAY,
    SCOPE_GLOBAL,
    KIND_TOP_PHOTOS,
    ensure_results_schema,
    upsert_results_items,
    has_results,
)

from services.results_queries import (
    get_global_mean_and_count,
    get_day_photo_rows_global,
    get_day_photo_rows_user,
)
from services.results_scoring import bayes_score, rules_for_scope, pick_top_photos
from utils.time import get_moscow_today
from database import get_user_by_tg_id


try:
    from database import _assert_pool
except Exception:  # pragma: no cover
    _assert_pool = None  # type: ignore


def _pool() -> Any:
    if _assert_pool is None:
        raise RuntimeError("DB pool is not available: cannot import _assert_pool from database.py")
    return _assert_pool()


async def _ensure_last_win_column(conn) -> None:
    """Добавляем photos.last_win_date, если ещё нет (idempotent)."""
    await conn.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name='photos' AND column_name='last_win_date'
            ) THEN
                ALTER TABLE photos ADD COLUMN last_win_date TEXT;
            END IF;
        END $$;
        """
    )


async def recalc_day_global(*, day_key: str, limit: int = 10) -> int:
    """Подсчёт и кэширование глобальных итогов дня (чистая бизнес‑логика).

    Правила (MVP):
    - участник: у автора >=2 квалифицированных приглашения;
    - фото: активно, нет открытых жалоб, >=20 оценок, не выигрывал в этот же день,
      и победившее фото уходит на «кд» 1 день (last_win_date).
    - скор: Байес + бонус за количество оценок + лёгкий бонус за комментарии;
      жалобы режут скор, но pending жалобы уже исключены.
    - кэш: если уже есть готовые итоги и пересчёт не требуется — выходим.
    """

    await ensure_results_schema()

    # Кэш: не пересчитываем, если уже есть сохранённые итоги на этот день.
    if await has_results(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=SCOPE_GLOBAL,
        scope_key="global",
        kind=KIND_TOP_PHOTOS,
    ):
        return 0

    p = _pool()
    async with p.acquire() as conn:
        await _ensure_last_win_column(conn)
        global_mean, _cnt = await get_global_mean_and_count(conn)
        rows = await get_day_photo_rows_global(conn, day_key=str(day_key))

    # Параметры скоринга (тонкая настройка через ENV).
    prior = int(os.getenv("RESULTS_BAYES_PRIOR", "20"))
    min_ratings = 20  # жёсткое требование из ТЗ
    min_invited = 2
    rating_weight = float(os.getenv("RESULTS_WEIGHT_RATINGS", "0.02"))
    comments_weight = float(os.getenv("RESULTS_WEIGHT_COMMENTS", "0.01"))
    reports_penalty = float(os.getenv("RESULTS_PENALTY_REPORT", "5.0"))

    # Разбор day_key в дату для проверки cooldown победителей.
    try:
        day_dt = datetime.fromisoformat(str(day_key)).date()
    except Exception:
        day_dt = None

    candidates: list[dict] = []

    for r in rows:
        ratings_count = int(r.get("ratings_count") or 0)
        comments_count = int(r.get("comments_count") or 0)
        pending_reports = int(r.get("pending_reports") or 0)
        invited = int(r.get("invited_qualified") or 0)

        if ratings_count < min_ratings:
            continue
        if invited < min_invited:
            continue
        if pending_reports > 0:
            continue

        # cooldown победителя: last_win_date + 1 день недоступны
        last_win_raw = r.get("last_win_date")
        if last_win_raw and day_dt is not None:
            try:
                last_win_dt = datetime.fromisoformat(str(last_win_raw)).date()
                if (day_dt - last_win_dt).days <= 1:
                    continue
            except Exception:
                pass

        sum_values = float(r.get("sum_values") or 0.0)
        bayes = bayes_score(
            sum_values=sum_values,
            n=ratings_count,
            global_mean=float(global_mean),
            prior=prior,
        )
        if bayes is None:
            continue

        score = float(bayes)
        score += rating_weight * math.log1p(max(ratings_count, 0))
        score += comments_weight * math.log1p(max(comments_count, 0))
        if pending_reports:
            score -= reports_penalty * pending_reports

        rr = dict(r)
        rr["score"] = float(score)
        rr["bayes_score"] = float(bayes)
        candidates.append(rr)

    # Оставляем по одному лучшему фото на автора (актуально, если у премиум 2 активных фото).
    best_by_user: dict[int, dict] = {}
    for c in candidates:
        uid = int(c.get("user_id") or 0)
        prev = best_by_user.get(uid)
        if prev is None or _better_photo(c, prev):
            best_by_user[uid] = c

    # Сортировка и топ-N
    filtered = list(best_by_user.values())
    filtered.sort(
        key=lambda x: (
            -(float(x.get("score") or 0.0)),
            -(int(x.get("ratings_count") or 0)),
            -(int(x.get("comments_count") or 0)),
            str(x.get("created_at") or ""),
        )
    )

    top = filtered[: int(limit)]

    items: list[dict] = []
    for idx, r in enumerate(top, start=1):
        items.append(
            {
                "place": idx,
                "photo_id": int(r["photo_id"]),
                "user_id": int(r["user_id"]),
                "score": float(r.get("score") or 0.0),
                "payload": {
                    "photo_id": int(r["photo_id"]),
                    "file_id": r.get("file_id"),
                    "title": r.get("title") or "Без названия",
                    "avg_rating": r.get("avg_rating"),
                    "ratings_count": int(r.get("ratings_count") or 0),
                    "rated_users": int(r.get("rated_users") or 0),
                    "comments_count": int(r.get("comments_count") or 0),
                    "user_name": r.get("user_name") or "",
                    "user_username": r.get("user_username"),
                },
            }
        )

    await upsert_results_items(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=SCOPE_GLOBAL,
        scope_key="global",
        kind=KIND_TOP_PHOTOS,
        items=items,
    )

    # Отметить победителя (1 место) как победившего в этот день
    if top:
        winner_photo_id = int(top[0]["photo_id"])
        async with p.acquire() as conn:
            await _ensure_last_win_column(conn)
            await conn.execute(
                "UPDATE photos SET last_win_date=$1 WHERE id=$2",
                str(day_key),
                int(winner_photo_id),
            )

    return len(items)


def _better_photo(a: dict, b: dict) -> bool:
    """True если фото a лучше фото b по нашим правилам сравнения."""
    return (
        (float(a.get("score") or 0.0), int(a.get("ratings_count") or 0), int(a.get("comments_count") or 0))
        > (float(b.get("score") or 0.0), int(b.get("ratings_count") or 0), int(b.get("comments_count") or 0))
    )


# ---------------- Eligibility helper (для UI «Итоги дня») ----------------

async def get_day_eligibility(user_tg_id: int, day_key: str | None = None) -> dict:
    """
    Вернёт статусы условий допуска к итогам дня для конкретного пользователя.

    Поля:
      eligible: bool
      checks: list[{title, ok, value}]
      note_best_photo: str | None
    """
    day_key = day_key or get_moscow_today().isoformat()

    user = await get_user_by_tg_id(int(user_tg_id))
    if not user:
        return {"eligible": False, "checks": [], "note_best_photo": None}

    user_id = int(user["id"])

    p = _pool()
    async with p.acquire() as conn:
        await _ensure_last_win_column(conn)
        invited = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM referrals WHERE inviter_user_id=$1 AND qualified=1",
                int(user_id),
            )
            or 0
        )
        rows = await get_day_photo_rows_user(conn, day_key=str(day_key), user_id=user_id)

    min_invited = 2
    min_ratings = 20

    best_photo = None
    for r in rows:
        ratings_count = int(r.get("ratings_count") or 0)
        pending_reports = int(r.get("pending_reports") or 0)
        if ratings_count < min_ratings:
            continue
        if pending_reports > 0:
            continue
        best_photo = r
        break

    checks = [
        {"title": "Пригласить 2 друзей по реферальной ссылке", "ok": invited >= min_invited, "value": invited},
        {"title": "Собрать 20 оценок на сегодняшней фото", "ok": bool(best_photo), "value": None},
        {
            "title": "Отсутствие открытых жалоб на фото",
            "ok": bool(best_photo) and int(best_photo.get("pending_reports") or 0) == 0 if best_photo else False,
            "value": None,
        },
    ]

    note = None
    if rows:
        note = f"У тебя активных фото за день: {len(rows)}. В итоги попадёт одна — с лучшим рейтингом."

    return {
        "eligible": all(c["ok"] for c in checks) if checks else False,
        "checks": checks,
        "note_best_photo": note,
    }


from database_results import SCOPE_CITY, SCOPE_COUNTRY
from services.results_queries import (
    get_day_photo_rows_city,
    get_day_photo_rows_country,
    count_active_authors_city,
    count_active_authors_country,
)

async def recalc_day_city(*, day_key: str, city: str, limit: int = 10) -> int:
    await ensure_results_schema()

    p = _pool()
    async with p.acquire() as conn:
        pop = await count_active_authors_city(conn, city=str(city))
        if int(pop) < 5:
            return 0

        global_mean, _ = await get_global_mean_and_count(conn)
        rows = await get_day_photo_rows_city(conn, day_key=str(day_key), city=str(city))

    rules = rules_for_scope(SCOPE_CITY)
    top = pick_top_photos(rows, global_mean=float(global_mean), rules=rules, limit=int(limit))

    items: list[dict] = []
    for idx, r in enumerate(top, start=1):
        items.append(
            {
                "place": idx,
                "photo_id": int(r["photo_id"]),
                "user_id": int(r["user_id"]),
                "score": float(r.get("bayes_score") or 0.0),
                "payload": {
                    "photo_id": int(r["photo_id"]),
                    "file_id": r.get("file_id"),
                    "title": r.get("title") or "Без названия",
                    "avg_rating": r.get("avg_rating"),
                    "ratings_count": int(r.get("ratings_count") or 0),
                    "rated_users": int(r.get("rated_users") or 0),
                    "user_name": r.get("user_name") or "",
                    "user_username": r.get("user_username"),
                    "comments_count": int(r.get("comments_count") or 0),
                    "super_count": int(r.get("super_count") or 0),
                },
            }
        )

    await upsert_results_items(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=SCOPE_CITY,
        scope_key=str(city),
        kind=KIND_TOP_PHOTOS,
        items=items,
    )
    return len(items)


async def recalc_day_country(*, day_key: str, country: str, limit: int = 10) -> int:
    await ensure_results_schema()

    p = _pool()
    async with p.acquire() as conn:
        pop = await count_active_authors_country(conn, country=str(country))
        if int(pop) < 100:
            return 0

        global_mean, _ = await get_global_mean_and_count(conn)
        rows = await get_day_photo_rows_country(conn, day_key=str(day_key), country=str(country))

    rules = rules_for_scope(SCOPE_COUNTRY)
    top = pick_top_photos(rows, global_mean=float(global_mean), rules=rules, limit=int(limit))

    items: list[dict] = []
    for idx, r in enumerate(top, start=1):
        items.append(
            {
                "place": idx,
                "photo_id": int(r["photo_id"]),
                "user_id": int(r["user_id"]),
                "score": float(r.get("bayes_score") or 0.0),
                "payload": {
                    "photo_id": int(r["photo_id"]),
                    "file_id": r.get("file_id"),
                    "title": r.get("title") or "Без названия",
                    "avg_rating": r.get("avg_rating"),
                    "ratings_count": int(r.get("ratings_count") or 0),
                    "rated_users": int(r.get("rated_users") or 0),
                    "user_name": r.get("user_name") or "",
                    "user_username": r.get("user_username"),
                    "comments_count": int(r.get("comments_count") or 0),
                    "super_count": int(r.get("super_count") or 0),
                },
            }
        )

    await upsert_results_items(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=SCOPE_COUNTRY,
        scope_key=str(country),
        kind=KIND_TOP_PHOTOS,
        items=items,
    )
    return len(items)
