"""Background mailing loop: accounts → groups, cycles + cooldown."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aiogram import Bot

from mailer.db import MailerDB
from mailer.services.telethon_manager import TelethonManager

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class MailerEngine:
    def __init__(
        self,
        db: MailerDB,
        telethon: TelethonManager,
        bot: Bot,
    ) -> None:
        self.db = db
        self.telethon = telethon
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._group_rr: int = 0  # round-robin index

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="mailer-engine")
        log.info("Mailer engine started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Mailer engine stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not await self.db.is_mailing_enabled():
                    await asyncio.sleep(2)
                    continue
                did_work = await self._tick()
                delay = await self.db.get_float("delay_sec", 8.0)
                await asyncio.sleep(delay if did_work else 3.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mailer loop error")
                await asyncio.sleep(5)

    async def _tick(self) -> bool:
        """One send attempt. Returns True if a send was tried."""
        accounts = await self.db.list_accounts()
        groups = await self.db.list_groups(only_active=True)
        msg = await self.db.get_active_message()

        if not accounts or not groups or not msg or not (msg.get("text") or "").strip():
            return False

        # pick first ready account
        account = None
        for acc in accounts:
            if acc["status"] in ("disabled", "error"):
                continue
            ready = await self.db.maybe_end_cooldown(acc["id"])
            if ready:
                account = await self.db.get_account(acc["id"])
                break
            # still in cooldown — skip
        if not account:
            return False

        # round-robin group
        groups = await self.db.list_groups(only_active=True)
        if not groups:
            return False
        idx = self._group_rr % len(groups)
        group = groups[idx]
        self._group_rr = (idx + 1) % len(groups)

        text = msg["text"]
        result = await self.telethon.send_to_group(
            account["id"], int(group["chat_id"]), text
        )

        preview = (text[:80] + "…") if len(text) > 80 else text
        if result.get("ok"):
            updated = await self.db.mark_sent(account["id"])
            await self.db.touch_group_sent(group["id"])
            await self.db.log_send(
                account_id=account["id"],
                group_id=group["id"],
                group_title=group.get("title") or result.get("title") or "",
                group_chat_id=int(group["chat_id"]),
                message_preview=preview,
                status="ok",
            )
            await self._notify_log(
                account=updated or account,
                group=group,
                status="✅ OK",
                preview=preview,
                extra=None,
            )
            if (updated or {}).get("status") == "cooldown":
                pause = await self.db.get_int("cycle_pause_sec", 3600)
                limit = await self.db.get_int("cycle_limit", 50)
                await self._notify_log_raw(
                    f"⏸ <b>Круг завершён</b>\n"
                    f"Аккаунт: <b>{account.get('label') or account['phone']}</b>\n"
                    f"Отправлено в круге: {limit}\n"
                    f"Пауза: {pause // 60} мин\n"
                    f"Следующий круг: через {pause // 60} мин"
                )
            return True

        err = result.get("error") or "unknown"
        await self.db.log_send(
            account_id=account["id"],
            group_id=group["id"],
            group_title=group.get("title") or "",
            group_chat_id=int(group["chat_id"]),
            message_preview=preview,
            status="fail",
            error=err,
        )
        flood = result.get("flood_wait")
        if flood:
            # soft cooldown on flood
            await self.db.db.execute(
                "UPDATE accounts SET status = 'cooldown', next_cycle_at = ?, last_error = ? WHERE id = ?",
                (time.time() + int(flood) + 5, err, account["id"]),
            )
            await self.db.db.commit()
        else:
            await self.db.set_account_status(account["id"], "active", error=err)

        await self._notify_log(
            account=account,
            group=group,
            status="❌ FAIL",
            preview=preview,
            extra=err,
        )
        return True

    async def _notify_log(
        self,
        *,
        account: dict,
        group: dict,
        status: str,
        preview: str,
        extra: str | None,
    ) -> None:
        label = account.get("label") or account.get("phone") or "?"
        gtitle = group.get("title") or group.get("chat_id")
        cycle_limit = await self.db.get_int("cycle_limit", 50)
        sent = int(account.get("sent_in_cycle") or 0)
        text = (
            f"{status}\n"
            f"Аккаунт: <b>{_esc(label)}</b>\n"
            f"Группа: <b>{_esc(str(gtitle))}</b>\n"
            f"<code>{group.get('chat_id')}</code>\n"
            f"Круг: {sent}/{cycle_limit}\n"
            f"Текст: {_esc(preview)}"
        )
        if extra:
            text += f"\nОшибка: <code>{_esc(extra)}</code>"
        await self._notify_log_raw(text)

    async def _notify_log_raw(self, html: str) -> None:
        log_id = await self.db.get_setting("log_group_id", "")
        if not log_id:
            return
        try:
            chat_id = int(log_id)
        except ValueError:
            return
        try:
            await self.bot.send_message(chat_id, html, parse_mode="HTML")
        except Exception as e:
            log.warning("log group notify failed: %s", e)


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
