"""Background mailing loop — per-account groups & log groups (client isolation).

Public / log-facing strings are English (US audience).
Admin bot UI stays Russian in handlers.
"""

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
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    if not h and not m:
        parts.append(f"{s}s")
    return " ".join(parts)


def _fmt_clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")


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
        self._empty_text_warned: set[int] = set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._empty_text_warned.clear()
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
                # auto-stop when selected duration expires
                if await self.db.check_and_expire_mailing():
                    log.info("Mailing auto-stopped: duration expired")
                    await self._notify_mailing_ended()
                    await asyncio.sleep(2)
                    continue
                did_work, delay = await self._tick()
                await asyncio.sleep(delay if did_work else 3.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mailer loop error")
                await asyncio.sleep(5)

    async def _notify_mailing_ended(self) -> None:
        """Tell every active log group that the timed broadcast finished."""
        text = (
            "⏹ <b>Broadcast ended</b>\n\n"
            "Scheduled duration expired. Mailing has been stopped automatically.\n"
            f"Time: {_fmt_clock(time.time())}"
        )
        logs = await self.db.list_log_groups(only_active=True)
        chat_ids = [int(lg["chat_id"]) for lg in logs]
        if not chat_ids:
            legacy = (await self.db.get_setting("log_group_id", "")).strip()
            if legacy.lstrip("-").isdigit():
                chat_ids = [int(legacy)]
        for chat_id in chat_ids:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                log.warning("mailing-ended notify %s failed: %s", chat_id, e)

    async def _tick(self) -> tuple[bool, float]:
        """One send for one ready account into its own group list."""
        accounts = await self.db.list_accounts()
        if not accounts:
            return False, 3.0

        ready: list[dict] = []
        for acc in accounts:
            if acc["status"] in ("disabled", "error", "expired"):
                continue
            was_cooldown = acc["status"] == "cooldown"
            if await self.db.account_duration_expired(acc):
                await self.db.set_account_status(acc["id"], "expired", error="account term expired")
                await self._notify_account_duration_ended(acc)
                continue
            if not await self.db.maybe_end_cooldown(acc["id"]):
                continue
            acc = await self.db.get_account(acc["id"])
            if not acc:
                continue
            if was_cooldown and acc["status"] == "active":
                await self._notify_cycle_started(acc)
            text = await self.db.account_message_text(acc)
            groups = await self.db.list_account_groups(acc["id"], only_active=True)
            if not groups:
                continue
            if not text:
                aid = int(acc["id"])
                if aid not in self._empty_text_warned:
                    await self._notify_empty_text(acc, groups)
                    self._empty_text_warned.add(aid)
                continue
            self._empty_text_warned.discard(int(acc["id"]))
            ready.append(acc)

        if not ready:
            return False, 3.0

        # rotate among accounts that have their own work
        idx = self._account_rr % len(ready)
        account = ready[idx]
        self._account_rr = (idx + 1) % max(len(ready), 1)

        # only THIS account's groups
        groups = await self.db.list_account_groups(account["id"], only_active=True)
        if not groups:
            return False, 3.0

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
                status="✅ Sent",
                preview=preview,
                extra=None,
                cycle_limit=cycle_limit,
            )
            if (updated or {}).get("status") == "cooldown":
                await self._notify_cycle_finished(updated or account)
            return True, delay

        err = result.get("error") or "unknown error"
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
            await self._notify_account(
                account["id"],
                f"⏳ <b>Paused (Telegram rate limit)</b>\n"
                f"Account: <b>{_esc(account.get('label') or account.get('phone') or '?')}</b>\n"
                f"Client: <b>{_esc(account.get('client_label') or '—')}</b>\n"
                f"Wait: {_fmt_duration(int(flood) + 5)}\n"
                f"Reason: <code>{_esc(err)}</code>",
            )
        else:
            await self.db.set_account_status(account["id"], "active", error=err)

        await self._notify_log(
            account=account,
            group=group,
            status="❌ Failed",
            preview=preview,
            extra=err,
            cycle_limit=cycle_limit,
        )
        return True, delay

    def _client_line(self, account: dict) -> str:
        cl = (account.get("client_label") or "").strip()
        if cl:
            return f"Client: <b>{_esc(cl)}</b>\n"
        return ""

    async def _notify_cycle_finished(self, account: dict) -> None:
        pause = await self.db.account_cycle_pause(account)
        limit = await self.db.account_cycle_limit(account)
        next_at = account.get("next_cycle_at") or (time.time() + pause)
        label = account.get("label") or account.get("phone") or "?"
        text = (
            f"⏹ <b>Broadcast cycle finished</b>\n\n"
            f"Account: <b>{_esc(str(label))}</b>\n"
            f"{self._client_line(account)}"
            f"Messages sent this cycle: <b>{limit}</b>\n"
            f"Cooldown until next cycle: <b>{_fmt_duration(pause)}</b>\n"
            f"Next cycle around: <b>{_fmt_clock(float(next_at))}</b>\n\n"
            f"This account is on cooldown; sending is paused until the next cycle."
        )
        await self._notify_account(account["id"], text)

    async def _notify_cycle_started(self, account: dict) -> None:
        limit = await self.db.account_cycle_limit(account)
        label = account.get("label") or account.get("phone") or "?"
        text = (
            f"▶️ <b>New broadcast cycle started</b>\n\n"
            f"Account: <b>{_esc(str(label))}</b>\n"
            f"{self._client_line(account)}"
            f"Cycle message limit: <b>{limit}</b>\n"
            f"Time: {_fmt_clock(time.time())}"
        )
        await self._notify_account(account["id"], text)

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
            f"Account: <b>{_esc(label)}</b>\n"
            f"{self._client_line(account)}"
            f"Group: <b>{_esc(str(gtitle))}</b>\n"
            f"Chat ID: <code>{group.get('chat_id')}</code>\n"
            f"Cycle progress: <b>{sent}/{cycle_limit}</b>\n"
            f"Text: {_esc(preview)}"
        )
        if extra:
            text += f"\nError: <code>{_esc(extra)}</code>"
        await self._notify_account(account["id"], text)

    async def _notify_account_duration_ended(self, account: dict) -> None:
        label = account.get("label") or account.get("phone") or "?"
        await self._notify_account(
            account["id"],
            "⏹ <b>Account term expired</b>\n"
            f"Account: <b>{_esc(str(label))}</b>\n"
            "This account was stopped automatically; other accounts continue.",
        )

    async def _notify_empty_text(self, account: dict, groups: list[dict]) -> None:
        """Report an empty configured message without sending it."""
        label = account.get("label") or account.get("phone") or "?"
        group_names = ", ".join(str(g.get("title") or g.get("chat_id")) for g in groups)
        for group in groups:
            await self.db.log_send(
                account_id=account["id"], group_id=group["id"],
                group_title=group.get("title") or "", group_chat_id=int(group["chat_id"]),
                message_preview="", status="skipped", error="empty_message",
            )
        await self._notify_account(
            account["id"],
            "⚠️ <b>Sending skipped</b>\n"
            f"Account: <b>{_esc(str(label))}</b>\n"
            f"Targets: <b>{_esc(group_names)}</b>\n"
            "Text: <i>(empty — nothing was sent)</i>\n"
            "Set an account message or activate a non-empty template.",
        )

    async def _notify_account(self, account_id: int, html: str) -> None:
        """Send to all log groups linked to this account."""
        chat_ids = await self.db.account_log_chat_ids(account_id)
        if not chat_ids:
            log.info("notify account=%s (no log groups): %s", account_id, html[:160])
            return
        for chat_id in chat_ids:
            try:
                await self.bot.send_message(chat_id, html, parse_mode="HTML")
            except Exception as e:
                log.warning("log group %s notify failed: %s", chat_id, e)


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
