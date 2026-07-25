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
    user = event.from_user
    if not user:
        return False
    if config.is_admin(user.id, user.username):
        return True
    if db is not None and await db.is_operator(user.id):
        return True
    return False


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
        f"Попроси админа добавить тебя:\n"
        f"Команда → Добавить по ID → <code>{uid}</code>\n"
        f"или включи <code>MAILER_OPEN=true</code> на сервере."
    )
    log.warning("access denied user_id=%s username=%s", uid, uname)
    if isinstance(event, CallbackQuery):
        await event.answer(f"Нет доступа. ID: {uid}", show_alert=True)
        if event.message:
            await event.message.answer(text, parse_mode="HTML")
    else:
        await event.answer(text, parse_mode="HTML")


async def _main_text(db: MailerDB, config: MailerConfig | None = None) -> str:
    st = await db.stats()
    cycle = await db.get_int("cycle_limit", 50)
    pause = await db.get_int("cycle_pause_sec", 3600)
    delay = await db.get_float("delay_sec", 8)
    log_id = await db.get_setting("log_group_id", "")
    mail = "🟢 ВКЛ" if st["mailing"] else "🔴 ВЫКЛ"
    api_ok = "✅ задан" if (config and config.telethon_ready) else "❌ не задан → «🔑 API Telegram»"
    open_mode = "да (все с /start)" if (config and config.allow_all) else "нет (только команда)"
    return (
        f"<b>Mailer — панель</b>\n\n"
        f"Рассылка: <b>{mail}</b>\n"
        f"Аккаунты: {st['accounts']} (active {st['accounts_active']}, "
        f"cooldown {st['accounts_cooldown']})\n"
        f"Группы: {st['groups']}\n"
        f"Отправок OK/FAIL: {st['sends_ok']}/{st['sends_fail']}\n"
        f"Круг: <b>{cycle}</b> → пауза <b>{pause // 60}</b> мин · delay <b>{delay}</b>с\n"
        f"Лог-группа: <code>{log_id or 'не задана'}</code>\n"
        f"API: {api_ok}\n"
        f"Открытый доступ: <b>{open_mode}</b>\n\n"
        f"Админ: <b>🔑 API Telegram</b> (один раз)\n"
        f"Команда: <b>➕ Добавить аккаунт</b> → номер → код"
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


# ── mail start/stop ───────────────────────────────────────────


@router.callback_query(F.data == "mail:start")
async def mail_start(
    call: CallbackQuery,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
    mailer_engine: MailerEngine,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    accounts = [a for a in await mailer_db.list_accounts() if a["status"] != "disabled"]
    groups = await mailer_db.list_groups(only_active=True)
    if not accounts:
        await call.answer("Сначала добавь аккаунт", show_alert=True)
        return
    if not groups:
        await call.answer("Сначала добавь группы", show_alert=True)
        return
    ready_msg = 0
    for a in accounts:
        if await mailer_db.account_message_text(a):
            ready_msg += 1
    if ready_msg == 0:
        await call.answer(
            "Нет текста: задай сообщение на аккаунте или глобальный шаблон",
            show_alert=True,
        )
        return
    db_id = (await mailer_db.get_setting("tg_api_id", "")).strip()
    db_hash = (await mailer_db.get_setting("tg_api_hash", "")).strip()
    if db_id or db_hash:
        mailer_config.apply_api_from_values(db_id or None, db_hash or None)
    if not mailer_config.telethon_ready:
        await call.answer("Сначала «🔑 API Telegram»", show_alert=True)
        return
    await mailer_db.set_mailing_enabled(True)
    mailer_engine.start()
    await call.answer("Рассылка запущена")
    if call.message:
        await call.message.edit_text(
            await _main_text(mailer_db),
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
            await _main_text(mailer_db),
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
    msg = await mailer_db.account_message_text(acc)
    own_msg = bool((acc.get("message_text") or "").strip())
    preview = (msg[:120] + "…") if len(msg) > 120 else msg
    if not preview:
        preview = "— не задано —"
    return (
        f"<b>Аккаунт #{acc['id']}</b>\n"
        f"Имя: {acc.get('label')}\n"
        f"Телефон: <code>{acc['phone']}</code>\n"
        f"Статус: <code>{acc['status']}</code>\n"
        f"В круге: {acc.get('sent_in_cycle') or 0}/{limit}\n"
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
    text = message.text or message.caption or ""
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
    await state.clear()
    await message.answer(
        f"✅ Группа добавлена\n"
        f"#{gid} <b>{title or chat_id}</b>\n"
        f"<code>{chat_id}</code>",
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
    text = message.text or message.caption or ""
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
    text = message.text or message.caption or ""
    mid = await mailer_db.add_message(data.get("title") or "template", text)
    await mailer_db.set_active_message(mid)
    await state.clear()
    await message.answer(
        f"✅ Шаблон #{mid} создан и сделан активным",
        reply_markup=kb.back_main(),
    )


# ── settings / cycle ──────────────────────────────────────────


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
    cycle = await mailer_db.get_int("cycle_limit", 50)
    pause = await mailer_db.get_int("cycle_pause_sec", 3600)
    delay = await mailer_db.get_float("delay_sec", 8)
    text = (
        f"<b>Настройки цикла</b>\n\n"
        f"Лимит сообщений в круге: <b>{cycle}</b>\n"
        f"Пауза после круга: <b>{pause}</b> сек ({pause // 60} мин)\n"
        f"Пауза между отправками: <b>{delay}</b> сек\n\n"
        f"После N успешных отправок аккаунт уходит на паузу, "
        f"через час (или сколько задашь) — новый круг."
    )
    if call.message:
        await call.message.edit_text(
            text, reply_markup=kb.settings_menu(), parse_mode="HTML"
        )
    await call.answer()


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


# ── log group ─────────────────────────────────────────────────


@router.callback_query(F.data == "menu:log")
async def menu_log(
    call: CallbackQuery,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(call, mailer_config, mailer_db):
        await _deny(call)
        return
    await state.set_state(SettingsStates.log_group)
    log_id = await mailer_db.get_setting("log_group_id", "")
    if call.message:
        await call.message.edit_text(
            f"<b>Лог-группа</b>\n\n"
            f"Сейчас: <code>{log_id or 'не задана'}</code>\n\n"
            f"1. Создай группу\n"
            f"2. Добавь <b>этого бота</b> админом (чтобы писал логи)\n"
            f"3. Перешли сюда любое сообщение из лог-группы "
            f"или пришли chat_id\n\n"
            f"Туда: куда ушло, с какого аккаунта, статус, прогресс круга.",
            reply_markup=kb.cancel_kb(),
            parse_mode="HTML",
        )
    await call.answer()


@router.message(SettingsStates.log_group)
async def set_log_group(
    message: Message,
    state: FSMContext,
    mailer_config: MailerConfig,
    mailer_db: MailerDB,
) -> None:
    if not await _is_allowed(message, mailer_config, mailer_db):
        await _deny(message)
        return
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
    else:
        await message.answer(
            "Перешли сообщение из лог-группы или пришли числовой chat_id"
        )
        return

    await mailer_db.set_setting("log_group_id", str(chat_id))
    await state.clear()

    try:
        await message.bot.send_message(
            chat_id,
            f"✅ Лог-группа подключена к Mailer.\n"
            f"{('Группа: ' + title) if title else ''}",
        )
        probe = "Тестовое сообщение в лог отправлено."
    except Exception as e:
        probe = (
            f"chat_id сохранён, но бот не смог написать: {e}\n"
            f"Добавь бота в группу и дай право писать."
        )

    await message.answer(
        f"✅ Лог-группа: <code>{chat_id}</code>\n{probe}",
        reply_markup=kb.back_main(),
        parse_mode="HTML",
    )


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
