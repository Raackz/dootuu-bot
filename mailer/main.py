"""Entry point: control bot (aiogram) + mailing engine (Telethon accounts)."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from mailer.config import MailerConfig
from mailer.db import MailerDB
from mailer.handlers import setup_routers
from mailer.services.mailer_engine import MailerEngine
from mailer.services.telethon_manager import TelethonManager

_LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "mailer"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "mailer.log", encoding="utf-8", delay=True),
    ],
)
log = logging.getLogger("mailer")


async def _run() -> None:
    config = MailerConfig()
    if not config.bot_token:
        log.error("MAILER_BOT_TOKEN or BOT_TOKEN is required")
        sys.exit(1)
    if not config.telethon_ready:
        log.warning(
            "TG_API_ID / TG_API_HASH missing — account login will fail until set "
            "(https://my.telegram.org)"
        )

    db = MailerDB(config.db_path)
    await db.connect()

    # seed numeric defaults only when empty string
    if (await db.get_setting("cycle_limit", "")) == "":
        await db.set_setting("cycle_limit", str(config.default_cycle_limit))
    if (await db.get_setting("cycle_pause_sec", "")) == "":
        await db.set_setting("cycle_pause_sec", str(config.default_cycle_pause_sec))
    if (await db.get_setting("delay_sec", "")) == "":
        await db.set_setting("delay_sec", str(config.default_delay_sec))

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    telethon = TelethonManager(config, db)
    engine = MailerEngine(db, telethon, bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp["mailer_config"] = config
    dp["mailer_db"] = db
    dp["mailer_telethon"] = telethon
    dp["mailer_engine"] = engine
    dp.include_router(setup_routers())

    if await db.is_mailing_enabled():
        engine.start()
        log.info("Resumed mailing engine (was enabled in DB)")

    me = await bot.get_me()
    log.info("Mailer control bot @%s started", me.username)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            mailer_config=config,
            mailer_db=db,
            mailer_telethon=telethon,
            mailer_engine=engine,
        )
    finally:
        await engine.stop()
        await telethon.disconnect_all()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Stopped by user")


if __name__ == "__main__":
    main()
