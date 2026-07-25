from aiogram import Router

from mailer.handlers.admin import router as admin_router


def setup_routers() -> Router:
    root = Router()
    root.include_router(admin_router)
    return root
