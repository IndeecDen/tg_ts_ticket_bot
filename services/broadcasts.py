"""Broadcast content validation, exact preview/sending and the background worker.

Formatting entered in the Telegram client is preserved with ``entities`` /
``caption_entities`` (offsets are UTF-16 code units), ``parse_mode`` is never
mixed in, and attachments are replayed from their ``file_id``.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import MessageEntity, LinkPreviewOptions

from utils.logging_config import logger
from utils.text import utf16_size

# Text limits count Unicode characters; entity offsets alone use UTF-16 units.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
SUPPORTED_MEDIA = ('photo', 'document', 'video', 'audio')
MAX_SCHEDULE_MINUTES = 525600  # one year
JOB_BATCH = 20

STATUS_LABELS = {
    'draft': 'черновик',
    'scheduled': 'запланирована',
    'running': 'выполняется',
    'completed': 'завершена',
    'completed_with_errors': 'завершена с ошибками',
    'cancelled': 'отменена',
    'no_recipients': 'нет получателей',
}

CANCEL_REASONS = {
    'author': 'автор больше не администратор',
    'manual': 'отменена вручную',
    'no_known_groups': 'нет известных групп-получателей',
}


class BroadcastValidationError(Exception):
    """Input the administrator has to fix; ``hint`` is safe to show as is."""

    def __init__(self, hint):
        super().__init__(hint)
        self.hint = hint


def resolve_timezone(name):
    """Configuration is validated at startup; never silently change the timezone."""
    return ZoneInfo(name or 'UTC')


def timezone_label(name):
    tz = resolve_timezone(name)
    now = datetime.now(tz)
    return f'{name} (UTC{now.strftime("%z")[:3]}:{now.strftime("%z")[3:] or "00"})'


def serialize_entities(entities):
    """Store entities as plain JSON so drafts survive a restart."""
    if not entities:
        return []
    result = []
    for entity in entities:
        if hasattr(entity, 'model_dump'):
            result.append(entity.model_dump(exclude_none=True))
        elif isinstance(entity, dict):
            result.append({key: value for key, value in entity.items() if value is not None})
    return result


def build_entities(raw):
    return [MessageEntity(**item) for item in raw or []]


def _python_index(text, utf16_offset):
    """Return the Python index for a UTF-16 offset, or None inside a surrogate pair."""
    units = 0
    for index, char in enumerate(text):
        if units == utf16_offset:
            return index
        units += utf16_size(char)
    return len(text) if units == utf16_offset else None


def check_text(text, entities, limit, kind, allow_empty=False):
    """Reject empty or oversized text instead of silently truncating it."""
    size = len(text)
    if not text.strip() and not allow_empty:
        raise BroadcastValidationError('Текст не может быть пустым.')
    if size > limit:
        raise BroadcastValidationError(
            f'{kind} — {size} символов, а Telegram допускает {limit}. '
            f'Сократите примерно на {size - limit} и отправьте заново — обрезать форматирование бот не будет.')
    for entity in entities or []:
        offset, length = int(entity.get('offset', 0)), int(entity.get('length', 0))
        if offset < 0 or length <= 0 or offset + length > utf16_size(text):
            raise BroadcastValidationError('Форматирование не совпадает с текстом — пересоздайте сообщение.')
        if _python_index(text, offset) is None or _python_index(text, offset + length) is None:
            raise BroadcastValidationError(
                'Форматирование разрывает эмодзи — пересоздайте сообщение, сделав выделение целиком.')


def extract_content(message):
    """Build an exact, replayable copy of one Telegram message."""
    if getattr(message, 'media_group_id', None):
        raise BroadcastValidationError(
            'Это альбом из нескольких вложений. Рассылка поддерживает одно вложение — '
            'пришлите фото, документ, видео или аудио по одному.')
    caption = getattr(message, 'caption', None)
    caption_entities = serialize_entities(getattr(message, 'caption_entities', None))
    if message.animation or message.sticker or message.voice or message.video_note:
        raise BroadcastValidationError('Этот тип вложения бот не поддерживает. Пришлите фото, документ, видео или аудио.')
    if message.photo:
        # The largest size is the original upload.
        best = max(message.photo, key=lambda photo: (photo.width * photo.height, photo.file_size or 0))
        media_type, file_id = 'photo', best.file_id
    elif message.document:
        media_type, file_id = 'document', message.document.file_id
    elif message.video:
        media_type, file_id = 'video', message.video.file_id
    elif message.audio:
        media_type, file_id = 'audio', message.audio.file_id
    elif message.text:
        media_type, file_id = None, None
    else:
        raise BroadcastValidationError(
            'Такой тип сообщения рассылка не поддерживает. Подойдут текст, фото, документ, видео или аудио. '
            'Приложение не отброшено — ничего не отправлено.')
    content = {'text': message.text if media_type is None else None,
               'entities': serialize_entities(getattr(message, 'entities', None)) if media_type is None else [],
               'media_type': media_type, 'media_file_id': file_id,
                'caption': caption, 'caption_entities': caption_entities,
                'options': {}}
    if media_type in ('photo', 'video'):
        content['options'] = {'has_spoiler': bool(message.has_media_spoiler),
                              'show_caption_above_media': bool(message.show_caption_above_media)}
    if media_type is None:
        content['options']['link_preview_options'] = (
            message.link_preview_options.model_dump(exclude_none=True)
            if message.link_preview_options else {'is_disabled': True})
    return validate_content(content)


def validate_content(content):
    if content.get('media_type'):
        if content['media_type'] not in SUPPORTED_MEDIA:
            raise BroadcastValidationError('Тип вложения не поддерживается.')
        if not content.get('media_file_id'):
            raise BroadcastValidationError(
                'Не удалось получить вложение для повторной отправки. Пришлите файл ещё раз.')
        check_text(content.get('caption') or '', content.get('caption_entities') or [],
                   CAPTION_LIMIT, 'Подпись', allow_empty=True)
    else:
        check_text(content.get('text') or '', content.get('entities') or [], TEXT_LIMIT, 'Текст')
    return content


async def send_content(bot, chat_id, content, **extra):
    """Send a draft as an exact copy of what the administrator prepared."""
    media_type = content.get('media_type')
    options = dict(content.get('options') or {})
    if 'link_preview_options' in options:
        options['link_preview_options'] = LinkPreviewOptions(**options['link_preview_options'])
    if media_type:
        entities = build_entities(content.get('caption_entities'))
        return await getattr(bot, 'send_' + media_type)(
            chat_id=chat_id, caption=content.get('caption') or '', caption_entities=entities or None,
            parse_mode=None, **options, **{media_type: content['media_file_id']}, **extra)
    entities = build_entities(content.get('entities'))
    return await bot.send_message(chat_id=chat_id, text=content['text'],
                                 entities=entities or None, parse_mode=None, **options, **extra)


def content_summary(content, budget=60):
    if content.get('media_type'):
        head = (content.get('caption') or '').strip() or 'без подписи'
    else:
        head = (content.get('text') or '').strip()
    head = ' '.join(head.split())
    if utf16_size(head) > budget:
        return head[:budget] + '…'
    return head


def parse_schedule(raw, timezone_name, now=None):
    """Parse "30" (minutes ahead) or "ГГГГ-ММ-ДД ЧЧ:ММ" in the bot timezone."""
    tz = resolve_timezone(timezone_name)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    text = (raw or '').strip()
    if re.fullmatch(r'[0-9]{1,7}', text):
        minutes = int(text)
        if minutes < 1 or minutes > MAX_SCHEDULE_MINUTES:
            raise BroadcastValidationError(
                f'Укажите число минут от 1 до {MAX_SCHEDULE_MINUTES} (например, 30).')
        return now + timedelta(minutes=minutes)
    match = re.fullmatch(r'([0-9]{4})-([0-9]{2})-([0-9]{2})\s+([0-9]{1,2}):([0-9]{2})', text)
    if not match:
        raise BroadcastValidationError(
            'Формат не распознан. Пришлите число минут (например, 30) или дату в виде ГГГГ-ММ-ДД ЧЧ:ММ.')
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        naive = datetime(year, month, day, hour, minute)
    except ValueError:
        raise BroadcastValidationError('Такой даты не существует. Проверьте ГГГГ-ММ-ДД ЧЧ:ММ.')
    local = naive.replace(tzinfo=tz)
    if local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        raise BroadcastValidationError(
            f'В {timezone_name} в это время часы переводятся вперёд — время не существует. Выберите другое.')
    if naive.replace(tzinfo=tz, fold=0).utcoffset() != naive.replace(tzinfo=tz, fold=1).utcoffset():
        raise BroadcastValidationError(
            f'В {timezone_name} в это время местное время повторяется дважды. Выберите время после перевода часов.')
    if local.astimezone(timezone.utc) <= now:
        raise BroadcastValidationError('Указанное время уже прошло. Пришлите время в будущем.')
    return local.astimezone(timezone.utc)


def format_schedule(moment, timezone_name):
    if not moment:
        return '—'
    tz = resolve_timezone(timezone_name)
    try:
        return datetime.fromisoformat(moment).astimezone(tz).strftime('%d.%m.%Y %H:%M')
    except ValueError:
        return '—'


class BroadcastService:
    """Background worker: starts due broadcasts and delivers their queued jobs."""

    def __init__(self, bot, database, config):
        self.bot, self.db, self.config = bot, database, config
        self.wake = asyncio.Event()
        self.interval = max(0.0, float(getattr(config, 'broadcast_send_interval', 0.05) or 0))
        self.retry_until = 0.0
        self._tick_lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def run(self):
        interrupted = await self.db.restore_broadcasts_on_start()
        if interrupted:
            logger.warning('После перезапуска %s отправок рассылки имеют неопределённый результат '
                           'и ждут ручной сверки', interrupted)
        await self.db.finalize_broadcasts()
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка обработки очереди рассылок; повтор через 1 секунду')
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    def notify(self):
        self.wake.set()

    async def tick(self):
        async with self._tick_lock:
            await self._tick()

    async def _tick(self):
        if await self.db.broadcast_delivery_paused():
            return
        await self._verify_candidates()
        await self._start_due()
        loop = asyncio.get_running_loop()
        handled = 0
        while loop.time() >= self.retry_until and handled < JOB_BATCH:
            job = await self.db.claim_broadcast_job(interval=self.interval)
            if job is None:
                break
            await self._deliver(job)
            handled += 1
            if self.interval:
                await asyncio.sleep(self.interval)
        await self.db.finalize_broadcasts()
        if handled >= JOB_BATCH:
            self.wake.set()

    # ── scheduling ────────────────────────────────────────────────────────

    async def _verify_candidates(self):
        """Resolve chats recovered from old requests: keep groups, drop everything else."""
        for chat_id in await self.db.get_broadcast_chat_candidates():
            if await self.db.broadcast_delivery_paused():
                break
            await self.verify_chat(chat_id)
            if self.interval:
                await asyncio.sleep(self.interval)

    async def verify_chat(self, chat_id):
        try:
            chat = await self.bot.get_chat(chat_id)
            if chat.type not in ('group', 'supergroup'):
                await self.db.disable_broadcast_chat(chat_id, reason=f'type_{chat.type}')
                return False
            member = await self.bot.get_chat_member(chat_id, self.bot.id)
        except TelegramMigrateToChat as exc:
            await self.db.migrate_broadcast_chat(chat_id, exc.migrate_to_chat_id, 'supergroup')
            return False
        except TelegramRetryAfter as exc:
            delay = max(1, exc.retry_after)
            self.retry_until = asyncio.get_running_loop().time() + delay
            await self.db.pause_broadcast_delivery(delay)
            return None
        except (TelegramForbiddenError, TelegramBadRequest):
            await self.db.disable_broadcast_chat(chat_id)
            return False
        except (TelegramAPIError, asyncio.TimeoutError, OSError):
            if await self.db.broadcast_chat_needs_verification(chat_id):
                await self.db.set_broadcast_chat_can_send(chat_id, False)
            return None
        except Exception as exc:
            logger.warning('Ошибка проверки чата %s: %s', chat_id, type(exc).__name__)
            return None
        active = member.status in ('creator', 'administrator', 'member') or (
            member.status == 'restricted' and member.is_member)
        can_send = getattr(member, 'can_send_messages', True)
        permissions = getattr(chat, 'permissions', None)
        if member.status == 'member' and permissions:
            can_send = bool(permissions.can_send_messages)
        await self.db.apply_broadcast_chat_member(
            chat_id, chat.type, member.status if active else 'left', can_send, chat.title, source='verified')
        return active and can_send

    async def start_broadcast(self, broadcast_id, admin_id=None, due_only=False):
        """Launch a broadcast after re-checking the author's role."""
        broadcast = await self.db.get_broadcast(broadcast_id)
        if broadcast is None:
            return 'missing'
        if broadcast['status'] not in ('draft', 'scheduled'):
            return None
        if due_only and (broadcast['status'] != 'scheduled' or not broadcast['scheduled_at']):
            return None
        if admin_id is not None and admin_id != broadcast['admin_id']:
            return 'not_owner'
        allowed = await self.author_is_admin(broadcast['admin_id'], broadcast['admin_chat_id'])
        if allowed is None:
            return 'role_unavailable'
        if not allowed:
            await self.db.cancel_broadcast(broadcast_id, reason='author')
            await self.db.log_broadcast_audit(broadcast['admin_id'], broadcast_id, 'auto_cancelled', 'author')
            await self._notify_author(
                broadcast, f'⛔ Рассылка #{broadcast_id} отменена: автор больше не администратор.')
            return 'not_admin'
        outcome = await self.db.start_broadcast(
            broadcast_id, exclude_chat_ids=(self.config.work_chat_id,),
            expected_schedule=broadcast['scheduled_at'] if due_only else None)
        if outcome == 'no_recipients':
            await self._notify_author(broadcast,
                                      f'❌ Рассылка #{broadcast_id}: нет получателей — '
                                      f'боту не известна ни одна активная группа.')
        elif outcome == 'running':
            await self.db.log_broadcast_audit(broadcast['admin_id'], broadcast_id, 'started',
                                            'timer' if due_only else 'manual')
            self.wake.set()
        return outcome

    async def _start_due(self):
        for broadcast_id in await self.db.due_broadcasts():
            await self.start_broadcast(broadcast_id, due_only=True)

    async def author_is_admin(self, admin_id, admin_chat_id=None):
        """Roles are re-read from the database and Telegram before every launch."""
        if await self.db.is_botadmin(admin_id):
            return True
        try:
            member = await self.bot.get_chat_member(self.config.work_chat_id, admin_id)
            return member.status in ('creator', 'administrator')
        except TelegramRetryAfter as exc:
            await self.db.pause_broadcast_delivery(max(1, exc.retry_after))
            return None
        except Exception as exc:
            logger.warning('Не удалось проверить права автора %s: %s', admin_id, type(exc).__name__)
            return None  # Retry verification later; an API outage is not revocation.

    async def _notify_author(self, broadcast, text):
        try:
            await self.bot.send_message(chat_id=broadcast['admin_chat_id'], text=text)
        except Exception:
            logger.warning('Не удалось сообщить автору рассылки #%s', broadcast['id'])

    # ── delivery ──────────────────────────────────────────────────────────

    async def _deliver(self, job):
        job_id, chat_id = job['job_id'], job['chat_id']
        content = {key: job[key] for key in
                   ('text', 'entities', 'media_type', 'media_file_id', 'caption', 'caption_entities', 'options')}
        if chat_id == await self.db.resolve_broadcast_chat(self.config.work_chat_id):
            await self.db.set_broadcast_job_result(job_id, 'blocked', error='work_chat')
            return
        access = await self.verify_chat(chat_id)
        if access is None:
            await self.db.set_broadcast_job_result(job_id, 'pending', error='access_check', retry_in=30)
            return
        if not access:
            # A confirmed migration may already have moved this job to the new ID.
            if await self.db.resolve_broadcast_chat(chat_id) != chat_id:
                await self.db.set_broadcast_job_result(job_id, 'pending', error='migrated', retry_in=1)
            else:
                await self.db.set_broadcast_job_result(job_id, 'blocked', error='forbidden')
            return
        try:
            sent = await send_content(self.bot, chat_id, content)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            delay = max(1, int(getattr(exc, 'retry_after', 1) or 1))
            self.retry_until = asyncio.get_running_loop().time() + delay
            await self.db.pause_broadcast_delivery(delay)
            await self.db.set_broadcast_job_result(job_id, 'pending', error='retry_after', retry_in=delay)
            logger.warning('Telegram требует паузу %s с; рассылка в чат %s отложена', delay, chat_id)
        except TelegramMigrateToChat as exc:
            if int(exc.migrate_to_chat_id) == int(chat_id):
                await self.db.set_broadcast_job_result(job_id, 'failed', error='migration_loop')
            else:
                await self.db.migrate_broadcast_chat(chat_id, exc.migrate_to_chat_id, 'supergroup')
                await self.db.set_broadcast_job_result(job_id, 'pending', error='migrated', retry_in=1)
                self.wake.set()
        except TelegramForbiddenError:
            await self.db.set_broadcast_chat_can_send(chat_id, False)
            await self.db.set_broadcast_job_result(job_id, 'blocked', error='forbidden')
            logger.warning('Нет доступа к чату %s: бот заблокирован или удалён', chat_id)
        except TelegramEntityTooLarge:
            await self.db.set_broadcast_job_result(job_id, 'failed', error='entity_too_large')
        except TelegramBadRequest as exc:
            message = str(getattr(exc, 'message', '') or '').lower()
            if 'chat not found' in message or 'bot was kicked' in message:
                await self.db.set_broadcast_chat_can_send(chat_id, False)
                await self.db.set_broadcast_job_result(job_id, 'blocked', error='chat_not_found')
            else:
                await self.db.set_broadcast_job_result(job_id, 'failed', error='bad_request')
            logger.warning('Telegram отклонил отправку в чат %s: %s', chat_id, type(exc).__name__)
        except (TelegramNetworkError, asyncio.TimeoutError, OSError):
            # The request may already have been accepted: no blind retry, manual review only.
            await self.db.set_broadcast_job_result(job_id, 'uncertain', error='network')
            logger.warning('Неопределённый результат отправки в чат %s — нужна ручная сверка', chat_id)
        except TelegramServerError:
            await self.db.set_broadcast_job_result(job_id, 'uncertain', error='server_error')
        except Exception as exc:
            logger.warning('Непредвиденная ошибка отправки рассылки в чат %s: %s', chat_id, type(exc).__name__)
            await self.db.set_broadcast_job_result(job_id, 'uncertain', error='unexpected')
        else:
            await self.db.set_broadcast_job_result(job_id, 'sent',
                                                   message_id=getattr(sent, 'message_id', None))
