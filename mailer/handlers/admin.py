"""Admin control panel for the mailer bot."""

from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from mailer.config import MailerConfig
from mailer.db import MailerDB
from mailer import keyboards as kb
from mailer.services.mailer_engine import MailerEngine
from mailer.services.telethon_manager import TelethonManager
from mailer.states import (
    AccountConfigStates,
    AddAccountStates,
    AddGroupStates,
    AddLogGroupStates,
    ClientLabelStates,
    MessageStates,
    SettingsStates,
    TeamStates,
)

log = logging.getLogger(__name__)
router = Router(name="mailer_admin")


async def _is_allowed(
    event: Message | CallbackQuery,
    config: MailerConfig,
    db: MailerDB | None = None,
) -> bool:
    """Only ADMIN_IDS / ADMIN_USERNAMES (or MAILER_OPEN=true)."""
    user = event.from_user
    if not user:
        return False
    # operators table is informational only — access = env admins
    _ = db
    return config.is_admin(user.id, user.username)


async def _touch_operator(event: Message | CallbackQuery, db: MailerDB) -> None:
    """Remember who uses the bot (team list)."""
    user = event.from_user
    if not user:
        return
    name = " ".join(x for x in [user.first_name, user.last_name] if x) or ""
    await db.upsert_operator(user.id, user.username or "", name)


async def _deny(event: Message | CallbackQuery) -> None:
    user = event.from_user
    uid = user.id if user else "?"
    uname = f"@{user.username}" if user and user.username else "—"
    text = (
        f"⛔ Нет доступа.\n"
        f"Твой ID: <code>{uid}</code>\n"
        f"Username: {uname}\n\n"
        f"Доступ только у администратора."
    )
    log.warning("access denied user_id=%s username=%s", uid, uname)
    if isinstance(event, CallbackQuery):
        await event.answer(f"Нет доступа. ID: {uid}", show_alert=True)
        if event.message:
            await event.message.answer(text, parse_mode="HTML")
    else:
        await event.answer(text, parse_mode="HTML")


