"""Authorize actions and persist state; the delivery worker updates both chats."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
from db import database
from utils.telegram_utils import safe_answer_callback
from .commands import check_user_role

router = Router()


async def _request(callback):
    try:
        request_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError, AttributeError):
        await safe_answer_callback(callback, "Некорректная кнопка", show_alert=True)
        return None
    request = await database.get_request_by_id(request_id)
    if not request:
        await safe_answer_callback(callback, "Заявка не найдена", show_alert=True)
    return request


async def _staff_allowed(callback, work_chat_id):
    if not callback.message or callback.message.chat.id != work_chat_id:
        await safe_answer_callback(callback, "Используйте кнопку в рабочем чате", show_alert=True)
        return False
    roles = await check_user_role(callback.bot, callback.from_user.id, work_chat_id, callback.message.chat.type)
    if not (roles["is_specialist"] or roles["is_admin_or_botadmin"]):
        await safe_answer_callback(callback, "⛔ Доступ запрещён", show_alert=True)
        return False
    return True


@router.callback_query(F.data.startswith("take:"))
async def cb_take(callback: CallbackQuery, work_chat_id: int, request_service) -> None:
    if not await _staff_allowed(callback, work_chat_id):
        return
    request = await _request(callback)
    if not request:
        return
    success = await database.assign_specialist_if_new(request[0], callback.from_user.full_name, callback.from_user.id)
    request_service.wake.set()
    await safe_answer_callback(callback, "Вы приняли заявку." if success else "Заявка уже в работе или закрыта")


@router.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery, work_chat_id: int, request_service) -> None:
    if not await _staff_allowed(callback, work_chat_id):
        return
    request = await _request(callback)
    if not request:
        return
    success = await database.close_request(request[0], callback.from_user.id)
    request_service.wake.set()
    await safe_answer_callback(callback, "Заявка закрыта." if success else "Вы не являетесь исполнителем либо заявка уже закрыта", show_alert=not success)


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery, request_service) -> None:
    request = await _request(callback)
    if not request:
        return
    if callback.from_user.id != request[1]:
        await safe_answer_callback(callback, "⛔ Только автор может отменить заявку", show_alert=True)
        return
    success = await database.cancel_request_by_id(request[0])
    request_service.wake.set()
    await safe_answer_callback(callback, "Заявка отменена" if success else "Заявка уже в работе или завершена", show_alert=not success)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await safe_answer_callback(callback, "")
