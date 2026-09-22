from aiogram import Router, types, Bot, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import Config
from . import common as context
from .common import StatsStates, NoticeStates, check_user_role
from utils.logging_config import logger
from utils.telegram_utils import safe_send_message, safe_edit_message_text, safe_answer_callback
import openpyxl
from utils.excel import save_workbook
from openpyxl.utils import get_column_letter
from tempfile import NamedTemporaryFile
import os
from datetime import datetime
import html
import asyncio
from typing import List, Dict, Any, Optional

router = Router()


# ---------- Игнорируем сервисные сообщения (чтобы не создавались заявки) ----------
@router.message(
    F.new_chat_members |                # кто-то добавлен в чат
    F.left_chat_member |                # кто-то вышел
    F.group_chat_created |              # создана группа
    F.supergroup_chat_created |         # создан суперчат
    F.channel_chat_created |            # создан канал
    F.migrate_to_chat_id |              # миграция group -> supergroup
    F.migrate_from_chat_id |            # миграция supergroup -> group
    F.pinned_message                    # закрепление сообщения
)
async def _ignore_service_messages(message: types.Message) -> None:
    logger.debug("Сервисное сообщение проигнорировано, заявка не создаётся.")
    return
# ----------------------------------------------------------------------------------

def _is_service_message(m: types.Message) -> bool:
    return any([
        bool(getattr(m, "new_chat_members", None)),
        bool(getattr(m, "left_chat_member", None)),
        bool(getattr(m, "group_chat_created", None)),
        bool(getattr(m, "supergroup_chat_created", None)),
        bool(getattr(m, "channel_chat_created", None)),
        bool(getattr(m, "migrate_to_chat_id", None)),
        bool(getattr(m, "migrate_from_chat_id", None)),
        getattr(m, "pinned_message", None) is not None,
    ])


@router.message(Command("help"))
async def help_cmd(message: types.Message) -> None:
    """Обрабатывает команду /help."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not (roles["is_client"] or roles["is_specialist"] or roles["is_admin_or_botadmin"]):
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    response = "📋 Команды бота:\n\n"
    if roles["is_client"]:
        response += "/start — Начать работу с ботом и создать заявку\n"
        response += "/cancel — Отменить текущую заявку (если она не взята в работу)\n"
        response += "/help — Показать список команд\n"
    elif roles["is_specialist"]:
        response += "/start — Начать работу с ботом\n"
        response += "/open_requests — Показать все открытые заявки\n"
        response += "/open_unassigned_requests — Показать невзятые заявки\n"
        response += "/my_active_requests — Показать ваши заявки в работе\n"
        response += "/my_stats — Показать статистику по вашим заявкам\n"
    else:  # admin_or_botadmin
        response += "/start — Начать работу с ботом\n"
        response += "/help — Показать список команд\n"
        response += "/stats — Показать статистику\n"
        response += "/export — Экспортировать закрытые заявки в Excel\n"
        response += "/add_spec — Добавить специалиста\n"
        response += "/del_spec — Удалить специалиста\n"
        response += "/list_specs — Показать список специалистов\n"
        response += "/add_botadm — Добавить ботадмина\n"
        response += "/del_botadm — Удалить ботадмина\n"
        response += "/list_botadm — Показать список ботадминов\n"
        response += "/close_all — Закрыть все открытые заявки\n"
        response += "/open_requests — Показать все открытые заявки\n"
        response += "/open_unassigned_requests — Показать невзятые заявки\n"
        response += "/my_active_requests — Показать ваши заявки в работе\n"
        response += "/set_timeout — Установить время ожидания заявки\n"
        response += "/get_timeout — Показать текущее время ожидания\n"
        response += "/set_notice — Установить временное объявление к заявкам\n"
        response += "/del_notice — Удалить активное объявление\n"
        response += "/get_notice — Показать текущее объявление\n"
        response += "/set_reminder — Настроить интервал напоминаний по незакрытым заявкам\n"
        response += "/get_reminder — Показать текущий интервал напоминаний\n"
        response += "/set_daily_reminder — Ежедневный дайджест незакрытых заявок в рабочий чат\n"
        response += "/get_daily_reminder — Показать настройки дайджеста\n"
        response += "/set_unassigned_reminder — Напоминание о заявках без исполнителя\n"
        response += "/get_unassigned_reminder — Показать настройки напоминания о брошенных заявках\n"
        response += "/set_autoclean — Настроить автоочистку сообщений бота\n"
        response += "/get_autoclean — Показать настройки автоочистки\n"
        response += "/my_stats — Показать статистику по вашим заявкам\n"
        response += "/add_ignore — Добавить слово в игнорируемый список\n"
        response += "/del_ignore — Удалить слово из игнорируемого списка\n"
        response += "/list_ignore — Показать список игнорируемых слов\n"
    response += "\n💡 Для создания заявки отправьте сообщение с описанием проблемы (доступно клиентам)."
    await safe_send_message(message.bot, message.chat.id, response)

@router.message(Command("cancel"))
async def cancel_cmd(message: types.Message, state: FSMContext) -> None:
    """Обрабатывает команду /cancel."""
    if await state.get_state():
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, "Диалог отменён.")
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_client"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    success = (await context.database.cancel_request(user_id, chat_id))
    await safe_send_message(
        message.bot,
        message.chat.id,
        "✅ Ваша заявка отменена." if success else "❌ У вас нет активной заявки для отмены, или она уже в работе."
    )

@router.message(Command("stats"))
async def stats_cmd(message: types.Message) -> None:
    """Обрабатывает команду /stats."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="По специалистам", callback_data="stats:specialists"),
        InlineKeyboardButton(text="По клиентам", callback_data="stats:clients")
    ]])
    await safe_send_message(
        message.bot,
        message.chat.id,
        "📊 Выберите тип статистики:",
        reply_markup=keyboard
    )

