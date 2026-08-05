from fastapi import APIRouter

from contentai.api import admin, agents, auth, chat, health

router = APIRouter(prefix="/api")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(admin.router)
router.include_router(agents.router)
router.include_router(chat.router)
