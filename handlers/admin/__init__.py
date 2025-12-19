from __future__ import annotations

# =============================================================
# ==== АДМИНКА: СБОРКА РОУТЕРОВ ===============================
# =============================================================

from aiogram import Router

# ВАЖНО: импортируем роутеры из модулей напрямую,
# не делаем `from . import X`, чтобы не ловить циклы.

from .menu import router as menu_router
from .stats import router as stats_router
from .users import router as users_router
from .roles import router as roles_router
from .payments import router as payments_router
from .logs import router as logs_router
from .broadcast import router as broadcast_router

# settings.py может быть пустым/в процессе — подключаем мягко
try:
    from .settings import router as settings_router
except Exception:
    settings_router = None

router = Router()

# Порядок важен: меню обычно верхний уровень
router.include_router(menu_router)
router.include_router(stats_router)
router.include_router(users_router)
router.include_router(roles_router)
router.include_router(payments_router)
router.include_router(logs_router)
router.include_router(broadcast_router)
if settings_router is not None:
    router.include_router(settings_router)