from typing import Optional
from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from utils.logging_config import logger
from utils.text import plain_text, split_text


async def _send(bot, chat_id, text, parse_mode, **kwargs):
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, **kwargs)
    except TelegramBadRequest as exc:
        if parse_mode == 'HTML' and "can't parse entities" in exc.message.lower():
            return await bot.send_message(chat_id=chat_id, text=plain_text(text), parse_mode=None, **kwargs)
        raise


async def safe_send_message(bot: Bot, chat_id: int, text: str,
                            parse_mode: Optional[str] = 'HTML', on_sent=None, **kwargs) -> types.Message:
    first = None
    for index, chunk in enumerate(split_text(text, parse_mode)):
        options = dict(kwargs)
        if index:
            options.pop('reply_markup', None)
        sent = await _send(bot, chat_id, chunk, parse_mode, **options)
        if on_sent:
            await on_sent(sent.message_id)
        if first is None:
            first = sent
    return first


async def safe_edit_message_text(bot: Bot, chat_id: int, message_id: int, text: str,
                                 parse_mode: Optional[str] = 'HTML', **kwargs) -> None:
    chunks = split_text(text, parse_mode)
    try:
        await bot.edit_message_text(text=chunks[0], chat_id=chat_id, message_id=message_id,
                                    parse_mode=parse_mode, **kwargs)
    except TelegramBadRequest as exc:
        if 'message is not modified' in exc.message.lower():
            pass
        elif parse_mode == 'HTML' and "can't parse entities" in exc.message.lower():
            await bot.edit_message_text(text=plain_text(chunks[0]), chat_id=chat_id,
                                        message_id=message_id, parse_mode=None, **kwargs)
        else:
            raise
    for chunk in chunks[1:]:
        await _send(bot, chat_id, chunk, parse_mode)


async def safe_answer_callback(callback: types.CallbackQuery, text: str, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning('Не удалось ответить на callback: %s', exc)
