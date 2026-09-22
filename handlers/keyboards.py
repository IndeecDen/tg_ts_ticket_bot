"""
ReplyKeyboard — многоуровневое меню по ролям.
"""
from aiogram import Router, types, F, Bot
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from db import database
from utils.logging_config import logger
from utils.telegram_utils import safe_send_message
from .commands import check_user_role

router = Router()

# ── Тексты кнопок ────────────────────────────────────────────────

BTN_HELP           = "❓ Помощь"
BTN_BACK           = "◀️ Назад"
BTN_BACK_SETTINGS  = "◀️ В настройки"
BTN_CANCEL         = "🚫 Отменить заявку"
BTN_LEAVE_REQUEST  = "📝 Оставьте заявку"

# Главное меню
BTN_MY_REQUESTS = "✅ Мои заявки"
BTN_MY_STATS    = "📊 Моя статистика"
BTN_STATS       = "📈 Общая статистика"
BTN_EXPORT      = "📤 Экспорт"
BTN_SETTINGS    = "⚙️ Настройки"

# Настройки — разделы
BTN_OPEN_REQUESTS = "📋 Открытые заявки"
BTN_UNASSIGNED    = "🔍 Без исполнителя"
BTN_SPECIALISTS   = "👥 Специалисты"
BTN_BOTADMINS     = "🤖 Ботадмины"
BTN_TIMEOUT       = "⏱ Таймаут"
BTN_NOTICES       = "📢 Объявления"
BTN_DAILY         = "📅 Дайджест незакрытых"
BTN_UNASSIGNED_D  = "👁 Дайджест забытых"
BTN_STOPWORDS     = "🚫 Исключения"
BTN_AUTOCLEAN     = "🧹 Автоочистка"

# Подменю автоочистки
BTN_AUTOCLEAN_VIEW = "👁 Настройки очистки"
BTN_AUTOCLEAN_SET  = "✏️ Настроить очистку"

# Подменю заявок
BTN_REQ_VIEW      = "📋 Посмотреть заявки"
BTN_REQ_CLOSE_ALL = "🔒 Закрыть все"

# Подменю специалистов
BTN_SPEC_ADD  = "➕ Добавить специалиста"
BTN_SPEC_DEL  = "➖ Удалить специалиста"
BTN_SPEC_LIST = "📋 Список специалистов"

# Подменю ботадминов
BTN_ADM_ADD  = "➕ Добавить ботадмина"
BTN_ADM_DEL  = "➖ Удалить ботадмина"
BTN_ADM_LIST = "📋 Список ботадминов"

# Подменю таймаута
BTN_TIMEOUT_GET = "👁 Текущий таймаут"
BTN_TIMEOUT_SET = "✏️ Изменить таймаут"

# Подменю объявлений
BTN_NOTICE_LIST = "👁 Объявления"
BTN_NOTICE_ADD  = "➕ Добавить объявление"
BTN_NOTICE_DEL  = "➖ Удалить объявление"

# Подменю дайджеста незакрытых
BTN_DAILY_VIEW = "👁 Настройки незакрытых"
BTN_DAILY_SET  = "✏️ Настроить незакрытые"

# Подменю дайджеста забытых
BTN_UNASSD_VIEW = "👁 Настройки забытых"
BTN_UNASSD_SET  = "✏️ Настроить забытые"

# Подменю слов-исключений
BTN_STOP_LIST = "👁 Список исключений"
BTN_STOP_ADD  = "➕ Добавить исключение"
BTN_STOP_DEL  = "➖ Удалить исключение"

# ── Клавиатуры ───────────────────────────────────────────────────

def kb_client() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_LEAVE_REQUEST)]],
        resize_keyboard=True,
        input_field_placeholder="Опишите проблему...",
        is_persistent=True
    )