@router.message(Command("export"))
async def export_cmd(message: types.Message) -> None:
    """Обрабатывает команду /export."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    data = (await context.database.get_all_closed_requests())
    if not data:
        await safe_send_message(message.bot, message.chat.id, "Нет закрытых заявок для экспорта.")
        return

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Closed Requests"
    headers = ["User ID", "Username", "Description", "Specialist", "Start Time", "End Time"]
    ws.append(headers)
    for row in data:
        ws.append(row)

    for i, _ in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = 20

    with NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp_path = tmp.name
    try:
        await asyncio.to_thread(save_workbook, wb, tmp_path)
        await message.answer_document(types.FSInputFile(tmp_path, filename="requests_export.xlsx"))
    finally:
        os.unlink(tmp_path)


@router.message(Command("open_requests"))
async def open_requests_cmd(message: types.Message) -> None:
    """Обрабатывает команду /open_requests."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not (roles["is_specialist"] or roles["is_admin_or_botadmin"]):
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    rows = (await context.database.get_open_requests())
    if not rows:
        await safe_send_message(message.bot, message.chat.id, "📭 Открытых заявок нет.")
        return
    response = "📋 Открытые заявки:\n"
    for row in rows:
        req_id, user, desc, status, specialist = row
        user_s = html.escape(user or 'без_ника')
        spec_s = html.escape(specialist) if specialist else None
        desc_s = html.escape(desc.strip()) if desc else ""
        response += f"#{req_id} от @{user_s} — {html.escape(status)}"
        if spec_s:
            response += f" (👨‍🔧 {spec_s})"
        if desc_s:
            response += f"\n  ➤ {desc_s}"
        response += "\n"
    # тут мы явно хотим обычный текст без HTML, поэтому отключаем парсинг:
    await safe_send_message(message.bot, message.chat.id, response.strip(), parse_mode="HTML")

