from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu(mailing_on: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="➕ Добавить аккаунт (номер)",
            callback_data="acc:add",
        )
    )
    b.row(InlineKeyboardButton(text="👤 Аккаунты (клиенты)", callback_data="menu:accounts"))
    b.row(InlineKeyboardButton(text="👥 Пул групп", callback_data="menu:groups"))
    b.row(InlineKeyboardButton(text="📋 Пул лог-групп", callback_data="menu:logs"))
    b.row(InlineKeyboardButton(text="✉️ Сообщения", callback_data="menu:messages"))
    b.row(InlineKeyboardButton(text="🔑 API Telegram", callback_data="menu:api"))
    b.row(InlineKeyboardButton(text="⚙️ Настройки / цикл", callback_data="menu:settings"))
    b.row(InlineKeyboardButton(text="🧑‍🤝‍🧑 Команда", callback_data="menu:team"))
    if mailing_on:
        b.row(InlineKeyboardButton(text="⏹ Стоп рассылки", callback_data="mail:stop"))
    else:
        b.row(InlineKeyboardButton(text="▶️ Старт рассылки", callback_data="mail:start"))
    b.row(InlineKeyboardButton(text="📊 Статус", callback_data="menu:status"))
    return b.as_markup()


def back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Меню", callback_data="menu:main")]]
    )