def kb_specialist() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MY_REQUESTS), KeyboardButton(text=BTN_MY_STATS)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_admin() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MY_REQUESTS), KeyboardButton(text=BTN_MY_STATS)],
            [KeyboardButton(text=BTN_STATS),       KeyboardButton(text=BTN_EXPORT)],
            [KeyboardButton(text=BTN_SETTINGS),    KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_requests_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REQ_VIEW), KeyboardButton(text=BTN_REQ_CLOSE_ALL)],
            [KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_OPEN_REQUESTS), KeyboardButton(text=BTN_UNASSIGNED)],
            [KeyboardButton(text=BTN_SPECIALISTS),   KeyboardButton(text=BTN_BOTADMINS)],
            [KeyboardButton(text=BTN_TIMEOUT),       KeyboardButton(text=BTN_NOTICES)],
            [KeyboardButton(text=BTN_DAILY),         KeyboardButton(text=BTN_UNASSIGNED_D)],
            [KeyboardButton(text=BTN_STOPWORDS),     KeyboardButton(text=BTN_AUTOCLEAN)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_autoclean_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_AUTOCLEAN_VIEW), KeyboardButton(text=BTN_AUTOCLEAN_SET)],
            [KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_specialists_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SPEC_ADD),  KeyboardButton(text=BTN_SPEC_DEL)],
            [KeyboardButton(text=BTN_SPEC_LIST), KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_botadmins_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADM_ADD),  KeyboardButton(text=BTN_ADM_DEL)],
            [KeyboardButton(text=BTN_ADM_LIST), KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_timeout_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TIMEOUT_GET), KeyboardButton(text=BTN_TIMEOUT_SET)],
            [KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_notices_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NOTICE_LIST), KeyboardButton(text=BTN_NOTICE_ADD)],
            [KeyboardButton(text=BTN_NOTICE_DEL),  KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_daily_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DAILY_VIEW), KeyboardButton(text=BTN_DAILY_SET)],
            [KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_unassigned_digest_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_UNASSD_VIEW), KeyboardButton(text=BTN_UNASSD_SET)],
            [KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def kb_stopwords_sub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STOP_LIST), KeyboardButton(text=BTN_STOP_ADD)],
            [KeyboardButton(text=BTN_STOP_DEL),  KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

async def get_role_keyboard(bot: Bot, user_id: int, chat_id: int, chat_type: str) -> ReplyKeyboardMarkup:
    roles = await check_user_role(bot, user_id, chat_id, chat_type)
    if roles["is_admin_or_botadmin"]:
        return kb_admin()
    elif roles["is_specialist"]:
        return kb_specialist()
    else:
        return kb_client()

# ── /start ───────────────────────────────────────────────────────

@router.message(Command("start"))
async def start_with_keyboard(message: types.Message) -> None:
    if message.chat.type != "private":
        await safe_send_message(message.bot, message.chat.id,
            "Добро пожаловать! Напишите описание проблемы для создания заявки.")
        return
    kb = await get_role_keyboard(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    await message.answer("Добро пожаловать! Выберите действие или напишите описание проблемы.", reply_markup=kb)

# ── Навигация ────────────────────────────────────────────────────

@router.message(F.text == BTN_BACK, F.chat.type == "private")
async def btn_back(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    kb = await get_role_keyboard(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    await message.answer("Главное меню:", reply_markup=kb)

@router.message(F.text == BTN_BACK_SETTINGS, F.chat.type == "private")
async def btn_back_settings(message: types.Message, state: FSMContext) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await state.clear()
    await message.answer("⚙️ Настройки:", reply_markup=kb_settings())

# ── Главное меню ─────────────────────────────────────────────────

@router.message(F.text == BTN_HELP, F.chat.type == "private")
async def btn_help(message: types.Message) -> None:
    from .commands import help_cmd
    await help_cmd(message)

@router.message(F.text == BTN_CANCEL, F.chat.type == "private")
async def btn_cancel(message: types.Message, state: FSMContext) -> None:
    from .commands import cancel_cmd
    await cancel_cmd(message, state)

@router.message(F.text == BTN_LEAVE_REQUEST, F.chat.type == "private")
async def btn_leave_request(message: types.Message) -> None:
    await safe_send_message(
        message.bot, message.chat.id,
        "✍️ Опишите вашу проблему — напишите следующим сообщением, и мы её обработаем."
    )

@router.message(F.text == BTN_MY_REQUESTS, F.chat.type == "private")
async def btn_my_requests(message: types.Message) -> None:
    from .commands import my_active_requests_cmd
    await my_active_requests_cmd(message)

@router.message(F.text == BTN_MY_STATS, F.chat.type == "private")
async def btn_my_stats(message: types.Message, state: FSMContext) -> None:
    from .commands import my_stats_cmd
    await my_stats_cmd(message, state)

@router.message(F.text == BTN_STATS, F.chat.type == "private")
async def btn_stats(message: types.Message) -> None:
    from .commands import stats_cmd
    await stats_cmd(message)

@router.message(F.text == BTN_EXPORT, F.chat.type == "private")
async def btn_export(message: types.Message) -> None:
    from .commands import export_cmd
    await export_cmd(message)

@router.message(F.text == BTN_SETTINGS, F.chat.type == "private")
async def btn_settings(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Недоступно.")
        return
    await message.answer("⚙️ Настройки:", reply_markup=kb_settings())

# ── Открытые заявки / Без исполнителя ───────────────────────────

_req_ctx: dict = {}

@router.message(F.text == BTN_OPEN_REQUESTS, F.chat.type == "private")
async def btn_open_requests(message: types.Message) -> None:
    _req_ctx[message.from_user.id] = "open"
    await message.answer("📋 Открытые заявки:", reply_markup=kb_requests_sub())

@router.message(F.text == BTN_UNASSIGNED, F.chat.type == "private")
async def btn_unassigned(message: types.Message) -> None:
    _req_ctx[message.from_user.id] = "unassigned"
    await message.answer("🔍 Без исполнителя:", reply_markup=kb_requests_sub())

@router.message(F.text == BTN_REQ_VIEW, F.chat.type == "private")
async def btn_req_view(message: types.Message) -> None:
    from .commands import open_requests_cmd, open_unassigned_requests_cmd
    if _req_ctx.get(message.from_user.id) == "unassigned":
        await open_unassigned_requests_cmd(message)
    else:
        await open_requests_cmd(message)

@router.message(F.text == BTN_REQ_CLOSE_ALL, F.chat.type == "private")
async def btn_req_close_all(message: types.Message) -> None:
    from .commands import close_all_cmd
    await close_all_cmd(message)

# ── Специалисты ──────────────────────────────────────────────────

@router.message(F.text == BTN_SPECIALISTS, F.chat.type == "private")
async def btn_specialists(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("👥 Специалисты:", reply_markup=kb_specialists_sub())

@router.message(F.text == BTN_SPEC_LIST, F.chat.type == "private")
async def btn_spec_list(message: types.Message) -> None:
    from .commands import list_specs_cmd
    await list_specs_cmd(message)

@router.message(F.text == BTN_SPEC_ADD, F.chat.type == "private")
async def btn_spec_add(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/add_spec @username или /add_spec &lt;user_id&gt;\n"
        "Пользователь должен состоять в рабочем чате.", parse_mode="HTML")

@router.message(F.text == BTN_SPEC_DEL, F.chat.type == "private")
async def btn_spec_del(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/del_spec @username", parse_mode="HTML")

# ── Ботадмины ────────────────────────────────────────────────────

@router.message(F.text == BTN_BOTADMINS, F.chat.type == "private")
async def btn_botadmins(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("🤖 Ботадмины:", reply_markup=kb_botadmins_sub())

@router.message(F.text == BTN_ADM_LIST, F.chat.type == "private")
async def btn_adm_list(message: types.Message) -> None:
    from .commands import list_botadm_cmd
    await list_botadm_cmd(message)

@router.message(F.text == BTN_ADM_ADD, F.chat.type == "private")
async def btn_adm_add(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/add_botadm @username или /add_botadm &lt;user_id&gt;\n"
        "Пользователь должен состоять в рабочем чате.", parse_mode="HTML")

@router.message(F.text == BTN_ADM_DEL, F.chat.type == "private")
async def btn_adm_del(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/del_botadm @username", parse_mode="HTML")

# ── Таймаут ──────────────────────────────────────────────────────

@router.message(F.text == BTN_TIMEOUT, F.chat.type == "private")
async def btn_timeout(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("⏱ Таймаут заявок:", reply_markup=kb_timeout_sub())

@router.message(F.text == BTN_TIMEOUT_GET, F.chat.type == "private")
async def btn_timeout_get(message: types.Message) -> None:
    from .commands import get_timeout_cmd
    await get_timeout_cmd(message)

@router.message(F.text == BTN_TIMEOUT_SET, F.chat.type == "private")
async def btn_timeout_set(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/set_timeout &lt;секунды&gt;\n"
        "Пример: /set_timeout 300 (5 минут)", parse_mode="HTML")

# ── Объявления ───────────────────────────────────────────────────

@router.message(F.text == BTN_NOTICES, F.chat.type == "private")
async def btn_notices(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("📢 Объявления:", reply_markup=kb_notices_sub())

@router.message(F.text == BTN_NOTICE_LIST, F.chat.type == "private")
async def btn_notice_list(message: types.Message) -> None:
    from .commands import get_notice_cmd
    await get_notice_cmd(message)

@router.message(F.text == BTN_NOTICE_ADD, F.chat.type == "private")
async def btn_notice_add(message: types.Message, state: FSMContext) -> None:
    from .commands import set_notice_cmd
    await set_notice_cmd(message, state)

@router.message(F.text == BTN_NOTICE_DEL, F.chat.type == "private")
async def btn_notice_del(message: types.Message) -> None:
    from .commands import get_notice_cmd
    await get_notice_cmd(message)
    await safe_send_message(message.bot, message.chat.id,
        "Удалить: /del_notice &lt;номер&gt; или /del_notice all",
        parse_mode="HTML")

# ── Дайджест незакрытых ──────────────────────────────────────────

@router.message(F.text == BTN_DAILY, F.chat.type == "private")
async def btn_daily(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("📅 Дайджест незакрытых:", reply_markup=kb_daily_sub())

@router.message(F.text == BTN_DAILY_VIEW, F.chat.type == "private")
async def btn_daily_view(message: types.Message) -> None:
    from .commands import get_daily_reminder_cmd
    await get_daily_reminder_cmd(message)

@router.message(F.text == BTN_DAILY_SET, F.chat.type == "private")
async def btn_daily_set(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/set_daily_reminder &lt;ЧЧ:ММ&gt; &lt;дни&gt;\n\n"
        "Пример: /set_daily_reminder 09:00 1,2,3,4,5\n\n"
        "Дни: 1-Пн 2-Вт 3-Ср 4-Чт 5-Пт 6-Сб 7-Вс\n\n"
        "Отключить: /set_daily_reminder off",
        parse_mode="HTML")

# ── Дайджест забытых ─────────────────────────────────────────────

@router.message(F.text == BTN_UNASSIGNED_D, F.chat.type == "private")
async def btn_unassigned_digest(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("👁 Дайджест забытых:", reply_markup=kb_unassigned_digest_sub())

@router.message(F.text == BTN_UNASSD_VIEW, F.chat.type == "private")
async def btn_unassd_view(message: types.Message) -> None:
    from .commands import get_unassigned_reminder_cmd
    await get_unassigned_reminder_cmd(message)

@router.message(F.text == BTN_UNASSD_SET, F.chat.type == "private")
async def btn_unassd_set(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n"
        "/set_unassigned_reminder &lt;интервал_мин&gt; &lt;дни&gt; &lt;начало&gt; &lt;конец&gt; &lt;порог_мин&gt;\n\n"
        "Пример: /set_unassigned_reminder 60 1,2,3,4,5 08:00 17:00 30\n\n"
        "Дни: 1-Пн 2-Вт 3-Ср 4-Чт 5-Пт 6-Сб 7-Вс\n\n"
        "Отключить: /set_unassigned_reminder off",
        parse_mode="HTML")

# ── Слова-исключения ─────────────────────────────────────────────

@router.message(F.text == BTN_STOPWORDS, F.chat.type == "private")
async def btn_stopwords(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("🚫 Слова-исключения:", reply_markup=kb_stopwords_sub())

@router.message(F.text == BTN_STOP_LIST, F.chat.type == "private")
async def btn_stop_list(message: types.Message) -> None:
    from .commands import list_ignore_cmd
    await list_ignore_cmd(message)

@router.message(F.text == BTN_STOP_ADD, F.chat.type == "private")
async def btn_stop_add(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/add_ignore &lt;слово&gt;", parse_mode="HTML")

@router.message(F.text == BTN_STOP_DEL, F.chat.type == "private")
async def btn_stop_del(message: types.Message) -> None:
    from .commands import list_ignore_cmd
    await list_ignore_cmd(message)
    await safe_send_message(message.bot, message.chat.id,
        "Удалить: /del_ignore &lt;слово&gt;",
        parse_mode="HTML")

# ── Автоочистка ──────────────────────────────────────────────────

@router.message(F.text == BTN_AUTOCLEAN, F.chat.type == "private")
async def btn_autoclean(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    await message.answer("🧹 Автоочистка:", reply_markup=kb_autoclean_sub())

@router.message(F.text == BTN_AUTOCLEAN_VIEW, F.chat.type == "private")
async def btn_autoclean_view(message: types.Message) -> None:
    from .commands import get_autoclean_cmd
    await get_autoclean_cmd(message)

@router.message(F.text == BTN_AUTOCLEAN_SET, F.chat.type == "private")
async def btn_autoclean_set(message: types.Message) -> None:
    await safe_send_message(message.bot, message.chat.id,
        "Введите команду:\n/set_autoclean &lt;ЧЧ:ММ&gt; &lt;дни&gt;\n\n"
        "Пример: /set_autoclean 23:00 1,2,3,4,5,6,7\n\n"
        "Дни: 1-Пн 2-Вт 3-Ср 4-Чт 5-Пт 6-Сб 7-Вс\n\n"
        "Бот удалит все свои сообщения в чатах в указанное время,\n"
        "<b>только если все заявки закрыты.</b>\n\n"
        "Отключить: /set_autoclean off",
        parse_mode="HTML")
