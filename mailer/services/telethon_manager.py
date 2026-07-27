"""Telethon session manager for mailing accounts."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    BadRequestError,
    ChatAdminRequiredError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UserBannedInChannelError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    InviteHashInvalidError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CreateForumTopicRequest, ForwardMessagesRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, User

from mailer.config import MailerConfig
from mailer.db import MailerDB

log = logging.getLogger(__name__)


def _safe_session_name(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone)
    return f"acc_{digits or 'unknown'}"


class PendingLogin:
    """In-memory state for multi-step Telethon login."""

    def __init__(self, client: TelegramClient, phone: str, phone_code_hash: str) -> None:
        self.client = client
        self.phone = phone
        self.phone_code_hash = phone_code_hash


class TelethonManager:
    def __init__(self, config: MailerConfig, db: MailerDB) -> None:
        self.config = config
        self.db = db
        self._clients: dict[int, TelegramClient] = {}
        self._pending: dict[int, PendingLogin] = {}  # admin_user_id -> pending
        self._lock = asyncio.Lock()

    def session_path(self, session_name: str) -> Path:
        return self.config.sessions_dir / session_name

    async def _load_api(self) -> tuple[int, str]:
        """Prefer DB-saved credentials, then env/config."""
        db_id = (await self.db.get_setting("tg_api_id", "")).strip()
        db_hash = (await self.db.get_setting("tg_api_hash", "")).strip()
        if db_id.isdigit() and db_hash:
            self.config.apply_api_from_values(db_id, db_hash)
        if not self.config.telethon_ready:
            raise RuntimeError(
                "API не заданы. Админ: меню → 🔑 API Telegram → ввести api_id и api_hash "
                "(https://my.telegram.org)"
            )
        return self.config.api_id, self.config.api_hash

    async def start_login(self, admin_id: int, phone: str) -> str:
        """Send login code. Returns human status."""
        api_id, api_hash = await self._load_api()
        phone = phone.strip().replace(" ", "")
        if not phone.startswith("+"):
            phone = "+" + phone

        # cancel previous pending for this admin
        await self.cancel_pending(admin_id)

        session_name = _safe_session_name(phone)
        client = TelegramClient(
            str(self.session_path(session_name)),
            api_id,
            api_hash,
        )
        await client.connect()
        result = await client.send_code_request(phone)
        self._pending[admin_id] = PendingLogin(client, phone, result.phone_code_hash)
        return f"Код отправлен на {phone}"

    async def confirm_code(self, admin_id: int, code: str) -> tuple[str, bool]:
        """
        Confirm SMS/Telegram code.
        Returns (message, needs_password).
        """
        pending = self._pending.get(admin_id)
        if not pending:
            raise RuntimeError("Нет активного входа. Начни заново: добавь аккаунт.")

        code = code.strip().replace(" ", "").replace("-", "")
        try:
            await pending.client.sign_in(
                phone=pending.phone,
                code=code,
                phone_code_hash=pending.phone_code_hash,
            )
        except SessionPasswordNeededError:
            return ("Нужен пароль 2FA. Отправь пароль облака Telegram.", True)
        except PhoneCodeInvalidError:
            raise RuntimeError("Неверный код. Попробуй ещё раз.")
        except PhoneCodeExpiredError:
            await self.cancel_pending(admin_id)
            raise RuntimeError("Код истёк. Начни добавление аккаунта заново.")

        return await self._finalize_login(admin_id, pending, label="")

    async def confirm_password(self, admin_id: int, password: str) -> tuple[str, bool]:
        pending = self._pending.get(admin_id)
        if not pending:
            raise RuntimeError("Нет активного входа. Начни заново.")
        try:
            await pending.client.sign_in(password=password.strip())
        except Exception as e:
            raise RuntimeError(f"Пароль не принят: {e}") from e
        return await self._finalize_login(admin_id, pending, label="")

    async def _finalize_login(
        self, admin_id: int, pending: PendingLogin, label: str
    ) -> tuple[str, bool]:
        me = await pending.client.get_me()
        name = " ".join(
            x for x in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if x
        ) or pending.phone
        if me.username:
            name = f"{name} (@{me.username})"

        session_name = _safe_session_name(pending.phone)
        existing = await self.db.get_account_by_phone(pending.phone)
        if existing:
            account_id = existing["id"]
            await self.db.set_account_status(account_id, "active", error=None)
            await self.db.db.execute(
                "UPDATE accounts SET label = ?, session_name = ?, added_by = COALESCE(added_by, ?) WHERE id = ?",
                (label or name, session_name, admin_id, account_id),
            )
            await self.db.db.commit()
        else:
            account_id = await self.db.add_account(
                phone=pending.phone,
                session_name=session_name,
                label=label or name,
                added_by=admin_id,
            )

        # disconnect pending client; engine will reconnect by session file
        try:
            await pending.client.disconnect()
        except Exception:
            pass
        self._pending.pop(admin_id, None)

        # warm connect into pool
        await self.get_client(account_id)
        return (f"✅ Аккаунт добавлен: {name}\nID: {account_id}", False)

    async def cancel_pending(self, admin_id: int) -> None:
        pending = self._pending.pop(admin_id, None)
        if pending:
            try:
                await pending.client.disconnect()
            except Exception:
                pass

    async def get_client(self, account_id: int) -> TelegramClient:
        async with self._lock:
            if account_id in self._clients:
                client = self._clients[account_id]
                if client.is_connected():
                    return client
            acc = await self.db.get_account(account_id)
            if not acc:
                raise RuntimeError(f"Account {account_id} not found")
            api_id, api_hash = await self._load_api()
            client = TelegramClient(
                str(self.session_path(acc["session_name"])),
                api_id,
                api_hash,
            )
            await client.connect()
            if not await client.is_user_authorized():
                await self.db.set_account_status(account_id, "error", "session not authorized")
                raise RuntimeError(f"Session not authorized for account {account_id}")
            self._clients[account_id] = client
            return client

    async def disconnect_account(self, account_id: int) -> None:
        client = self._clients.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def disconnect_all(self) -> None:
        for admin_id in list(self._pending.keys()):
            await self.cancel_pending(admin_id)
        for aid in list(self._clients.keys()):
            await self.disconnect_account(aid)

    async def _send_in_forum_topic(
        self, client: TelegramClient, entity: Any, text: str,
        source_chat_id: int | None = None, source_message_id: int | None = None,
    ):
        """Send to an open forum topic, creating one when needed."""
        async def topic_ids() -> list[int]:
            messages = await client.get_messages(entity, limit=100)
            ids: list[int] = []
            for item in messages:
                reply = getattr(item, "reply_to", None)
                top_id = getattr(reply, "reply_to_top_id", None)
                action = getattr(item, "action", None)
                if top_id:
                    candidate = int(top_id)
                elif action and action.__class__.__name__ == "MessageActionTopicCreate":
                    candidate = int(item.id)
                else:
                    continue
                if candidate not in ids:
                    ids.append(candidate)
            return ids

        async def try_topics(ids: list[int]):
            for topic_id in ids:
                try:
                    if source_chat_id is not None and source_message_id is not None:
                        updates = await client(ForwardMessagesRequest(
                            from_peer=source_chat_id, id=[source_message_id],
                            to_peer=entity, top_msg_id=topic_id,
                        ))
                        for update in getattr(updates, "updates", []):
                            forwarded = getattr(update, "message", None)
                            if forwarded is not None:
                                return forwarded
                        latest = await client.get_messages(entity, limit=1)
                        return latest[0] if latest else None
                    return await client.send_message(
                        entity, text, reply_to=topic_id,
                        parse_mode=("html" if "<tg-emoji " in text else None),
                    )
                except Exception as exc:
                    if "TOPIC_CLOSED" not in str(exc):
                        raise
            return None

        msg = await try_topics(await topic_ids())
        if msg is not None:
            return msg
        try:
            await client(CreateForumTopicRequest(entity, title="Broadcast"))
            await asyncio.sleep(0.5)
            return await try_topics(await topic_ids())
        except Exception as exc:
            log.warning("could not create forum topic for %s: %s", entity, exc)
            return None
    async def send_to_group(
        self,
        account_id: int,
        chat_id: int,
        text: str,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a text or forward a source message as account into a group."""
        client = await self.get_client(account_id)
        try:
            entity = await client.get_entity(chat_id)
            source_peer = source_chat_id
            if source_chat_id is not None and source_message_id is not None:
                try:
                    source_peer = await client.get_entity(source_chat_id)
                except (TypeError, ValueError) as exc:
                    # The account cannot resolve the source chat. Fall back to
                    # the HTML representation so custom emoji are retained.
                    log.warning("source chat unavailable for account=%s chat=%s: %s", account_id, source_chat_id, exc)
                    source_chat_id = None
                    source_message_id = None
            try:
                if source_chat_id is not None and source_message_id is not None:
                    forwarded = await client.forward_messages(
                        entity, source_message_id, from_peer=source_peer
                    )
                    msg = forwarded[0] if isinstance(forwarded, (list, tuple)) else forwarded
                else:
                    msg = await client.send_message(entity, text, parse_mode=("html" if "<tg-emoji " in text else None))
            except BadRequestError as exc:
                if "TOPIC_CLOSED" not in str(exc):
                    raise
                msg = await self._send_in_forum_topic(
                    client, entity, text, source_chat_id, source_message_id
                )
                if msg is None:
                    raise
            title = getattr(entity, "title", None) or str(chat_id)
            return {
                "ok": True,
                "message_id": msg.id,
                "title": title,
                "chat_id": chat_id,
            }
        except FloodWaitError as e:
            return {"ok": False, "error": f"FloodWait {e.seconds}s", "flood_wait": e.seconds}
        except (
            ChatWriteForbiddenError,
            ChatAdminRequiredError,
            ChatForbiddenError,
            ChannelPrivateError,
            UserBannedInChannelError,
        ) as e:
            # Telegram rejected this account for this target; retrying every
            # cycle only creates a permanent failure loop.
            return {"ok": False, "error": str(e), "write_forbidden": True}
        except BadRequestError as e:
            # Some permanent target failures are returned as generic 400 errors,
            # e.g. TOPIC_CLOSED, instead of a dedicated Telethon exception.
            error = str(e)
            permanent_markers = (
                "TOPIC_CLOSED",
                "CHAT_WRITE_FORBIDDEN",
                "CHAT_FORBIDDEN",
                "USER_BANNED_IN_CHANNEL",
                "CHANNEL_PRIVATE",
                "CHANNEL_INVALID",
                "PEER_ID_INVALID",
                "You can't write in this chat",
            )
            if "TOPIC_CLOSED" in error:
                return {"ok": False, "error": error, "topic_closed": True}
            if any(marker in error for marker in permanent_markers):
                return {"ok": False, "error": error, "write_forbidden": True}
            return {"ok": False, "error": error}
        except Exception as e:
            log.exception("send_to_group failed account=%s chat=%s", account_id, chat_id)
            return {"ok": False, "error": str(e)}

    async def join_group(self, account_id: int, ref: str) -> dict[str, Any]:
        """Join a public group/channel or invite link for one account."""
        client = await self.get_client(account_id)
        ref = (ref or "").strip()
        try:
            if "t.me/+" in ref or "t.me/joinchat/" in ref:
                invite_hash = ref.rstrip("/").split("/")[-1].split("+")[-1]
                await client(ImportChatInviteRequest(invite_hash))
                return {"ok": True, "already": False}

            entity_ref: Any = int(ref) if re.fullmatch(r"-?\d+", ref) else ref.lstrip("@")
            entity = await client.get_entity(entity_ref)
            if isinstance(entity, User):
                return {"ok": False, "permanent": True, "error": "target is a user"}
            if isinstance(entity, Channel):
                await client(JoinChannelRequest(entity))
                return {"ok": True, "already": False}
            # Basic chats returned here are already in the account's dialogs.
            return {"ok": True, "already": True}
        except UserAlreadyParticipantError:
            return {"ok": True, "already": True}
        except FloodWaitError as exc:
            return {"ok": False, "flood_wait": int(exc.seconds), "error": f"FloodWait {exc.seconds}s"}
        except (InviteHashExpiredError, InviteHashInvalidError, ChannelPrivateError) as exc:
            return {"ok": False, "permanent": True, "error": str(exc)}
        except Exception as exc:
            log.warning("join_group failed account=%s ref=%s: %s", account_id, ref, exc)
            return {"ok": False, "error": str(exc)}

    async def resolve_group(self, account_id: int, ref: str) -> dict[str, Any]:
        """
        Resolve group by username, invite link, or numeric id using account dialogs.
        """
        client = await self.get_client(account_id)
        ref = ref.strip()
        entity = None

        # numeric chat id
        if re.fullmatch(r"-?\d+", ref):
            entity = await client.get_entity(int(ref))
        else:
            # username or t.me link
            ref = ref.replace("https://t.me/", "").replace("http://t.me/", "")
            ref = ref.replace("t.me/", "").lstrip("@")
            if ref.startswith("+") or "joinchat" in ref:
                # invite hash — try join
                from telethon.tl.functions.messages import ImportChatInviteRequest
                from telethon.tl.functions.messages import CheckChatInviteRequest

                hash_part = ref.split("+")[-1].split("/")[-1]
                try:
                    await client(ImportChatInviteRequest(hash_part))
                except Exception:
                    pass
                inv = await client(CheckChatInviteRequest(hash_part))
                entity = getattr(inv, "chat", None) or inv
            else:
                entity = await client.get_entity(ref)

        if isinstance(entity, User):
            raise RuntimeError("Это пользователь, а не группа/канал")

        chat_id = entity.id
        # Telethon often returns positive id for channels; normalize to bot-style when possible
        if isinstance(entity, Channel):
            # full id for channels/supergroups: -100xxxxxxxxxx
            full_id = int(f"-100{entity.id}")
            chat_id = full_id
        elif isinstance(entity, Chat):
            chat_id = -entity.id if entity.id > 0 else entity.id

        title = getattr(entity, "title", "") or str(chat_id)
        username = getattr(entity, "username", "") or ""
        return {"chat_id": chat_id, "title": title, "username": username or ""}
