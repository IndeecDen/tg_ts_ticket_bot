"""Collect messages durably and deliver persisted request state to Telegram."""
import asyncio
import html
from collections import defaultdict
from datetime import datetime
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError
from utils.logging_config import logger
from utils.text import clip, escaped_clip, utf16_size


def request_card(request, destination):
    request_id, user_id, username, description, status, specialist = request[:6]
    if destination == 'client':
        labels = {'new': '🔎 Ищем свободного специалиста.', 'in_progress': '🛠 Заявка в работе.',
                  'closed': '✅ Заявка закрыта. Спасибо за обращение!', 'canceled': '🚫 Заявка отменена.'}
        text = f'📩 Заявка #{request_id}\n\n{labels.get(status, status)}'
    else:
        author = escaped_clip(username or 'Клиент', 140)
        text = f'📩 Заявка #{request_id} от <a href="tg://user?id={user_id}">{author}</a>'
        chat_id, message_id = request[8:10]
        if str(chat_id).startswith('-100'):
            text += f'\n<a href="https://t.me/c/{str(chat_id)[4:]}/{message_id}">Исходное сообщение</a>'
        else:
            text += '\nОбращение из личного или обычного группового чата'
        text += '\nСтатус: ' + {'new': 'ожидает специалиста', 'in_progress': 'в работе',
                                  'closed': 'закрыта', 'canceled': 'отменена'}.get(status, status)
    if specialist:
        name = escaped_clip(specialist, 140)
        sid = request[13]
        text += f'\n👨‍🔧 <a href="tg://user?id={sid}">{name}</a>' if sid else f'\n👨‍🔧 {name} (архив без ID)'
    if destination == 'work':
        for label, start, end in (
            ('⌛ Ожидание', request[12], request[6]),
            ('⏰ Выполнение', request[6], request[7]),
        ):
            if start and end:
                try:
                    seconds = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
                    text += f'\n{label}: {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'
                except ValueError:
                    pass
        remaining = max(0, 950 - utf16_size(text) - 2)
        snippet = ''
        for char in description or '':
            candidate = snippet + html.escape(char)
            if utf16_size(candidate) > remaining - 1:
                snippet += '…'
                break
            snippet = candidate
        text += '\n\n' + snippet
    buttons = []
    if destination == 'client' and status == 'new':
        buttons = [InlineKeyboardButton(text='🚫 Отменить заявку', callback_data=f'cancel:{request_id}')]
    elif destination == 'work' and status == 'new':
        buttons = [InlineKeyboardButton(text='📌 Взять в работу', callback_data=f'take:{request_id}')]
    elif destination == 'work' and status == 'in_progress':
        buttons = [InlineKeyboardButton(text='🏁 Готово', callback_data=f'done:{request_id}')]
    return text, InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


class RequestService:
    def __init__(self, bot, database, config):
        self.bot, self.db, self.config = bot, database, config
        self.wake = asyncio.Event()
        self.slots = asyncio.Semaphore(8)

    async def collect(self, message):
        accepted = await self.db.collect_message(
            message.chat.id, message.from_user.id,
            message.from_user.username or message.from_user.full_name,
            {'text': message.caption or message.text or '', 'message_id': message.message_id,
             'photo': message.photo[-1].file_id if message.photo else None,
             'video': message.video.file_id if message.video else None,
             'document': message.document.file_id if message.document else None},
            self.config.response_timeout)
        self.wake.set()
        return accepted

    async def specialist_response(self, chat_id):
        await self.db.cancel_pending(chat_id)

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка обработки очереди заявок; повтор через 1 секунду')
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def tick(self):
        await self.db.promote_due_requests()
        grouped = defaultdict(list)
        for job in await self.db.due_deliveries():
            grouped[job[0]].append(job)
        await asyncio.gather(*(self._deliver_group(jobs) for jobs in grouped.values()))

    async def _deliver_group(self, jobs):
        async with self.slots:
            for request_id, destination, revision, attempts in jobs:
                try:
                    await self._deliver(request_id, destination, revision)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delay = max(1, exc.retry_after) if isinstance(exc, TelegramRetryAfter) else None
                    if isinstance(exc, TelegramForbiddenError):
                        delay = 3600
                    await self.db.delivery_failed(request_id, destination, revision, attempts, str(exc), delay)
                    logger.warning('Доставка заявки #%s (%s) отложена: %s', request_id, destination, type(exc).__name__)

    async def _deliver(self, request_id, destination, revision):
        request = await self.db.get_request_by_id(request_id)
        if request is None:
            return
        payload = await self.db.delivery_payload(request_id)
        media = next((m for m in payload if m.get('photo') or m.get('video') or m.get('document')), {})
        text, keyboard = request_card(request, destination)
        if destination == 'client' and request[4] == 'new':
            for notice in await self.db.get_active_announcements():
                available = 3900 - utf16_size(text)
                if available < 30:
                    break
                text += '\n📢 ' + html.escape(clip(notice[1], min(400, available // 6)))
        chat_id = self.config.work_chat_id if destination == 'work' else request[8]
        mid = request[11] if destination == 'work' else request[10]
        media_kind = next((kind for kind in ('photo', 'video', 'document') if media.get(kind)), None)
        if mid:
            try:
                if destination == 'work' and media_kind:
                    await self.bot.edit_message_caption(chat_id=chat_id, message_id=mid,
                        caption=text, reply_markup=keyboard, parse_mode='HTML')
                else:
                    try:
                        await self.bot.edit_message_text(chat_id=chat_id, message_id=mid,
                            text=text, reply_markup=keyboard, parse_mode='HTML', disable_web_page_preview=True)
                    except TelegramBadRequest as exc:
                        if destination == 'work' and 'no text in the message' in exc.message.lower():
                            await self.bot.edit_message_caption(chat_id=chat_id, message_id=mid,
                                caption=text, reply_markup=keyboard, parse_mode='HTML')
                        else:
                            raise
            except TelegramBadRequest as exc:
                error = exc.message.lower()
                if 'message is not modified' in error:
                    pass
                elif 'message to edit not found' in error:
                    mid = None
                else:
                    raise
        if not mid:
            if destination == 'work' and media_kind:
                sent = await getattr(self.bot, 'send_' + media_kind)(chat_id=chat_id,
                    **{media_kind: media[media_kind]}, caption=text, reply_markup=keyboard, parse_mode='HTML')
            else:
                sent = await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard,
                    parse_mode='HTML', disable_web_page_preview=True)
            mid = sent.message_id
        await self.db.delivery_succeeded(request_id, destination, revision, mid)
