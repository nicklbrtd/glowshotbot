from __future__ import annotations
from aiogram import Router

router = Router()

from . import common
from . import menu
from . import stats
from . import users
from . import awards
from . import roles
from . import payments
from . import logs
from . import settings

router.include_router(menu.router)
router.include_router(stats.router)
router.include_router(users.router)
router.include_router(awards.router)
router.include_router(roles.router)
router.include_router(payments.router)
router.include_router(logs.router)
router.include_router(settings.router)

__all__ = ["router"]