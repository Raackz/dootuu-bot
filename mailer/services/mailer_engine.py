"""Background mailing loop: accounts → groups, cycles + cooldown."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot

from mailer.db import MailerDB
from mailer.services.telethon_manager import TelethonManager

log = logging.getLogger(__name__)


def _fmt_duration(sec: int) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h} ч")
    if m or h:
        parts.append(f"{m} мин")
    if not h and not m:
        parts.append(f"{s} сек")
    return " ".join(parts)


def _fmt_clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


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
        self._group_rr: dict[int, int] = {}  # account_id -> rr index
        self._account_rr: int = 0

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
                did_work, delay = await self._tick()
                await asyncio.sleep(delay if did_work else 3.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mailer loop error")
                await asyncio.sleep(5)

    async def _tick(self) -> tuple[bool, float]:
        """One send attempt. Returns (did_work, sleep_seconds)."""
        accounts = await self.db.list_accounts()
        groups = await self.db.list_groups(only_active=True)
        if not accounts or not groups:
            return False, 3.0

        ready: list[dict] = []
        for acc in accounts:
            if acc["status"] in ("disabled", "error"):
                continue
            was_cooldown = acc["status"] == "cooldown"
            if not await self.db.maybe_end_cooldown(acc["id"]):
                continue
            acc = await self.db.get_account(acc["id"])
            if not acc:
                continue
            # Новый круг после паузы
            if was_cooldown and acc["status"] == "active":
                await self._notify_cycle_started(acc)
            text = await self.db.account_message_text(acc)
            if not text:
                continue
            ready.append(acc)

        if not ready:
            return False, 3.0

        idx = self._account_rr % len(ready)
        account = ready[idx]
        self._account_rr = (idx + 1) % max(len(ready), 1)

        aid = int(account["id"])
        g_idx = self._group_rr.get(aid, 0) % len(groups)
        group = groups[g_idx]
        self._group_rr[aid] = (g_idx + 1) % len(groups)

        text = await self.db.account_message_text(account)
        delay = await self.db.account_delay(account)
        cycle_limit = await self.db.account_cycle_limit(account)

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
                status="✅ Успешно",
                preview=preview,
                extra=None,
                cycle_limit=cycle_limit,
            )
            if (updated or {}).get("status") == "cooldown":
                await self._notify_cycle_finished(updated or account)
            return True, delay

        err = result.get("error") or "неизвестная ошибка"
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
            await self.db.db.execute(
                "UPDATE accounts SET status = 'cooldown', next_cycle_at = ?, last_error = ? WHERE id = ?",
                (time.time() + int(flood) + 5, err, account["id"]),
            )
            await self.db.db.commit()
            await self._notify_log_raw(
                f"⏳ <b>Пауза из‑за лимита Telegram</b>\n"
                f"Аккаунт: <b>{_esc(account.get('label') or account.get('phone') or '?')}</b>\n"
                f"Ждём: {_fmt_duration(int(flood) + 5)}\n"
                f"Причина: <code>{_esc(err)}</code>"
            )
        else:
            await self.db.set_account_status(account["id"], "active", error=err)

        await self._notify_log(
            account=account,
            group=group,
            status="❌ Ошибка",
            preview=preview,
            extra=err,
            cycle_limit=cycle_limit,
        )
        return True, delay

    async def _notify_cycle_finished(self, account: dict) -> None:
        pause = await self.db.account_cycle_pause(account)
        limit = await self.db.account_cycle_limit(account)
        next_at = account.get("next_cycle_at") or (time.time() + pause)
        label = account.get("label") or account.get("phone") or "?"
        text = (
            f"⏹ <b>Круг рассылки завершён</b>\n\n"
            f"Аккаунт: <b>{_esc(str(label))}</b>\n"
            f"Отправлено сообщений в этом круге: <b>{limit}</b>\n"
            f"Пауза до следующего круга: <b>{_fmt_duration(pause)}</b>\n"
            f"Следующий круг примерно: <b>{_fmt_clock(float(next_at))}</b>\n\n"
            f"Аккаунт на паузе, рассылка с него временно остановлена."
        )
        await self._notify_log_raw(text)

    async def _notify_cycle_started(self, account: dict) -> None:
        limit = await self.db.account_cycle_limit(account)
        label = account.get("label") or account.get("phone") or "?"
        text = (
            f"▶️ <b>Новый круг рассылки начат</b>\n\n"
            f"Аккаунт: <b>{_esc(str(label))}</b>\n"
            f"Лимит сообщений в круге: <b>{limit}</b>\n"
            f"Время: {_fmt_clock(time.time())}"
        )
        await self._notify_log_raw(text)

    async def _notify_log(
        self,
        *,
        account: dict,
        group: dict,
        status: str,
        preview: str,
        extra: str | None,
        cycle_limit: int,
    ) -> None:
        label = account.get("label") or account.get("phone") or "?"
        gtitle = group.get("title") or group.get("chat_id")
        sent = int(account.get("sent_in_cycle") or 0)
        text = (
            f"{status}\n"
            f"Аккаунт: <b>{_esc(label)}</b>\n"
            f"Группа: <b>{_esc(str(gtitle))}</b>\n"
            f"ID чата: <code>{group.get('chat_id')}</code>\n"
            f"Прогресс круга: <b>{sent}/{cycle_limit}</b>\n"
            f"Текст: {_esc(preview)}"
        )
        if extra:
            text += f"\nОшибка: <code>{_esc(extra)}</code>"
        await self._notify_log_raw(text)

    async def _notify_log_raw(self, html: str) -> None:
        log_id = await self.db.get_setting("log_group_id", "")
        if not log_id:
            log.info("cycle notify (no log group): %s", html[:200])
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