def _fmt_left_ru(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def _fmt_duration_choice(sec: int) -> str:
    if sec <= 0:
        return "без лимита"
    if sec < 3600:
        return f"{sec // 60} мин"
    h = sec // 3600
    return f"{h} ч" if h != 1 else "1 час"


async def _main_text(db: MailerDB, config: MailerConfig | None = None) -> str:
    st = await db.stats()
    cycle = await db.get_int("cycle_limit", 50)
    pause = await db.get_int("cycle_pause_sec", 3600)
    delay = await db.get_float("delay_sec", 8)
    dur_def = await db.get_mailing_duration_default()
    logs_n = len(await db.list_log_groups(only_active=True))
    mail = "🟢 ВКЛ" if st["mailing"] else "🔴 ВЫКЛ"
    left = await db.mailing_time_left() if st["mailing"] else None
    if st["mailing"] and left is not None:
        mail += f" · осталось <b>{_fmt_left_ru(left)}</b>"
    elif st["mailing"]:
        mail += " · без лимита"
    api_ok = "✅ задан" if (config and config.telethon_ready) else "❌ не задан → «🔑 API Telegram»"
    open_mode = "да" if (config and config.allow_all) else "только админ"
    return (
        f"<b>Mailer — панель</b>\n\n"
        f"Рассылка: <b>{mail}</b>\n"
        f"Срок (настр.): <b>{_fmt_duration_choice(dur_def)}</b>\n"
        f"Аккаунты (клиенты): {st['accounts']} "
        f"(active {st['accounts_active']}, cooldown {st['accounts_cooldown']})\n"
        f"Групп в пуле: {st['groups']} · лог-групп: {logs_n}\n"
        f"Отправок OK/FAIL: {st['sends_ok']}/{st['sends_fail']}\n"
        f"Круг (глоб.): <b>{cycle}</b> · пауза <b>{pause // 60}</b> мин · delay <b>{delay}</b>с\n"
        f"API: {api_ok}\n"
        f"Доступ: <b>{open_mode}</b>\n\n"
        f"Каждый аккаунт = отдельный клиент:\n"
        f"своё сообщение, свои группы, свои логи."
    )


async def _show_main(
    event: Message | CallbackQuery,
    state: FSMContext | None,
    db: MailerDB,
    telethon: TelethonManager,
    config: MailerConfig | None = None,
) -> None:
    if state:
        await state.clear()
    if isinstance(event, CallbackQuery) and event.from_user:
        await telethon.cancel_pending(event.from_user.id)
    await _touch_operator(event, db)
    mailing = await db.is_mailing_enabled()
    text = await _main_text(db, config)
    markup = kb.main_menu(mailing)
    if isinstance(event, CallbackQuery) and event.message:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
    elif isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


# ── entry ─────────────────────────────────────────────────────


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Anyone can request their Telegram id."""
    user = message.from_user
    if not user:
        return
    await message.answer(
        f"Твой ID: <code>{user.id}</code>\n"
        f"Username: @{user.username or '—'}\n\n"
        f"Скинь ID админу → Команда → Добавить по ID",
        parse_mode="HTML",
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_telethon: TelethonManager,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    await _show_main(message, state, mailer_db, mailer_telethon, mailer_config)


@router.callback_query(F.data == "menu:main")
async def cb_main(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_telethon: TelethonManager,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await _show_main(call, state, mailer_db, mailer_telethon, mailer_config)


# ── status ────────────────────────────────────────────────────


@router.callback_query(F.data == "menu:status")
async def cb_status(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    accounts = await mailer_db.list_accounts()
    lines = [await _main_text(mailer_db), "", "<b>Аккаунты:</b>"]
    if not accounts:
        lines.append("— нет —")
    for a in accounts:
        sent = a.get("sent_in_cycle") or 0
        limit = await mailer_db.account_cycle_limit(a)
        own = "✉" if (a.get("message_text") or "").strip() else "·"
        extra = ""
        if a["status"] == "cooldown" and a.get("next_cycle_at"):
            left = max(0, int(a["next_cycle_at"] - time.time()))
            extra = f", пауза ещё {left // 60}м {left % 60}с"
        lines.append(
            f"• {own} {a.get('label') or a['phone']}: <code>{a['status']}</code> "
            f"({sent}/{limit}{extra})"
        )
    logs = await mailer_db.recent_logs(5)
    lines.append("\n<b>Последние отправки:</b>")
    if not logs:
        lines.append("— пусто —")
    for row in logs:
        ts = time.strftime("%H:%M:%S", time.localtime(row["created_at"]))
        lines.append(
            f"{ts} [{row['status']}] {row.get('group_title') or row.get('group_chat_id')}"
        )
    mailing = await mailer_db.is_mailing_enabled()
    if call.message:
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=kb.main_menu(mailing),
            parse_mode="HTML",
        )
    await call.answer()


# ── mail start/stop (+ duration) ──────────────────────────────


async def _mail_ready_check(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> bool:
    """Validate accounts/API before showing duration or starting. False = blocked."""
    accounts = [a for a in await mailer_db.list_accounts() if a["status"] != "disabled"]
    if not accounts:
        await call.answer("Сначала добавь аккаунт", show_alert=True)
        return False
    ready = 0
    for a in accounts:
        text = await mailer_db.account_message_text(a)
        grps = await mailer_db.list_account_groups(a["id"], only_active=True)
        if text and grps:
            ready += 1
    if ready == 0:
        await call.answer(
            "Нет готовых аккаунтов: у каждого нужны своё сообщение + группы рассылки",
            show_alert=True,
        )
        return False
    db_id = (await mailer_db.get_setting("tg_api_id", "")).strip()
    db_hash = (await mailer_db.get_setting("tg_api_hash", "")).strip()
    if db_id or db_hash:
        mailer_config.apply_api_from_values(db_id or None, db_hash or None)
    if not mailer_config.telethon_ready:
        await call.answer("Сначала «🔑 API Telegram»", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "mail:start")
async def mail_start(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    """Step 1: pick how long the broadcast should run."""
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    # Show duration choices first; validate accounts/API on confirmation.
    dur_def = await mailer_db.get_mailing_duration_default()
    if call.message:
        await call.message.edit_text(
            "<b>▶️ Старт рассылки</b>\n\n"
            "Выбери <b>срок</b> — по истечении рассылка остановится сама.\n"
            "«Без лимита» — только ручной стоп.\n\n"
            f"Сейчас в настройках: <b>{_fmt_duration_choice(dur_def)}</b>\n"
            f"<i>Срок также: ⚙️ Настройки → 📅 Срок рассылки</i>",
            reply_markup=kb.mail_duration_menu(
                prefix="mail:dur",
                back="menu:main",
                selected=dur_def,
            ),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("mail:dur:"))
async def mail_start_with_duration(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_engine: MailerEngine,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    if not await _mail_ready_check(call, mailer_config, mailer_db):
        return
    try:
        duration = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    except (ValueError, AttributeError):
        await call.answer("Неверный срок", show_alert=True)
        return
    if duration < 0:
        await call.answer("Неверный срок", show_alert=True)
        return
    await mailer_db.start_mailing(duration if duration > 0 else None)
    mailer_engine.start()
    if duration > 0:
        await call.answer(f"Рассылка на {_fmt_left_ru(duration)}")
    else:
        await call.answer("Рассылка без лимита")
    if call.message:
        await call.message.edit_text(
            await _main_text(mailer_db, mailer_config),
            reply_markup=kb.main_menu(True),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "mail:stop")
async def mail_stop(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await mailer_db.set_mailing_enabled(False)
    await call.answer("Рассылка остановлена")
    if call.message:
        await call.message.edit_text(
            await _main_text(mailer_db, mailer_config),
            reply_markup=kb.main_menu(False),
            parse_mode="HTML",
        )


# ── accounts ──────────────────────────────────────────────────


@router.callback_query(F.data == "menu:accounts")
async def menu_accounts(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.clear()
    accounts = await mailer_db.list_accounts()
    text = (
        "<b>Аккаунты для рассылки</b>\n\n"
        "Любой из команды может добавить:\n"
        "номер (LZT) → код с сайта по ключу → 2FA.\n"
        "Аккаунт должен быть в целевых группах."
    )
    if call.message:
        await call.message.edit_text(
            text, reply_markup=kb.accounts_menu(accounts), parse_mode="HTML"
        )
    await call.answer()


@router.callback_query(F.data == "acc:add")
async def acc_add(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    # refresh API from DB (admin may have set via bot)
    db_id = (await mailer_db.get_setting("tg_api_id", "")).strip()
    db_hash = (await mailer_db.get_setting("tg_api_hash", "")).strip()
    if db_id or db_hash:
        mailer_config.apply_api_from_values(db_id or None, db_hash or None)
    if not mailer_config.telethon_ready:
        await call.answer("Сначала задай API в меню «🔑 API Telegram»", show_alert=True)
        if call.message:
            await call.message.answer(
                "❌ Нет <b>api_id / api_hash</b>.\n"
                "Админ: главное меню → <b>🔑 API Telegram</b> → вставить данные "
                "с https://my.telegram.org\n"
                "(это не ключ с LZT)",
                parse_mode="HTML",
            )
        return
    await state.set_state(AddAccountStates.phone)
    if call.message:
        await call.message.edit_text(
            "<b>Шаг 1/3 — номер</b>\n\n"
            "Пришли номер купленного аккаунта:\n"
            "<code>+79001234567</code>\n\n"
            "Ключ с LZT <b>в бота не кидай</b> — им смотри <b>код</b> в заказе на сайте, "
            "когда бот попросит на шаге 2.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(AddAccountStates.phone)
async def acc_phone(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_telethon: TelethonManager,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    phone = (message.text or "").strip()
    if len(phone) < 8:
        await message.answer("Не похоже на телефон. Пример: +79001234567")
        return
    try:
        status = await mailer_telethon.start_login(message.from_user.id, phone)
    except Exception as e:
        log.exception("start_login")
        await message.answer(f"Ошибка: {e}", reply_markup=kb.back_main())
        await state.clear()
        return
    await state.set_state(AddAccountStates.code)
    await message.answer(
        f"{status}\n\n"
        f"<b>Шаг 2/3 — код</b>\n"
        f"Открой заказ на LZT → по ключу возьми код (или SMS/Telegram) → "
        f"пришли сюда <b>только цифры кода</b>.",
        reply_markup=kb.cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddAccountStates.code)
async def acc_code(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_telethon: TelethonManager,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    try:
        text, need_pw = await mailer_telethon.confirm_code(
            message.from_user.id, message.text or ""
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return
    if need_pw:
        await state.set_state(AddAccountStates.password)
        await message.answer(
            f"<b>Шаг 3/3 — 2FA</b>\n{text}\n\n"
            f"Пришли облачный пароль (2FA), если продавец его дал.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
        return
    await state.clear()
    await message.answer(
        f"{text}\n\nДальше: Аккаунт → «Сообщение» и «Параметры», если нужно.",
        reply_markup=kb.back_main(),
    )


@router.message(AddAccountStates.password)
async def acc_password(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_telethon: TelethonManager,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    try:
        text, _ = await mailer_telethon.confirm_password(
            message.from_user.id, message.text or ""
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return
    await state.clear()
    await message.answer(
        f"{text}\n\nАккаунт готов. Можно задать своё сообщение в карточке аккаунта.",
        reply_markup=kb.back_main(),
    )


async def _fmt_acc_params(mailer_db: MailerDB, acc: dict) -> str:
    limit = await mailer_db.account_cycle_limit(acc)
    pause = await mailer_db.account_cycle_pause(acc)
    delay = await mailer_db.account_delay(acc)
    own_lim = acc.get("cycle_limit") is not None
    own_pause = acc.get("cycle_pause_sec") is not None
    own_delay = acc.get("delay_sec") is not None
    duration = await mailer_db.account_duration(acc)
    left = await mailer_db.account_time_left(acc)
    duration_label = "unlimited" if duration <= 0 else (_fmt_left_ru(int(left)) if left is not None else "expired")
    msg = await mailer_db.account_message_text(acc)
    own_msg = bool((acc.get("message_text") or "").strip())
    preview = (msg[:120] + "…") if len(msg) > 120 else msg
    if not preview:
        preview = "— не задано —"
    grps = await mailer_db.list_account_groups(acc["id"], only_active=True)
    logs = await mailer_db.list_account_log_groups(acc["id"])
    client = (acc.get("client_label") or "").strip() or "—"
    return (
        f"<b>Аккаунт #{acc['id']}</b> (отдельный клиент)\n"
        f"Клиент: <b>{_html_esc(client)}</b>\n"
        f"Имя: {acc.get('label')}\n"
        f"Телефон: <code>{acc['phone']}</code>\n"
        f"Статус: <code>{acc['status']}</code>\n"
        f"В круге: {acc.get('sent_in_cycle') or 0}/{limit}\n"
        f"Групп рассылки: <b>{len(grps)}</b> · лог-групп: <b>{len(logs)}</b>\n"
        f"Ошибка: {acc.get('last_error') or '—'}\n\n"
        f"<b>Сообщение</b> ({'своё' if own_msg else 'глобальный шаблон'}):\n"
        f"<code>{_html_esc(preview)}</code>\n\n"
        f"<b>Параметры</b>\n"
        f"• Лимит круга: <b>{limit}</b>"
        f"{'' if own_lim else ' <i>(глоб.)</i>'}\n"
        f"• Пауза: <b>{pause // 60}</b> мин"
        f"{'' if own_pause else ' <i>(глоб.)</i>'}\n"
        f"• Delay: <b>{delay}</b> сек"
        f"{'' if own_delay else ' <i>(глоб.)</i>'}"
        f"\nTerm: <b>{duration_label}</b>"
    )


def _message_content(message: Message) -> str:
    """Keep Telegram formatting, including Premium Emoji, as HTML."""
    return (
        getattr(message, "html_text", None)
        or getattr(message, "html_caption", None)
        or message.text
        or message.caption
        or ""
    )


def _html_esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@router.callback_query(F.data.startswith("acc:view:"))
async def acc_view(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    acc = await mailer_db.get_account(aid)
    if not acc:
        await call.answer("Не найден", show_alert=True)
        return
    text = await _fmt_acc_params(mailer_db, acc)
    if call.message:
        await call.message.edit_text(
            text,
            reply_markup=kb.account_card(aid, acc["status"]),
            parse_mode="HTML",
        )
    await call.answer()


# ── per-account message ───────────────────────────────────────


@router.callback_query(F.data.startswith("acc:msg:"))
async def acc_msg_menu(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    acc = await mailer_db.get_account(aid)
    if not acc:
        await call.answer("Не найден", show_alert=True)
        return
    own = (acc.get("message_text") or "").strip()
    body = own if own else "(пусто — будет глобальный шаблон из «Сообщения»)"
    if len(body) > 600:
        body = body[:600] + "…"
    if call.message:
        await call.message.edit_text(
            f"<b>Сообщение аккаунта #{aid}</b>\n\n{_html_esc(body)}",
            reply_markup=kb.account_msg_menu(aid),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("acc:msgedit:"))
async def acc_msg_edit(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(AccountConfigStates.message_text)
    await state.update_data(account_id=aid)
    if call.message:
        await call.message.edit_text(
            f"Пришли <b>текст сообщения</b> только для аккаунта #{aid}.\n"
            f"Он будет уходить из этого аккаунта вместо глобального шаблона.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(AccountConfigStates.message_text)
async def acc_msg_edit_text(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    aid = int(data["account_id"])
    text = _message_content(message)
    if not text.strip():
        await message.answer("Пустой текст")
        return
    await mailer_db.set_account_message(aid, text)
    await state.clear()
    await message.answer(
        f"✅ Сообщение для аккаунта #{aid} сохранено",
        reply_markup=kb.back_main(),
    )


@router.callback_query(F.data.startswith("acc:msgclear:"))
async def acc_msg_clear(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_account_message(aid, "")
    await call.answer("Очищено — будет глобальный шаблон")
    call.data = f"acc:msg:{aid}"
    await acc_msg_menu(call, mailer_config, mailer_db)


# ── per-account params ────────────────────────────────────────


@router.callback_query(F.data.startswith("acc:params:"))
async def acc_params(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    acc = await mailer_db.get_account(aid)
    if not acc:
        await call.answer("Не найден", show_alert=True)
        return
    if call.message:
        await call.message.edit_text(
            await _fmt_acc_params(mailer_db, acc)
            + "\n\nВыбери что изменить (0 = сброс на глобальное):",
            reply_markup=kb.account_params_menu(aid),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("acc:setduration:"))
async def acc_set_duration(call: CallbackQuery, mailer_config: MailerConfig, mailer_db: MailerDB) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    acc = await mailer_db.get_account(aid)
    current = await mailer_db.account_duration(acc or {})
    if call.message:
        await call.message.edit_text(
            "<b>Account term</b>\nChoose duration for this account only.\n\"Unlimited\" removes the deadline.",
            reply_markup=kb.mail_duration_menu(prefix=f"acc:dur:{aid}", back=f"acc:params:{aid}", selected=current),
            parse_mode="HTML",
        )
    await call.answer()

@router.callback_query(F.data.startswith("acc:dur:"))
async def acc_set_duration_value(call: CallbackQuery, mailer_config: MailerConfig, mailer_db: MailerDB) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    parts = call.data.split(":")  # type: ignore[union-attr]
    aid, duration = int(parts[2]), int(parts[3])
    await mailer_db.set_account_duration(aid, duration)
    await call.answer("Account term updated")
    call.data = f"acc:params:{aid}"
    await acc_params(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("acc:setlim:"))
async def acc_set_lim(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(AccountConfigStates.cycle_limit)
    await state.update_data(account_id=aid)
    if call.message:
        await call.message.edit_text(
            f"Лимит сообщений в круге для аккаунта #{aid}?\n"
            f"Число, напр. <code>30</code>. Или <code>0</code> = глобальный.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(AccountConfigStates.cycle_limit)
async def acc_set_lim_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    aid = int(data["account_id"])
    try:
        n = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число")
        return
    if n <= 0:
        await mailer_db.set_account_param(aid, cycle_limit=None)
        msg = "Лимит сброшен на глобальный"
    else:
        await mailer_db.set_account_param(aid, cycle_limit=n)
        msg = f"Лимит круга: {n}"
    await state.clear()
    await message.answer(f"✅ {msg}", reply_markup=kb.back_main())


@router.callback_query(F.data.startswith("acc:setpause:"))
async def acc_set_pause(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(AccountConfigStates.cycle_pause)
    await state.update_data(account_id=aid)
    if call.message:
        await call.message.edit_text(
            f"Пауза после круга для аккаунта #{aid} в <b>минутах</b>?\n"
            f"Напр. <code>60</code>. Или <code>0</code> = глобальная.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(AccountConfigStates.cycle_pause)
async def acc_set_pause_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    aid = int(data["account_id"])
    try:
        minutes = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число минут")
        return
    if minutes <= 0:
        await mailer_db.set_account_param(aid, cycle_pause_sec=None)
        msg = "Пауза сброшена на глобальную"
    else:
        await mailer_db.set_account_param(aid, cycle_pause_sec=minutes * 60)
        msg = f"Пауза: {minutes} мин"
    await state.clear()
    await message.answer(f"✅ {msg}", reply_markup=kb.back_main())


@router.callback_query(F.data.startswith("acc:setdelay:"))
async def acc_set_delay(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(AccountConfigStates.delay)
    await state.update_data(account_id=aid)
    if call.message:
        await call.message.edit_text(
            f"Задержка между сообщениями для аккаунта #{aid} в <b>секундах</b>?\n"
            f"Напр. <code>10</code>. Или <code>0</code> = глобальная.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(AccountConfigStates.delay)
async def acc_set_delay_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    aid = int(data["account_id"])
    try:
        sec = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        await message.answer("Нужно число секунд")
        return
    if sec <= 0:
        await mailer_db.set_account_param(aid, delay_sec=None)
        msg = "Delay сброшен на глобальный"
    else:
        await mailer_db.set_account_param(aid, delay_sec=sec)
        msg = f"Delay: {sec} сек"
    await state.clear()
    await message.answer(f"✅ {msg}", reply_markup=kb.back_main())


@router.callback_query(F.data.startswith("acc:resetparams:"))
async def acc_reset_params(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_account_param(
        aid, cycle_limit=None, cycle_pause_sec=None, delay_sec=None
    )
    await mailer_db.set_account_duration(aid, 0)
    await call.answer("Параметры сброшены")
    call.data = f"acc:params:{aid}"
    await acc_params(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("acc:enable:"))
async def acc_enable(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_account_status(aid, "active", error=None)
    await call.answer("Включён")
    call.data = f"acc:view:{aid}"
    await acc_view(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("acc:disable:"))
async def acc_disable(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_account_status(aid, "disabled")
    await call.answer("Выключен")
    call.data = f"acc:view:{aid}"
    await acc_view(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("acc:del:"))
async def acc_del(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_telethon: TelethonManager,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_telethon.disconnect_account(aid)
    await mailer_db.delete_account(aid)
    await call.answer("Удалён")
    await menu_accounts(call, state, mailer_config, mailer_db)


# ── groups ────────────────────────────────────────────────────


@router.callback_query(F.data == "menu:groups")
async def menu_groups(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.clear()
    groups = await mailer_db.list_groups()
    text = (
        "<b>Группы для рассылки</b>\n\n"
        "Добавь:\n"
        "• перешли любое сообщение из группы сюда\n"
        "• или @username / t.me/… / invite / chat_id\n\n"
        "Аккаунт-рассыльщик должен иметь доступ к группе."
    )
    if call.message:
        await call.message.edit_text(
            text, reply_markup=kb.groups_menu(groups), parse_mode="HTML"
        )
    await call.answer()


@router.callback_query(F.data == "grp:add")
async def grp_add(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(AddGroupStates.waiting)
    if call.message:
        await call.message.edit_text(
            "Перешли сообщение из группы <b>или</b> пришли @username / ссылку / id.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(AddGroupStates.waiting)
@router.message(AddGroupStates.for_account)
async def grp_waiting(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_telethon: TelethonManager,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    link_aid = data.get("link_account_id")
    chat_id: int | None = None
    title = ""
    username = ""

    if message.forward_from_chat:
        ch = message.forward_from_chat
        chat_id = ch.id
        title = ch.title or str(ch.id)
        username = ch.username or ""
    elif message.forward_origin and getattr(message.forward_origin, "chat", None):
        ch = message.forward_origin.chat  # type: ignore[attr-defined]
        chat_id = ch.id
        title = getattr(ch, "title", None) or str(ch.id)
        username = getattr(ch, "username", None) or ""
    elif message.text:
        ref = message.text.strip()
        accounts = await mailer_db.list_accounts()
        active = [a for a in accounts if a["status"] in ("active", "cooldown")]
        if link_aid:
            acc = await mailer_db.get_account(int(link_aid))
            if acc:
                active = [acc] + [a for a in active if a["id"] != acc["id"]]
        if not active:
            await message.answer(
                "Нет аккаунтов. Сначала добавь аккаунт, потом группу по ссылке.\n"
                "Или перешли сообщение из группы (без аккаунта)."
            )
            return
        try:
            resolved = await mailer_telethon.resolve_group(active[0]["id"], ref)
            chat_id = int(resolved["chat_id"])
            title = resolved.get("title") or ""
            username = resolved.get("username") or ""
        except Exception as e:
            log.exception("resolve group")
            await message.answer(f"Не удалось добавить: {e}")
            return
    else:
        await message.answer("Пришли текст, ссылку или перешли сообщение из группы.")
        return

    if chat_id is None:
        await message.answer("Не удалось определить chat_id")
        return

    gid = await mailer_db.add_group(chat_id, title=title, username=username)
    extra = ""
    if link_aid:
        await mailer_db.link_account_group(int(link_aid), gid)
        extra = f"\n\u041f\u0440\u0438\u0432\u044f\u0437\u0430\u043d\u0430 \u043a \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0443 #{link_aid}"
    else:
        accounts = await mailer_db.list_accounts()
        active_accounts = [account for account in accounts if account["status"] in ("active", "cooldown")]
        for account in active_accounts:
            await mailer_db.link_account_group(int(account["id"]), gid)
        pause = await mailer_db.get_int("join_pause_sec", 300)
        extra = f"\n\u041f\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0430 \u0432 \u043e\u0447\u0435\u0440\u0435\u0434\u044c \u0434\u043b\u044f {len(active_accounts)} \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u043e\u0432. \u041f\u0430\u0443\u0437\u0430: {pause // 60} \u043c\u0438\u043d."
    await state.clear()
    await message.answer(
        f"✅ Группа добавлена\n"
        f"#{gid} <b>{title or chat_id}</b>\n"
        f"<code>{chat_id}</code>{extra}",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("grp:view:"))
async def grp_view(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    gid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    g = await mailer_db.get_group(gid)
    if not g:
        await call.answer("Не найдена", show_alert=True)
        return
    text = (
        f"<b>Группа #{g['id']}</b>\n"
        f"Название: {g.get('title')}\n"
        f"Username: {('@' + g['username']) if g.get('username') else '—'}\n"
        f"chat_id: <code>{g['chat_id']}</code>\n"
        f"Активна: {'да' if g.get('active') else 'нет'}"
    )
    if call.message:
        await call.message.edit_text(
            text,
            reply_markup=kb.group_card(gid, bool(g.get("active"))),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("grp:on:"))
async def grp_on(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    gid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_group_active(gid, True)
    await call.answer("Включена")
    call.data = f"grp:view:{gid}"
    await grp_view(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("grp:off:"))
async def grp_off(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    gid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_group_active(gid, False)
    await call.answer("Выключена")
    call.data = f"grp:view:{gid}"
    await grp_view(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("grp:del:"))
async def grp_del(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    gid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.delete_group(gid)
    await call.answer("Удалена")
    await menu_groups(call, state, mailer_config, mailer_db)


# ── messages ──────────────────────────────────────────────────


@router.callback_query(F.data == "menu:messages")
async def menu_messages(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.clear()
    messages = await mailer_db.list_messages()
    active = await mailer_db.get_active_message()
    active_id = active["id"] if active else None
    text = (
        "<b>Глобальные шаблоны сообщений</b>\n\n"
        "Используются, если у аккаунта <b>нет своего</b> текста.\n"
        "Своё сообщение: Аккаунты → аккаунт → «Сообщение аккаунта».\n"
        f"Активный глобальный: <b>{(active or {}).get('title') or '—'}</b>"
    )
    if call.message:
        await call.message.edit_text(
            text,
            reply_markup=kb.messages_menu(messages, active_id),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("msg:view:"))
async def msg_view(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    mid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    m = await mailer_db.get_message(mid)
    if not m:
        await call.answer("Нет", show_alert=True)
        return
    body = m.get("text") or ""
    if len(body) > 800:
        body = body[:800] + "…"
    text = f"<b>{m.get('title')}</b> (#{m['id']})\n\n{body}"
    if call.message:
        await call.message.edit_text(
            text, reply_markup=kb.message_card(mid), parse_mode="HTML"
        )
    await call.answer()


@router.callback_query(F.data.startswith("msg:edit:"))
async def msg_edit(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    mid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(MessageStates.edit_text)
    await state.update_data(message_id=mid)
    if call.message:
        await call.message.edit_text(
            "Пришли <b>новый текст</b> сообщения.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(MessageStates.edit_text)
async def msg_edit_text(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    mid = int(data["message_id"])
    text = _message_content(message)
    if not text.strip():
        await message.answer("Пустой текст")
        return
    await mailer_db.update_message_text(mid, text)
    await state.clear()
    await message.answer("✅ Текст обновлён", reply_markup=kb.back_main())


@router.callback_query(F.data.startswith("msg:use:"))
async def msg_use(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    mid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_active_message(mid)
    await call.answer("Активный шаблон выбран")
    call.data = f"msg:view:{mid}"
    await msg_view(call, mailer_config, mailer_db)


@router.callback_query(F.data == "msg:new")
async def msg_new(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(MessageStates.new_title)
    if call.message:
        await call.message.edit_text(
            "Название шаблона (коротко):",
            reply_markup=kb.cancel_kb(),
        )
    await call.answer()


@router.message(MessageStates.new_title)
async def msg_new_title(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    title = (message.text or "").strip()[:64] or "template"
    await state.update_data(title=title)
    await state.set_state(MessageStates.new_text)
    await message.answer("Теперь пришли текст сообщения:", reply_markup=kb.cancel_kb())


@router.message(MessageStates.new_text)
async def msg_new_text(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    text = _message_content(message)
    mid = await mailer_db.add_message(data.get("title") or "template", text)
    await mailer_db.set_active_message(mid)
    await state.clear()
    await message.answer(
        f"✅ Шаблон #{mid} создан и сделан активным",
        reply_markup=kb.back_main(),
    )


# ── settings / cycle ──────────────────────────────────────────


async def _settings_text(db: MailerDB) -> str:
    cycle = await db.get_int("cycle_limit", 50)
    pause = await db.get_int("cycle_pause_sec", 3600)
    delay = await db.get_float("delay_sec", 8)
    dur_def = await db.get_mailing_duration_default()
    mailing = await db.is_mailing_enabled()
    if mailing:
        left = await db.mailing_time_left()
        if left is not None:
            run_line = f"сейчас ВКЛ · осталось <b>{_fmt_left_ru(left)}</b>"
        else:
            run_line = "сейчас ВКЛ · <b>без лимита</b>"
    else:
        run_line = "сейчас ВЫКЛ"
    return (
        f"<b>Настройки / цикл</b>\n\n"
        f"📅 <b>Срок рассылки:</b> {_fmt_duration_choice(dur_def)}\n"
        f"   ({run_line})\n"
        f"   По истечении срока рассылка останавливается сама.\n\n"
        f"🔢 Лимит сообщений в круге: <b>{cycle}</b>\n"
        f"⏱ Пауза после круга: <b>{pause}</b> сек ({pause // 60} мин)\n"
        f"⏳ Пауза между отправками: <b>{delay}</b> сек\n\n"
        f"После N успешных отправок аккаунт уходит на паузу, "
        f"затем — новый круг.\n"
        f"Срок меняется кнопкой ниже; если рассылка уже идёт — "
        f"таймер перезапустится от сейчас."
    )


@router.callback_query(F.data == "menu:settings")
async def menu_settings(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.clear()
    if call.message:
        await call.message.edit_text(
            await _settings_text(mailer_db),
            reply_markup=kb.settings_menu(),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data == "set:duration")
async def set_duration(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    dur_def = await mailer_db.get_mailing_duration_default()
    mailing = await mailer_db.is_mailing_enabled()
    extra = ""
    if mailing:
        left = await mailer_db.mailing_time_left()
        if left is not None:
            extra = f"\nСейчас идёт · осталось <b>{_fmt_left_ru(left)}</b> — выбор сбросит таймер."
        else:
            extra = "\nСейчас идёт · без лимита — выбор поставит таймер."
    if call.message:
        await call.message.edit_text(
            f"<b>📅 Срок рассылки</b>\n\n"
            f"Текущий: <b>{_fmt_duration_choice(dur_def)}</b>\n"
            f"По окончании срока рассылка <b>закончится автоматически</b>.\n"
            f"«Без лимита» — только ручной стоп.{extra}",
            reply_markup=kb.mail_duration_menu(
                prefix="set:dur",
                back="menu:settings",
                selected=dur_def,
            ),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("set:dur:"))
async def set_duration_val(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    try:
        duration = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    except (ValueError, AttributeError):
        await call.answer("Неверный срок", show_alert=True)
        return
    if duration < 0:
        await call.answer("Неверный срок", show_alert=True)
        return
    await mailer_db.set_mailing_duration_default(duration)
    label = _fmt_duration_choice(duration)
    await call.answer(f"Срок: {label}")
    if call.message:
        await call.message.edit_text(
            await _settings_text(mailer_db),
            reply_markup=kb.settings_menu(),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "set:cycle_limit")
async def set_cycle_limit(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(SettingsStates.cycle_limit)
    if call.message:
        await call.message.edit_text(
            "Сколько сообщений в одном круге? (число, напр. 50)",
            reply_markup=kb.cancel_kb(),
        )
    await call.answer()


@router.message(SettingsStates.cycle_limit)
async def set_cycle_limit_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    try:
        n = int((message.text or "").strip())
        if n < 1 or n > 100_000:
            raise ValueError
    except ValueError:
        await message.answer("Нужно целое число от 1")
        return
    await mailer_db.set_setting("cycle_limit", str(n))
    await state.clear()
    await message.answer(f"✅ Лимит круга: {n}", reply_markup=kb.back_main())


@router.callback_query(F.data == "set:cycle_pause")
async def set_cycle_pause(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(SettingsStates.cycle_pause)
    if call.message:
        await call.message.edit_text(
            "Пауза между кругами в <b>минутах</b> (по умолчанию 60):",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(SettingsStates.cycle_pause)
async def set_cycle_pause_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    try:
        minutes = int((message.text or "").strip())
        if minutes < 1 or minutes > 24 * 60:
            raise ValueError
    except ValueError:
        await message.answer("Минуты: целое от 1")
        return
    await mailer_db.set_setting("cycle_pause_sec", str(minutes * 60))
    await state.clear()
    await message.answer(
        f"✅ Пауза между кругами: {minutes} мин",
        reply_markup=kb.back_main(),
    )


@router.callback_query(F.data == "set:delay")
async def set_delay(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(SettingsStates.delay)
    if call.message:
        await call.message.edit_text(
            "Задержка между сообщениями в <b>секундах</b> (напр. 8):",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(SettingsStates.delay)
async def set_delay_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    try:
        sec = float((message.text or "").strip().replace(",", "."))
        if sec < 1 or sec > 3600:
            raise ValueError
    except ValueError:
        await message.answer("Число секунд от 1")
        return
    await mailer_db.set_setting("delay_sec", str(sec))
    await state.clear()
    await message.answer(f"✅ Задержка: {sec} сек", reply_markup=kb.back_main())


# ── multi log groups pool ─────────────────────────────────────


@router.callback_query(F.data == "menu:logs")
async def menu_logs(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.clear()
    logs = await mailer_db.list_log_groups()
    if call.message:
        await call.message.edit_text(
            "<b>Пул лог-групп</b>\n\n"
            "Можно много лог-групп. Потом в карточке аккаунта "
            "отметь, какие логи слушают <b>этого</b> клиента.\n\n"
            "1. Создай группу в Telegram\n"
            "2. Добавь <b>этого бота</b> админом\n"
            "3. Добавь группу сюда (forward / chat_id)",
            reply_markup=kb.log_groups_pool_menu(logs),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data == "log:add")
async def log_add(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(AddLogGroupStates.waiting)
    await state.update_data(link_account_id=None)
    if call.message:
        await call.message.edit_text(
            "Перешли сообщение из лог-группы или пришли chat_id.",
            reply_markup=kb.cancel_kb(),
        )
    await call.answer()


async def _parse_chat_ref(message: Message) -> tuple[int | None, str]:
    chat_id: int | None = None
    title = ""
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        title = message.forward_from_chat.title or ""
    elif message.forward_origin and getattr(message.forward_origin, "chat", None):
        ch = message.forward_origin.chat  # type: ignore[attr-defined]
        chat_id = ch.id
        title = getattr(ch, "title", "") or ""
    elif message.text and message.text.strip().lstrip("-").isdigit():
        chat_id = int(message.text.strip())
    return chat_id, title


@router.message(AddLogGroupStates.waiting)
@router.message(AddLogGroupStates.for_account)
async def log_add_waiting(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    chat_id, title = await _parse_chat_ref(message)
    if chat_id is None:
        await message.answer("Перешли сообщение из группы или числовой chat_id")
        return
    data = await state.get_data()
    link_aid = data.get("link_account_id")
    lid = await mailer_db.add_log_group(chat_id, title=title)
    if link_aid:
        await mailer_db.link_account_log_group(int(link_aid), lid)
    await state.clear()
    try:
        await message.bot.send_message(
            chat_id,
            f"✅ Log group connected to mailer.\n"
            f"{('Title: ' + title) if title else ''}",
        )
        probe = "Test message sent."
    except Exception as e:
        probe = f"Saved, but bot cannot write: {e}"
    extra = f"\nПривязана к аккаунту #{link_aid}" if link_aid else ""
    await message.answer(
        f"✅ Лог-группа #{lid}: <code>{chat_id}</code>\n{probe}{extra}",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("log:view:"))
async def log_view(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    lid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    lg = await mailer_db.get_log_group(lid)
    if not lg:
        await call.answer("Не найдена", show_alert=True)
        return
    if call.message:
        await call.message.edit_text(
            f"<b>Лог-группа #{lg['id']}</b>\n"
            f"Название: {lg.get('title')}\n"
            f"chat_id: <code>{lg['chat_id']}</code>\n"
            f"Активна: {'да' if lg.get('active') else 'нет'}",
            reply_markup=kb.log_group_card(lid, bool(lg.get("active"))),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("log:on:"))
async def log_on(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    lid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_log_group_active(lid, True)
    call.data = f"log:view:{lid}"
    await log_view(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("log:off:"))
async def log_off(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    lid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.set_log_group_active(lid, False)
    call.data = f"log:view:{lid}"
    await log_view(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("log:del:"))
async def log_del(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    lid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.delete_log_group(lid)
    await call.answer("Удалена")
    await menu_logs(call, state, mailer_config, mailer_db)


# ── per-account groups / logs / client ────────────────────────


@router.callback_query(F.data.startswith("acc:client:"))
async def acc_client(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(ClientLabelStates.waiting)
    await state.update_data(account_id=aid)
    if call.message:
        await call.message.edit_text(
            f"Метка клиента для аккаунта #{aid} "
            f"(например имя рекламодателя):\n"
            f"Отправь текст или <code>-</code> чтобы очистить.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(ClientLabelStates.waiting)
async def acc_client_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    data = await state.get_data()
    aid = int(data["account_id"])
    raw = (message.text or "").strip()
    if raw in ("-", "—", "0"):
        raw = ""
    await mailer_db.set_client_label(aid, raw)
    await state.clear()
    await message.answer(
        f"✅ Клиент аккаунта #{aid}: <b>{_html_esc(raw or '—')}</b>",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("acc:grps:"))
async def acc_grps(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    all_g = await mailer_db.list_groups()
    linked = {g["id"] for g in await mailer_db.list_account_groups(aid, only_active=False)}
    if call.message:
        await call.message.edit_text(
            f"<b>Группы рассылки аккаунта #{aid}</b>\n\n"
            f"Отметь ✅ группы, куда <b>этот</b> аккаунт шлёт рекламу.\n"
            f"Другой аккаунт / клиент — другие галочки.",
            reply_markup=kb.account_groups_menu(aid, all_g, linked),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("acc:grptog:"))
async def acc_grp_tog(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    # acc:grptog:aid:gid
    parts = call.data.split(":")  # type: ignore[union-attr]
    aid, gid = int(parts[2]), int(parts[3])
    if await mailer_db.account_has_group(aid, gid):
        await mailer_db.unlink_account_group(aid, gid)
    else:
        await mailer_db.link_account_group(aid, gid)
    call.data = f"acc:grps:{aid}"
    await acc_grps(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("acc:grpadd:"))
async def acc_grp_add(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(AddGroupStates.for_account)
    await state.update_data(link_account_id=aid)
    if call.message:
        await call.message.edit_text(
            f"Группа для аккаунта #{aid}: перешли сообщение из группы "
            f"или @username / invite / chat_id.",
            reply_markup=kb.cancel_kb(),
        )
    await call.answer()


@router.callback_query(F.data.startswith("acc:logs:"))
async def acc_logs(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    all_l = await mailer_db.list_log_groups()
    linked = {x["id"] for x in await mailer_db.list_account_log_groups(aid)}
    # list_account_log_groups only active — for toggle menu show all linked via raw
    cur_linked = await mailer_db.db.execute(
        "SELECT log_group_id FROM account_log_groups WHERE account_id = ?", (aid,)
    )
    linked = {int(r[0]) for r in await cur_linked.fetchall()}
    if call.message:
        await call.message.edit_text(
            f"<b>Лог-группы аккаунта #{aid}</b>\n\n"
            f"Сюда пишутся отправки / конец круга <b>только этого</b> клиента.\n"
            f"Можно несколько галочек.",
            reply_markup=kb.account_logs_menu(aid, all_l, linked),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("acc:logtog:"))
async def acc_log_tog(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    parts = call.data.split(":")  # type: ignore[union-attr]
    aid, lid = int(parts[2]), int(parts[3])
    cur = await mailer_db.db.execute(
        "SELECT 1 FROM account_log_groups WHERE account_id = ? AND log_group_id = ?",
        (aid, lid),
    )
    if await cur.fetchone():
        await mailer_db.unlink_account_log_group(aid, lid)
    else:
        await mailer_db.link_account_log_group(aid, lid)
    call.data = f"acc:logs:{aid}"
    await acc_logs(call, mailer_config, mailer_db)


@router.callback_query(F.data.startswith("acc:logadd:"))
async def acc_log_add(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    aid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await state.set_state(AddLogGroupStates.for_account)
    await state.update_data(link_account_id=aid)
    if call.message:
        await call.message.edit_text(
            f"Новая лог-группа для аккаунта #{aid}:\n"
            f"перешли сообщение из группы или chat_id.",
            reply_markup=kb.cancel_kb(),
        )
    await call.answer()


# ── team (operators) ──────────────────────────────────────────


@router.callback_query(F.data == "menu:team")
async def menu_team(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.clear()
    ops = await mailer_db.list_operators()
    mode = "открыт для всех (MAILER_OPEN)" if mailer_config.allow_all else "только список ниже + ADMIN_IDS"
    lines = [
        "<b>Команда</b>",
        f"Режим доступа: <b>{mode}</b>",
        "",
        "Кто уже заходил в бота:",
    ]
    if not ops:
        lines.append("— пока никого —")
    else:
        for op in ops:
            un = f"@{op['username']}" if op.get("username") else op.get("full_name") or "—"
            lines.append(f"• {un} — <code>{op['user_id']}</code>")
    lines.append("\nДобавить вручную: ID человека (/id в боте).")
    if call.message:
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=kb.team_menu(ops),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data == "team:add")
async def team_add(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(TeamStates.add_id)
    if call.message:
        await call.message.edit_text(
            "Пришли <b>числовой Telegram ID</b> товарища.\n"
            "Он может узнать ID командой /id в этом боте.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(TeamStates.add_id)
async def team_add_id(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой ID, например 123456789")
        return
    uid = int(raw)
    await mailer_db.upsert_operator(uid, "", f"added_by_{message.from_user.id}")
    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{uid}</code> в команде.\n"
        f"Пусть нажмёт /start — сможет добавлять аккаунты.",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("team:del:"))
async def team_del(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    uid = int(call.data.split(":")[-1])  # type: ignore[union-attr]
    await mailer_db.remove_operator(uid)
    await call.answer("Удалён из списка")
    await menu_team(call, state, mailer_config, mailer_db)

# ── API credentials (admin via bot) ───────────────────────────


async def _api_status_text(mailer_db: MailerDB, mailer_config: MailerConfig) -> str:
    db_id = (await mailer_db.get_setting("tg_api_id", "")).strip()
    db_hash = (await mailer_db.get_setting("tg_api_hash", "")).strip()
    if db_id.isdigit() and db_hash:
        mailer_config.apply_api_from_values(db_id, db_hash)
    ready = mailer_config.telethon_ready
    hid = str(mailer_config.api_id) if mailer_config.api_id else "—"
    hh = (mailer_config.api_hash[:6] + "…" + mailer_config.api_hash[-4:]) if mailer_config.api_hash and len(mailer_config.api_hash) > 12 else (mailer_config.api_hash or "—")
    src = "БД (через бота)" if db_id and db_hash else ("env/Railway" if ready else "не задано")
    return (
        f"<b>🔑 API Telegram (Telethon)</b>\n\n"
        f"Статус: <b>{'✅ готово' if ready else '❌ не задано'}</b>\n"
        f"Источник: {src}\n"
        f"api_id: <code>{hid}</code>\n"
        f"api_hash: <code>{hh}</code>\n\n"
        f"Взять на https://my.telegram.org → API development tools\n"
        f"(один раз на весь бот, не ключ с LZT)\n\n"
        f"Можно вставить одним сообщением:\n"
        f"<code>12345678\nabcdef0123456789...</code>\n"
        f"или <code>12345678:abcdef...</code>"
    )


@router.callback_query(F.data == "menu:api")
async def menu_api(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.clear()
    text = await _api_status_text(mailer_db, mailer_config)
    if call.message:
        await call.message.edit_text(
            text,
            reply_markup=kb.api_menu(mailer_config.telethon_ready),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data == "api:both")
async def api_both(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(SettingsStates.api_both)
    if call.message:
        await call.message.edit_text(
            "Пришли <b>api_id</b> и <b>api_hash</b> одним сообщением:\n\n"
            "• две строки\n"
            "• или <code>api_id:api_hash</code>\n"
            "• или <code>api_id api_hash</code>\n\n"
            "Пример:\n<code>28491234\na1b2c3d4e5f6789012345678abcdef01</code>",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(SettingsStates.api_both)
async def api_both_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    raw = (message.text or "").strip()
    api_id = ""
    api_hash = ""
    if "\n" in raw:
        parts = [p.strip() for p in raw.splitlines() if p.strip()]
        if len(parts) >= 2:
            api_id, api_hash = parts[0], parts[1]
    elif ":" in raw:
        api_id, api_hash = [p.strip() for p in raw.split(":", 1)]
    else:
        parts = raw.split()
        if len(parts) >= 2:
            api_id, api_hash = parts[0], parts[1]
    if not api_id.isdigit() or len(api_hash) < 16:
        await message.answer(
            "Не распознал. Нужно число api_id и длинный api_hash.\n"
            "Формат: две строки или id:hash"
        )
        return
    await mailer_db.set_setting("tg_api_id", api_id)
    await mailer_db.set_setting("tg_api_hash", api_hash)
    mailer_config.set_api(int(api_id), api_hash)
    await state.clear()
    # try delete message with secrets
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        f"✅ API сохранены в боте.\n"
        f"api_id: <code>{api_id}</code>\n"
        f"Теперь можно: <b>➕ Добавить аккаунт</b>",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "api:id")
async def api_id_btn(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(SettingsStates.api_id)
    if call.message:
        await call.message.edit_text(
            "Пришли <b>api_id</b> (только число):",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(SettingsStates.api_id)
async def api_id_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("api_id должен быть числом")
        return
    await mailer_db.set_setting("tg_api_id", raw)
    mailer_config.api_id = int(raw)
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        f"✅ api_id = <code>{raw}</code>\nТеперь задай api_hash (меню API → 2️⃣).",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "api:hash")
async def api_hash_btn(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(SettingsStates.api_hash)
    if call.message:
        await call.message.edit_text(
            "Пришли <b>api_hash</b> (строка с my.telegram.org):",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(SettingsStates.api_hash)
async def api_hash_val(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
    raw = (message.text or "").strip()
    if len(raw) < 16:
        await message.answer("Слишком короткий api_hash")
        return
    await mailer_db.set_setting("tg_api_hash", raw)
    mailer_config.api_hash = raw
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    ready = mailer_config.telethon_ready
    await message.answer(
        f"✅ api_hash сохранён.\n"
        f"Статус API: <b>{'готово ✅' if ready else 'нужен ещё api_id'}</b>",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "api:clear")
async def api_clear(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await mailer_db.set_setting("tg_api_id", "")
    await mailer_db.set_setting("tg_api_hash", "")
    mailer_config.api_id = 0
    mailer_config.api_hash = ""
    await call.answer("Очищено")
    await menu_api(call, state, mailer_config, mailer_db)
