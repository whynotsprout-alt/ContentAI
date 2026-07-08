from api import accounts, health
from api.chat import message, session, stream
from fastapi import APIRouter

router = APIRouter(prefix="/api")
router.include_router(health.router)
router.include_router(accounts.router)
router.include_router(session.router)
router.include_router(message.router)
router.include_router(stream.router)
