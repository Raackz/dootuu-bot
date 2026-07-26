from aiogram.fsm.state import State, StatesGroup


class AddAccountStates(StatesGroup):
    phone = State()
    code = State()
    password = State()
    label = State()


class AddGroupStates(StatesGroup):
    waiting = State()
    for_account = State()  # add group and link to account_id


class AddLogGroupStates(StatesGroup):
    waiting = State()
    for_account = State()


class ClientLabelStates(StatesGroup):
    waiting = State()


class MessageStates(StatesGroup):
    edit_text = State()
    new_title = State()
    new_text = State()


class SettingsStates(StatesGroup):
    cycle_limit = State()
    cycle_pause = State()
    delay = State()
    duration = State()
    log_group = State()
    api_id = State()
    api_hash = State()
    api_both = State()


class AccountConfigStates(StatesGroup):
    message_text = State()
    cycle_limit = State()
    cycle_pause = State()
    delay = State()
    duration = State()


class TeamStates(StatesGroup):
    add_id = State()