@router.message(Command("open_unassigned_requests"))
async def open_unassigned_requests_cmd(message: types.Message) -> None:
    """Обрабатывает команду /open_unassigned_requests для показа невзятых заявок."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not (roles["is_specialist"] or roles["is_admin_or_botadmin"]):
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    rows = (await context.database.get_unassigned_requests())
    if not rows:
        await safe_send_message(message.bot, message.chat.id, "📭 Нет невзятых заявок.")
        return
    response = "📋 Невзятые заявки:\n"
    for row in rows:
        req_id, user, desc, status = row
        user_s = html.escape(user or 'без_ника')
        status_s = html.escape(status)
        desc_s = html.escape(desc.strip()) if desc else ""
        response += f"#{req_id} от @{user_s} — {status_s}"
        if desc_s:
            response += f"\n  ➤ {desc_s}"
        response += "\n"
    await safe_send_message(message.bot, message.chat.id, response.strip(), parse_mode="HTML")

@router.message(Command("my_active_requests"))
async def my_active_requests_cmd(message: types.Message) -> None:
    """Обрабатывает команду /my_active_requests для показа заявок, взятых в работу пользователем."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not (roles["is_specialist"] or roles["is_admin_or_botadmin"]):
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    specialist_name = message.from_user.full_name
    rows = (await context.database.get_active_requests_by_specialist(message.from_user.id))
    if not rows:
        await safe_send_message(message.bot, message.chat.id, "📭 У вас нет заявок в работе.")
        return
    response = "📋 Ваши заявки в работе:\n"
    for row in rows:
        req_id, user, desc, status = row
        user_s = html.escape(user or 'без_ника')
        status_s = html.escape(status)
        desc_s = html.escape(desc.strip()) if desc else ""
        response += f"#{req_id} от @{user_s} — {status_s}"
        if desc_s:
            response += f"\n  ➤ {desc_s}"
        response += "\n"
    await safe_send_message(message.bot, message.chat.id, response.strip(), parse_mode="HTML")