def team_menu(operators: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить по ID", callback_data="team:add"))
    for op in operators[:30]:
        uname = op.get("username") or op.get("full_name") or str(op["user_id"])
        b.row(
            InlineKeyboardButton(
                text=f"🗑 {uname[:24]} ({op['user_id']})",
                callback_data=f"team:del:{op['user_id']}",
            )
        )
    b.row(InlineKeyboardButton(text="« Меню", callback_data="menu:main"))
    return b.as_markup()


def accounts_menu(accounts: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить аккаунт (номер + код)", callback_data="acc:add"))
    for a in accounts:
        st = a.get("status") or "?"
        icon = {"active": "🟢", "cooldown": "⏸", "error": "🔴", "disabled": "⚪"}.get(st, "•")
        label = a.get("label") or a.get("phone") or f"#{a['id']}"
        b.row(
            InlineKeyboardButton(
                text=f"{icon} {label[:28]}",
                callback_data=f"acc:view:{a['id']}",
            )
        )
    b.row(InlineKeyboardButton(text="« Меню", callback_data="menu:main"))
    return b.as_markup()


def account_card(account_id: int, status: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="🏷️ Клиент (метка)",
            callback_data=f"acc:client:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="✉️ Сообщение аккаунта",
            callback_data=f"acc:msg:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="📣 Группы рассылки (этого аккаунта)",
            callback_data=f"acc:grps:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="📋 Лог-группы (этого аккаунта)",
            callback_data=f"acc:logs:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="⚙️ Параметры (круг / пауза / delay)",
            callback_data=f"acc:params:{account_id}",
        )
    )
    if status == "disabled":
        b.row(InlineKeyboardButton(text="Включить", callback_data=f"acc:enable:{account_id}"))
    else:
        b.row(InlineKeyboardButton(text="Выключить", callback_data=f"acc:disable:{account_id}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"acc:del:{account_id}"))
    b.row(InlineKeyboardButton(text="« Аккаунты", callback_data="menu:accounts"))
    return b.as_markup()


def account_groups_menu(
    account_id: int,
    all_groups: list[dict],
    linked_ids: set[int],
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="➕ Добавить новую группу сюда",
            callback_data=f"acc:grpadd:{account_id}",
        )
    )
    for g in all_groups:
        on = g["id"] in linked_ids
        mark = "✅" if on else "☐"
        title = (g.get("title") or str(g.get("chat_id")))[:22]
        b.row(
            InlineKeyboardButton(
                text=f"{mark} {title}",
                callback_data=f"acc:grptog:{account_id}:{g['id']}",
            )
        )
    b.row(InlineKeyboardButton(text="« Аккаунт", callback_data=f"acc:view:{account_id}"))
    return b.as_markup()


def account_logs_menu(
    account_id: int,
    all_logs: list[dict],
    linked_ids: set[int],
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="➕ Добавить новую лог-группу",
            callback_data=f"acc:logadd:{account_id}",
        )
    )
    for lg in all_logs:
        on = lg["id"] in linked_ids
        mark = "✅" if on else "☐"
        title = (lg.get("title") or str(lg.get("chat_id")))[:22]
        b.row(
            InlineKeyboardButton(
                text=f"{mark} {title}",
                callback_data=f"acc:logtog:{account_id}:{lg['id']}",
            )
        )
    b.row(InlineKeyboardButton(text="« Аккаунт", callback_data=f"acc:view:{account_id}"))
    return b.as_markup()


def log_groups_pool_menu(logs: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить лог-группу", callback_data="log:add"))
    for lg in logs:
        icon = "🟢" if lg.get("active") else "⚪"
        title = (lg.get("title") or str(lg.get("chat_id")))[:28]
        b.row(
            InlineKeyboardButton(
                text=f"{icon} {title}",
                callback_data=f"log:view:{lg['id']}",
            )
        )
    b.row(InlineKeyboardButton(text="« Меню", callback_data="menu:main"))
    return b.as_markup()


def log_group_card(log_group_id: int, active: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if active:
        b.row(InlineKeyboardButton(text="Выключить", callback_data=f"log:off:{log_group_id}"))
    else:
        b.row(InlineKeyboardButton(text="Включить", callback_data=f"log:on:{log_group_id}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"log:del:{log_group_id}"))
    b.row(InlineKeyboardButton(text="« Лог-группы", callback_data="menu:logs"))
    return b.as_markup()


def account_params_menu(account_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="🔢 Лимит круга",
            callback_data=f"acc:setlim:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="⏱ Пауза после круга (мин)",
            callback_data=f"acc:setpause:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="⏳ Задержка между сообщениями (сек)",
            callback_data=f"acc:setdelay:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="♻️ Сбросить всё на глобальные",
            callback_data=f"acc:resetparams:{account_id}",
        )
    )
    b.row(InlineKeyboardButton(text="« Аккаунт", callback_data=f"acc:view:{account_id}"))
    return b.as_markup()


def account_msg_menu(account_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="✏️ Задать / изменить текст",
            callback_data=f"acc:msgedit:{account_id}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="🗑 Очистить (брать глобальный шаблон)",
            callback_data=f"acc:msgclear:{account_id}",
        )
    )
    b.row(InlineKeyboardButton(text="« Аккаунт", callback_data=f"acc:view:{account_id}"))
    return b.as_markup()


def groups_menu(groups: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить группу", callback_data="grp:add"))
    for g in groups:
        icon = "🟢" if g.get("active") else "⚪"
        title = (g.get("title") or str(g.get("chat_id")))[:28]
        b.row(
            InlineKeyboardButton(
                text=f"{icon} {title}",
                callback_data=f"grp:view:{g['id']}",
            )
        )
    b.row(InlineKeyboardButton(text="« Меню", callback_data="menu:main"))
    return b.as_markup()


def group_card(group_id: int, active: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if active:
        b.row(InlineKeyboardButton(text="Выключить", callback_data=f"grp:off:{group_id}"))
    else:
        b.row(InlineKeyboardButton(text="Включить", callback_data=f"grp:on:{group_id}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"grp:del:{group_id}"))
    b.row(InlineKeyboardButton(text="« Группы", callback_data="menu:groups"))
    return b.as_markup()


def messages_menu(messages: list[dict], active_id: int | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Новое сообщение", callback_data="msg:new"))
    for m in messages:
        mark = "✅ " if active_id and m["id"] == active_id else ""
        title = (m.get("title") or f"#{m['id']}")[:24]
        b.row(
            InlineKeyboardButton(
                text=f"{mark}{title}",
                callback_data=f"msg:view:{m['id']}",
            )
        )
    b.row(InlineKeyboardButton(text="« Меню", callback_data="menu:main"))
    return b.as_markup()


def message_card(message_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"msg:edit:{message_id}"))
    b.row(InlineKeyboardButton(text="⭐ Сделать активным", callback_data=f"msg:use:{message_id}"))
    b.row(InlineKeyboardButton(text="« Сообщения", callback_data="menu:messages"))
    return b.as_markup()


def settings_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔢 Лимит круга", callback_data="set:cycle_limit"))
    b.row(InlineKeyboardButton(text="⏱ Пауза между кругами", callback_data="set:cycle_pause"))
    b.row(InlineKeyboardButton(text="⏳ Задержка между сообщениями", callback_data="set:delay"))
    b.row(InlineKeyboardButton(text="« Меню", callback_data="menu:main"))
    return b.as_markup()


def api_menu(ready: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="📝 Вставить api_id + api_hash одним сообщением",
            callback_data="api:both",
        )
    )
    b.row(InlineKeyboardButton(text="1️⃣ Задать api_id", callback_data="api:id"))
    b.row(InlineKeyboardButton(text="2️⃣ Задать api_hash", callback_data="api:hash"))
    if ready:
        b.row(InlineKeyboardButton(text="🗑 Очистить API", callback_data="api:clear"))
    b.row(InlineKeyboardButton(text="« Меню", callback_data="menu:main"))
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]]
    )
