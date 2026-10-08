"""Shared runtime dependencies and access checks."""
from typing import Dict
from aiogram import Bot
from aiogram.fsm.state import State, StatesGroup
from utils.logging_config import logger

database = None
config = None

class StatsStates(StatesGroup):
    waiting_for_custom_start_date = State()
    waiting_for_custom_end_date = State()


class NoticeStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_expires = State()
    waiting_for_time_range = State()


class BroadcastStates(StatesGroup):
    waiting_content = State()
    waiting_schedule = State()
    preview = State()


async def check_user_role(bot: Bot, user_id: int, chat_id: int, chat_type: str) -> Dict[str, bool]:
    """
    Проверяет роль пользователя.

    Args:
        bot: Экземпляр бота.
        user_id: ID пользователя.
        chat_id: ID чата.
        chat_type: Тип чата ('private', 'group', 'supergroup', etc.).

    Returns:
        Dict с флагами: is_client, is_specialist, is_admin_or_botadmin.
    """
    is_specialist = (await database.is_specialist(user_id))
    is_botadmin = (await database.is_botadmin(user_id))
    is_admin = False

    # Роль определяется рабочим чатом, в том числе для команд в личной переписке.
    if not is_botadmin:
        try:
            member = await bot.get_chat_member(config.work_chat_id, user_id)
            is_admin = member.status in ("creator", "administrator")
        except Exception as e:
            logger.error(f"Ошибка при проверке статуса администратора для пользователя {user_id} в чате {chat_id}: {e}")

    return {
        "is_client": not (is_specialist or is_botadmin or is_admin),
        "is_specialist": is_specialist,
        "is_admin_or_botadmin": is_botadmin or is_admin
    }
