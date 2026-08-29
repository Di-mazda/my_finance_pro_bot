from aiogram import Router

from .start import router as start_router
from .reports import router as reports_router
from .tbank_auth import router as tbank_auth_router
from .account import router as account_router
from .planning import router as planning_router
from .fallback import router as fallback_router

router = Router()

router.include_router(start_router)
router.include_router(reports_router)
router.include_router(tbank_auth_router)
router.include_router(account_router)
router.include_router(planning_router)

# ВАЖНО: fallback_router подключается последним - в нём catch-all хендлер
# без фильтров, который должен получать шанс только после того, как
# все остальные роутеры уже проверили сообщение.
router.include_router(fallback_router)
