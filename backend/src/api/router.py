from api import accounts, chat, health
from fastapi import APIRouter

router = APIRouter(prefix="/api")
router.include_router(health.router)
router.include_router(accounts.router)
router.include_router(chat.router)