@router.message(Command("my_stats"))
async def my_stats_cmd(message: types.Message, state: FSMContext) -> None:
    """Обрабатывает команду /my_stats для показа статистики по заявкам текущего специалиста."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not (roles["is_specialist"] or roles["is_admin_or_botadmin"]):
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    await state.update_data(stats_type="specialist_self")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="День", callback_data="my_stats:period:day"),
            InlineKeyboardButton(text="Неделя", callback_data="my_stats:period:week")
        ],
        [
            InlineKeyboardButton(text="Месяц", callback_data="my_stats:period:month"),
            InlineKeyboardButton(text="Произвольный период", callback_data="my_stats:period:custom")
        ]
    ])
    await safe_send_message(
        message.bot,
        message.chat.id,
        "📊 Выберите временной интервал для вашей статистики:",
        reply_markup=keyboard
    )


# ---------------------------------------------------------------------------
# Ежедневный дайджест незакрытых заявок (/set_daily_reminder, /get_daily_reminder)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Напоминание о брошенных заявках (/set_unassigned_reminder, /get_unassigned_reminder)
# ---------------------------------------------------------------------------


async def _client_message(message: types.Message) -> bool:
    if not message.from_user or message.chat.id == context.config.work_chat_id:
        return False
    if message.text and message.text.startswith("/"):
        return False
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    return roles["is_client"]


async def _staff_message(message: types.Message) -> bool:
    if not message.from_user or message.chat.id == context.config.work_chat_id or message.chat.type == "private":
        return False
    if message.text and message.text.startswith("/"):
        return False
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    return roles["is_specialist"] or roles["is_admin_or_botadmin"]


@router.message(_client_message, StateFilter(None))
async def handle_description(message: types.Message, request_service) -> None:
    if _is_service_message(message):
        return
    if (await context.database.is_ignored_message(message.caption or message.text or "")):
        return
    await request_service.collect(message)


@router.message(_staff_message, StateFilter(None))
async def handle_specialist_response(message: types.Message, request_service) -> None:
    await request_service.specialist_response(message.chat.id)


@router.message(StateFilter(StatsStates.waiting_for_custom_start_date))
async def process_custom_start_date(message: types.Message, state: FSMContext) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    data = await state.get_data()
    allowed = roles["is_admin_or_botadmin"]
    allowed = allowed or (data.get("stats_type") == "specialist_self" and roles["is_specialist"])
    if not allowed:
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    """Обрабатывает ввод начальной даты для статистики."""
    logger.debug(f"Обработка ввода начальной даты от пользователя {message.from_user.id} в чате {message.chat.id}")
    try:
        datetime.strptime(message.text or '', '%Y-%m-%d')
        await state.update_data(start_date=message.text)
        await state.set_state(StatsStates.waiting_for_custom_end_date)
        await safe_send_message(
            message.bot,
            message.chat.id,
            "📅 Введите конечную дату (ГГГГ-ММ-ДД):"
        )
        logger.debug(f"Установлено состояние waiting_for_custom_end_date для пользователя {message.from_user.id} в чате {message.chat.id}")
    except ValueError:
        await safe_send_message(
            message.bot,
            message.chat.id,
            "❌ Неверный формат даты. Пожалуйста, используйте ГГГГ-ММ-ДД (например, 2025-05-13)."
        )
        logger.debug(f"Неверный формат даты от пользователя {message.from_user.id} в чате {message.chat.id}")

@router.message(StateFilter(StatsStates.waiting_for_custom_end_date))
async def process_custom_end_date(message: types.Message, state: FSMContext) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    data = await state.get_data()
    allowed = roles["is_admin_or_botadmin"]
    allowed = allowed or (data.get("stats_type") == "specialist_self" and roles["is_specialist"])
    if not allowed:
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    """Обрабатывает ввод конечной даты для статистики."""
    logger.debug(f"Обработка ввода конечной даты от пользователя {message.from_user.id} в чате {message.chat.id}")
    try:
        datetime.strptime(message.text or '', '%Y-%m-%d')
        data = await state.get_data()
        start_date = data.get('start_date')
        stats_type = data.get('stats_type', 'specialists')
        if start_date and message.text < start_date:
            raise ValueError('Дата окончания раньше начала')
        if not start_date:
            await safe_send_message(
                message.bot,
                message.chat.id,
                "❌ Ошибка: начальная дата не найдена. Попробуйте снова с /stats."
            )
            await state.clear()
            logger.error(f"Начальная дата не найдена для пользователя {message.from_user.id} в чате {message.chat.id}")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="В Telegram", callback_data=f"stats:telegram:custom:{start_date}:{message.text}:{stats_type}"),
            InlineKeyboardButton(text="Скачать Excel", callback_data=f"stats:excel:custom:{start_date}:{message.text}:{stats_type}")
        ]])
        if stats_type == "specialist_self":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="В Telegram", callback_data=f"my_stats:telegram:custom:{start_date}:{message.text}"),
                InlineKeyboardButton(text="Скачать Excel", callback_data=f"my_stats:excel:custom:{start_date}:{message.text}")
            ]])
        await safe_send_message(
            message.bot,
            message.chat.id,
            "📊 Выберите формат вывода статистики:",
            reply_markup=keyboard
        )
        await state.clear()
        logger.debug(f"Состояние очищено после ввода конечной даты для пользователя {message.from_user.id} в чате {message.chat.id}")
    except ValueError:
        await safe_send_message(
            message.bot,
            message.chat.id,
            "❌ Неверный формат даты. Пожалуйста, используйте ГГГГ-ММ-ДД (например, 2025-05-13)."
        )
        logger.debug(f"Неверный формат даты от пользователя {message.from_user.id} в чате {message.chat.id}")


from . import admin
router.include_router(admin.router)
from .admin import (
    add_botadm_cmd,
    add_ignore_cmd,
    add_spec_cmd,
    assign_request_cmd,
    close_all_cmd,
    del_botadm_cmd,
    del_ignore_cmd,
    del_notice_cmd,
    del_spec_cmd,
    get_autoclean_cmd,
    get_daily_reminder_cmd,
    get_notice_cmd,
    get_timeout_cmd,
    get_unassigned_reminder_cmd,
    list_botadm_cmd,
    list_ignore_cmd,
    list_specs_cmd,
    notice_get_expires,
    notice_get_text,
    notice_get_time_range,
    set_autoclean_cmd,
    set_daily_reminder_cmd,
    set_notice_cmd,
    set_timeout_cmd,
    set_unassigned_reminder_cmd
)
