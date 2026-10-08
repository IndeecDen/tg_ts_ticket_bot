"""Административный раздел «📣 Рассылки».

Раздел доступен только администраторам в личном диалоге; права проверяются при
каждом сообщении и при каждом нажатии кнопки, а также повторно перед запуском.
Форматирование Telegram сохраняется через entities/caption_entities, вложения
передаются адресатам по file_id.
"""
import html

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from services.broadcasts import (
    STATUS_LABELS,
    BroadcastValidationError,
    content_summary,
    extract_content,
    format_schedule,
    parse_schedule,
    send_content,
    timezone_label,
)
from utils.logging_config import logger
from utils.telegram_utils import safe_answer_callback, safe_send_message

from . import common as context
from .common import BroadcastStates, check_user_role

router = Router()

BTN_BROADCAST = "📣 Рассылки"
BTN_BC_NEW = "✏ Новая рассылка"
BTN_BC_LIST = "📋 Список рассылок"

PAGE_SIZE = 5
ACCESS_DENIED = "⛔ Рассылки доступны только администраторам бота в личной переписке."

CONTENT_PROMPT = (
    "✏ <b>Новая рассылка</b>\n\n"
    "Пришлите одно сообщение: текст, фото, документ, видео или аудио "
    "(к вложению можно добавить подпись).\n\n"
    "• Форматирование берётся из вашего сообщения: жирный, курсив, подчёркивание, "
    "зачёркивание, спойлер, ссылки, код.\n"
    "• Текст — до 4096 символов, подпись — до 1024.\n"
    "• Одно вложение на рассылку: альбом не подходит.\n\n"
    "Пока вы не выберете отправку, в группы ничего не уходит."
)

SCHEDULE_PROMPT = (
    "🕒 <b>Отправка по таймеру</b>\n\n"
    "Часовой пояс бота: <b>{tz}</b>\n\n"
    "Пришлите:\n"
    "• число минут от отправки — например, <code>30</code>;\n"
    "• или дату и время — <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>.\n\n"
    "Рассылка разовая, повторов нет. Черновик сохранён, время можно ввести ещё раз."
)

_username_cache = {}


async def _bot_username(bot):
    key = id(bot)
    if key not in _username_cache:
        try:
            _username_cache[key] = (await bot.me()).username
        except Exception:
            logger.debug('Не удалось получить имя бота', exc_info=True)
            _username_cache[key] = None
    return _username_cache[key]


async def _require_admin(message, actor=None) -> bool:
    if not message or message.chat.type != "private" or message.chat.id <= 0:
        return False
    actor = actor or message.from_user
    if not actor or actor.is_bot:
        return False
    roles = await check_user_role(message.bot, actor.id, message.chat.id, message.chat.type)
    return bool(roles["is_admin_or_botadmin"])


async def _known_recipients() -> int:
    return await context.database.count_broadcast_chats(exclude_chat_ids=(context.config.work_chat_id,))


def kb_broadcast_sub() -> ReplyKeyboardMarkup:
    from .keyboards import BTN_BACK_SETTINGS
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BC_NEW), KeyboardButton(text=BTN_BC_LIST)],
            [KeyboardButton(text=BTN_BACK_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BTN_BC_NEW, callback_data="bc:new")],
        [InlineKeyboardButton(text=BTN_BC_LIST, callback_data="bc:list:0")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="bc:noop")],
    ])


def preview_markup(broadcast_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Отправить сейчас", callback_data=f"bc:send:{broadcast_id}"),
         InlineKeyboardButton(text="🕒 По таймеру", callback_data=f"bc:sched:{broadcast_id}")],
        [InlineKeyboardButton(text="✖ Отменить", callback_data=f"bc:cancel:{broadcast_id}"),
         ],
    ])


def cancel_markup(broadcast_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖ Отменить", callback_data=f"bc:cancel:{broadcast_id}"),
        InlineKeyboardButton(text="📋 Список рассылок", callback_data="bc:list:0"),
    ]])


def list_only_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=BTN_BC_LIST, callback_data="bc:list:0"),
        InlineKeyboardButton(text="✖ Отменить", callback_data="bc:abort"),
    ]])


async def _broadcast_summary(broadcast, counts) -> str:
    """One list row: number, author, state, times, recipients and every counter."""
    tz = broadcast.get('timezone') or context.config.timezone
    status = STATUS_LABELS.get(broadcast["status"], broadcast["status"])
    lines = [f'<b>Рассылка #{broadcast["id"]}</b> — {html.escape(status)}']
    if broadcast.get("reason") == "author":
        lines.append("⛔ Автор больше не администратор — отправка отменена")
    author = html.escape(str(broadcast.get("admin_name") or broadcast["admin_id"]))
    lines.append(f'👤 {author} · ID {broadcast["admin_id"]}')
    lines.append(f'🕓 Создана: {format_schedule(broadcast.get("created_at"), tz)} ({html.escape(tz)})')
    if broadcast["status"] == "draft":
        lines.append("🚀 Отправка: по кнопке у черновика")
    elif broadcast.get("scheduled_at"):
        lines.append(f'🕒 Отправка: {format_schedule(broadcast["scheduled_at"], tz)} ({html.escape(tz)})')
    elif broadcast.get("started_at"):
        lines.append(f'🚀 Начата: {format_schedule(broadcast["started_at"], tz)} ({html.escape(tz)})')
    else:
        lines.append("🚀 Отправка: —")
    if broadcast.get('finished_at'):
        lines.append(f'Завершена: {format_schedule(broadcast["finished_at"], tz)} ({html.escape(tz)})')
    lines.append(
        "📬 Получателей: {total} · ✅ Отправлено: {sent} · ⏳ Ожидают: {pending} · "
        "📤 Отправляется: {sending} · ❌ Ошибки: {errors} · ❓ Неопределённо: {uncertain}".format(
            total=counts.get("total", 0), sent=counts.get("sent", 0), pending=counts.get("pending", 0),
            sending=counts.get("sending", 0),
            errors=counts.get("failed", 0) + counts.get("blocked", 0),
            uncertain=counts.get("uncertain", 0)))
    content = {"media_type": broadcast.get("media_type"),
               "caption": broadcast.get("caption"), "text": broadcast.get("text")}
    kind = {"photo": "🖼 фото", "document": "📄 документ", "video": "🎬 видео",
            "audio": "🎵 аудио"}.get(broadcast.get("media_type"), "💬 текст")
    lines.append(f'{kind} · {html.escape(content_summary(content))}')
    return "\n".join(lines)


def _list_markup(rows, page, pages):
    keyboard = []
    for row in rows:
        keyboard.append([InlineKeyboardButton(text=f'📋 Открыть #{row["id"]}',
                                              callback_data=f'bc:show:{row["id"]}')])
        if row["status"] in ("draft", "scheduled"):
            keyboard.append([InlineKeyboardButton(text=f'✖ Отменить #{row["id"]}',
                                                  callback_data=f'bc:cancel:{row["id"]}')])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"bc:list:{page - 1}"))
    navigation.append(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"bc:list:{page}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"bc:list:{page + 1}"))
    keyboard.append(navigation)
    keyboard.append([InlineKeyboardButton(text=BTN_BC_NEW, callback_data="bc:new"),
                     InlineKeyboardButton(text="◀ Назад", callback_data="bc:noop")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _list_text(page, pages):
    database = context.database
    total = await database.count_broadcasts()
    rows = await database.list_broadcasts(PAGE_SIZE, page * PAGE_SIZE)
    lines = [f'📋 <b>Рассылки</b> · всего {total} · страница {page + 1} из {pages}']
    if not rows:
        lines.append("Рассылок пока нет. Создайте первую: «✏ Новая рассылка».")
    for row in rows:
        counts = await database.broadcast_counts(row["id"])
        lines.append("")
        lines.append(await _broadcast_summary(row, counts))
    return "\n".join(lines), rows, total


async def _render_list(callback, page: int) -> None:
    total = await context.database.count_broadcasts()
    pages = max(1, -(-total // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    text, rows, _ = await _list_text(page, pages)
    markup = _list_markup(rows, page, pages)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)


async def open_broadcast_menu(message) -> bool:
    if not await _require_admin(message):
        await safe_send_message(message.bot, message.chat.id, ACCESS_DENIED)
        return False
    known = await _known_recipients()
    await safe_send_message(
        message.bot, message.chat.id,
        '📣 <b>Рассылки</b>\n\n'
        f'Известных групп-получателей: <b>{known}</b> (чат специалистов исключён).\n'
        'Новые группы добавляются автоматически по сообщениям и событиям Telegram.',
        reply_markup=menu_markup())
    await message.answer("Выберите действие:", reply_markup=kb_broadcast_sub())
    return True


async def open_broadcast_list(message, page: int = 0) -> bool:
    if not await _require_admin(message):
        await safe_send_message(message.bot, message.chat.id, ACCESS_DENIED)
        return False
    total = await context.database.count_broadcasts()
    pages = max(1, -(-total // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    text, rows, _ = await _list_text(page, pages)
    await safe_send_message(message.bot, message.chat.id, text,
                            reply_markup=_list_markup(rows, page, pages))
    return True


async def start_new_broadcast(message, state, actor=None) -> bool:
    if not await _require_admin(message, actor):
        await safe_send_message(message.bot, message.chat.id, ACCESS_DENIED)
        return False
    await state.clear()
    await safe_send_message(message.bot, message.chat.id, CONTENT_PROMPT,
                            reply_markup=list_only_markup())
    await state.set_state(BroadcastStates.waiting_content)
    return True


async def _show_preview(message, broadcast_id: int, content, known: int) -> None:
    """The preview is an exact copy; the buttons arrive in a new message below it."""
    database = context.database
    try:
        preview = await send_content(message.bot, message.chat.id, content)
    except Exception as exc:
        logger.warning('Не удалось отправить предпросмотр рассылки #%s: %s',
                       broadcast_id, type(exc).__name__)
        await safe_send_message(
            message.bot, message.chat.id,
            f'❌ Не удалось отправить предпросмотр ({type(exc).__name__}). '
            f'Рассылка #{broadcast_id} сохранена черновиком, в группы ничего не отправлено.',
            reply_markup=cancel_markup(broadcast_id))
        return
    await database.set_broadcast_preview(broadcast_id, preview.message_id)
    await safe_send_message(
        message.bot, message.chat.id,
        f'✅ Черновик рассылки <b>#{broadcast_id}</b> готов — выше точная копия сообщения.\n\n'
        f'Известных групп-получателей: <b>{known}</b> (без чата специалистов). '
        'Окончательный список определяется в момент запуска рассылки.',
        reply_markup=preview_markup(broadcast_id))


# ── Команды ─────────────────────────────────────────────────────────────

@router.message(Command("broadcast"), F.chat.type == "private")
async def broadcast_command(message: types.Message) -> None:
    await open_broadcast_menu(message)


@router.message(Command("whoami"))
async def whoami_command(message: types.Message) -> None:
    """Register a group the bot cannot otherwise observe (privacy mode)."""
    username = await _bot_username(message.bot)
    address = f"/whoami@{username}" if username else "/whoami"
    if message.chat.type == "private":
        await safe_send_message(
            message.bot, message.chat.id,
            f"🤖 Я — <code>{html.escape(username or 'этот бот')}</code>.\n\n"
            f"Чтобы подключить группу для рассылок, отправьте в ней команду "
            f"<code>{html.escape(address)}</code> и убедитесь, что у меня есть права "
             "на отправку сообщений. Команда адресована мне лично, поэтому "
            "приватность бота не мешает.")
        return
    if message.chat.type in ("group", "supergroup"):
        if message.chat.id == await context.database.resolve_broadcast_chat(context.config.work_chat_id):
            await message.answer('Это рабочий чат специалистов: он исключён из рассылок.')
            return
        from services.broadcasts import BroadcastService
        await context.database.register_broadcast_chat(
            message.chat.id, message.chat.type, message.chat.title, source='command')
        can_send = await BroadcastService(message.bot, context.database, context.config).verify_chat(message.chat.id)
        if can_send is None:
            await message.answer('Группа сохранена. Не удалось проверить права бота; повторите /whoami позже.')
            return
        if not can_send:
            await message.answer('Группа сохранена, но бот не может отправлять сообщения. Проверьте его права.')
            return
        await safe_send_message(
            message.bot, message.chat.id,
            f'✅ Группа «{html.escape(message.chat.title or "без названия")}» '
            f"(ID {message.chat.id}) зарегистрирована как получатель рассылок.\n"
            "Список получателей фиксируется в момент запуска рассылки.")


# ── Ввод содержимого и времени ──────────────────────────────────────────

@router.message(Command('cancel'), StateFilter(BroadcastStates))
async def cancel_broadcast_command(message: types.Message, state: FSMContext) -> None:
    await cancel_preparation(message, state)

@router.message(StateFilter(BroadcastStates.waiting_content), F.chat.type == "private")
async def take_broadcast_content(message: types.Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, ACCESS_DENIED)
        return
    try:
        content = extract_content(message)
    except BroadcastValidationError as exc:
        await safe_send_message(message.bot, message.chat.id,
                                f'❗ {exc.hint}\n\nПришлите сообщение ещё раз — ничего не отправлено.',
                                reply_markup=list_only_markup())
        return
    await state.clear()
    broadcast_id = await context.database.create_broadcast(
        message.from_user.id, message.from_user.full_name, message.chat.id,
        text=content.get("text"), entities=content.get("entities"),
        media_type=content.get("media_type"), media_file_id=content.get("media_file_id"),
        caption=content.get("caption"), caption_entities=content.get("caption_entities"),
        timezone_name=context.config.timezone, options=content.get('options'),
        source_message_id=message.message_id)
    await state.set_state(BroadcastStates.preview)
    await state.update_data(broadcast_id=broadcast_id)
    await context.database.log_broadcast_audit(message.from_user.id, broadcast_id, "draft_created")
    await _show_preview(message, broadcast_id, content, await _known_recipients())


@router.message(StateFilter(BroadcastStates.waiting_schedule), F.chat.type == "private")
async def take_broadcast_schedule(message: types.Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, ACCESS_DENIED)
        return
    data = await state.get_data()
    broadcast_id = data.get("broadcast_id")
    broadcast = await context.database.get_broadcast(broadcast_id) if broadcast_id else None
    if broadcast is None or broadcast["admin_id"] != message.from_user.id:
        await state.clear()
        await safe_send_message(message.bot, message.chat.id,
                                "❌ Черновик не найден. Создайте рассылку заново.")
        return
    try:
        moment = parse_schedule(message.text, context.config.timezone)
    except BroadcastValidationError as exc:
        await safe_send_message(message.bot, message.chat.id,
                                f'❗ {exc.hint}\n\nЧерновик #{broadcast_id} сохранён.',
                                reply_markup=cancel_markup(broadcast_id))
        return
    stored = await context.database.schedule_broadcast(
        broadcast_id, moment.isoformat(), context.config.timezone)
    await state.clear()
    if not stored:
        await safe_send_message(message.bot, message.chat.id,
                                f'❌ Рассылка #{broadcast_id} уже запущена или отменена — '
                                "время не сохранено.")
        return
    await context.database.log_broadcast_audit(
        message.from_user.id, broadcast_id, "scheduled",
        f"{format_schedule(moment.isoformat(), context.config.timezone)} {context.config.timezone}")
    await safe_send_message(
        message.bot, message.chat.id,
        f'🕒 Рассылка <b>#{broadcast_id}</b> запланирована на '
        f'<b>{format_schedule(moment.isoformat(), context.config.timezone)}</b> '
        f'({html.escape(context.config.timezone)}).\n\n'
        "Перед отправкой права автора проверяются ещё раз: если автор перестанет быть "
        "администратором, рассылка отменится сама. Состояние: «📋 Список рассылок».",
        reply_markup=cancel_markup(broadcast_id))


async def _cancel_broadcast(broadcast_id, actor_id) -> str:
    database = context.database
    broadcast = await database.get_broadcast(broadcast_id)
    if broadcast is None:
        return f"❌ Рассылка #{broadcast_id} не найдена."
    result = await database.cancel_broadcast(broadcast_id, reason='manual')
    if result == 'cancelled':
        await database.log_broadcast_audit(actor_id, broadcast_id, 'cancelled', 'вручную')
        return (f'✖ Рассылка #{broadcast_id} отменена. В группы ничего не отправлено, '
                "состояние сохранено в списке.")
    if result == 'running':
        return (f'⚠️ Рассылка #{broadcast_id} уже выполняется — отменить уже отправленные '
                "сообщения нельзя. Дождитесь завершения и посмотрите результат в списке.")
    return f'ℹ️ Рассылка #{broadcast_id} уже в статусе «{STATUS_LABELS.get(result, result)}». Повторная отмена ничего не меняет.'


async def cancel_preparation(message, state) -> bool:
    """Used by /cancel: cancels the current preparation and clears the dialogue."""
    state_name = await state.get_state()
    if state_name is None:
        return False
    if not str(getattr(state_name, "state", state_name)).startswith("BroadcastStates:"):
        return False
    if not await _require_admin(message):
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, ACCESS_DENIED)
        return True
    data = await state.get_data()
    broadcast_id = data.get("broadcast_id")
    await state.clear()
    if not broadcast_id:
        await context.database.log_broadcast_audit(message.from_user.id, None, 'preparation_cancelled')
        await safe_send_message(message.bot, message.chat.id,
                                "✖ Подготовка рассылки отменена. Черновик не создан.")
        return True
    await safe_send_message(message.bot, message.chat.id,
                            await _cancel_broadcast(broadcast_id, message.from_user.id))
    return True


# ── Кнопки ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^bc:send:\d+$"))
async def start_now_callback(callback: types.CallbackQuery, broadcast_service) -> None:
    """A repeated update of the same button must never start a second broadcast."""
    broadcast_id = int((callback.data or '').split(':')[2])
    if not await _require_admin(callback.message, callback.from_user):
        await safe_answer_callback(callback, ACCESS_DENIED, show_alert=True)
        return
    broadcast = await context.database.get_broadcast(broadcast_id)
    if broadcast is None:
        await safe_answer_callback(callback, "Рассылка не найдена.", show_alert=True)
        return
    if broadcast["admin_id"] != callback.from_user.id:
        await safe_answer_callback(callback, "Это рассылка другого администратора.", show_alert=True)
        return
    outcome = await broadcast_service.start_broadcast(broadcast_id, admin_id=callback.from_user.id)
    if outcome == 'running':
        counts = await context.database.broadcast_counts(broadcast_id)
        text = (f'🚀 Рассылка #{broadcast_id} поставлена в очередь: {counts["total"]} получателей. '
                "Отправка идёт в фоне, заявки и меню продолжают работать.")
    elif outcome == 'no_recipients':
        text = (f'❌ Рассылка #{broadcast_id}: нет получателей. Бот не знает ни одной активной '
                "группы, куда можно отправлять. Подключите группу и повторите.")
    elif outcome == 'not_admin':
        text = f'⛔ Рассылка #{broadcast_id} отменена: автор больше не администратор.'
    elif outcome == 'missing':
        text = f'❌ Рассылка #{broadcast_id} не найдена.'
    elif outcome == 'role_unavailable':
        text = 'Не удалось проверить права автора. Рассылка сохранена; повторите позже.'
    else:
        text = (f'ℹ️ Рассылка #{broadcast_id} уже в статусе «'
                f'{STATUS_LABELS.get(broadcast["status"], broadcast["status"])}»: '
                "повторное нажатие вторую рассылку не создаёт.")
    await safe_answer_callback(callback, "Запускаю.", show_alert=False)
    await safe_send_message(callback.bot, callback.message.chat.id, text,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="📋 Состояние",
                                                     callback_data=f"bc:show:{broadcast_id}"),
                                InlineKeyboardButton(text="📋 Список рассылок", callback_data="bc:list:0"),
                            ]]))


@router.callback_query(F.data.startswith("bc:"))
async def broadcast_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback.message, callback.from_user):
        await safe_answer_callback(callback, ACCESS_DENIED, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    raw = parts[2] if len(parts) > 2 else ""
    if raw and not raw.isdigit():
        await safe_answer_callback(callback, "Некорректные данные кнопки.", show_alert=True)
        return
    broadcast_id = int(raw) if raw else None

    if action == 'noop':
        from .keyboards import kb_admin
        await state.clear()
        await safe_answer_callback(callback, 'Главное меню.')
        await callback.message.answer('Главное меню:', reply_markup=kb_admin())
        return
    if action == 'abort':
        data = await state.get_data()
        if data.get('broadcast_id'):
            await callback.message.answer(await _cancel_broadcast(data['broadcast_id'], callback.from_user.id))
        else:
            await context.database.log_broadcast_audit(callback.from_user.id, None, 'preparation_cancelled')
            await callback.message.answer('✖ Подготовка рассылки отменена.')
        await state.clear()
        await safe_answer_callback(callback, 'Отменено.')
        return
    if action == 'new':
        await safe_answer_callback(callback, "Новая рассылка.")
        await start_new_broadcast(callback.message, state, callback.from_user)
        return
    if action == 'list':
        await safe_answer_callback(callback, "Обновляю список.")
        await _render_list(callback, int(raw or 0))
        return
    if action == 'show' and broadcast_id:
        await safe_answer_callback(callback, "Показываю состояние.")
        broadcast = await context.database.get_broadcast(broadcast_id)
        if broadcast is None:
            await safe_send_message(callback.bot, callback.message.chat.id,
                                    f'❌ Рассылка #{broadcast_id} не найдена.')
            return
        counts = await context.database.broadcast_counts(broadcast_id)
        buttons = [[InlineKeyboardButton(text='🔄 Обновить', callback_data=f'bc:show:{broadcast_id}'),
                    InlineKeyboardButton(text='Ошибки', callback_data=f'bc:errors:{broadcast_id}:0')]]
        if broadcast['status'] == 'draft' and broadcast['admin_id'] == callback.from_user.id:
            buttons.append([InlineKeyboardButton(text='Предпросмотр', callback_data=f'bc:preview:{broadcast_id}')])
        if broadcast['status'] in ('draft', 'scheduled'):
            buttons.append([InlineKeyboardButton(text='✖ Отменить', callback_data=f'bc:cancel:{broadcast_id}')])
        buttons.append([InlineKeyboardButton(text=BTN_BC_LIST, callback_data='bc:list:0')])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await safe_send_message(callback.bot, callback.message.chat.id,
                                await _broadcast_summary(broadcast, counts), reply_markup=markup)
        return
    if action == 'errors' and broadcast_id:
        page = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() else 0
        rows = await context.database.broadcast_errors(broadcast_id, 10, page * 10)
        labels = {'forbidden': 'нет доступа', 'chat_not_found': 'чат недоступен',
                  'network': 'сеть: нужна ручная сверка', 'interrupted': 'прервано: нужна ручная сверка',
                  'server_error': 'ошибка Telegram: нужна ручная сверка', 'bad_request': 'Telegram отклонил сообщение',
                  'entity_too_large': 'превышен лимит', 'migration_duplicate': 'дублирующий адрес после миграции',
                  'migration_loop': 'некорректная миграция', 'work_chat': 'рабочий чат исключён',
                  'unexpected': 'неопределённый результат: нужна ручная сверка'}
        text = f'Ошибки рассылки #{broadcast_id} · страница {page + 1}\n' + '\n'.join(
            f'{cid}: {labels.get(error, "ошибка отправки")}' for cid, status, error in rows)
        if not rows:
            text += 'Ошибок на этой странице нет.'
        buttons = []
        if page:
            buttons.append(InlineKeyboardButton(text='◀️', callback_data=f'bc:errors:{broadcast_id}:{page-1}'))
        if len(rows) == 10:
            buttons.append(InlineKeyboardButton(text='▶️', callback_data=f'bc:errors:{broadcast_id}:{page+1}'))
        buttons.append(InlineKeyboardButton(text='Состояние', callback_data=f'bc:show:{broadcast_id}'))
        await safe_answer_callback(callback, 'Результаты.')
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]))
        return
    if broadcast_id is None:
        await safe_answer_callback(callback, "Некорректные данные кнопки.", show_alert=True)
        return

    broadcast = await context.database.get_broadcast(broadcast_id)
    if broadcast is None:
        await safe_answer_callback(callback, "Рассылка не найдена.", show_alert=True)
        return
    if broadcast["admin_id"] != callback.from_user.id:
        await safe_answer_callback(callback, "Это рассылка другого администратора.", show_alert=True)
        return

    if action == 'cancel':
        await safe_answer_callback(callback, "Отменяю.")
        await state.clear()
        await safe_send_message(callback.bot, callback.message.chat.id,
                                await _cancel_broadcast(broadcast_id, callback.from_user.id))
        return
    if action == 'preview' and broadcast['status'] == 'draft':
        await state.set_state(BroadcastStates.preview)
        await state.update_data(broadcast_id=broadcast_id)
        await safe_answer_callback(callback, 'Предпросмотр.')
        await _show_preview(callback.message, broadcast_id, broadcast, await _known_recipients())
        return
    if action == 'sched':
        if broadcast["status"] not in ("draft", "scheduled"):
            await safe_answer_callback(
                callback,
                f'Рассылка уже в статусе «{STATUS_LABELS.get(broadcast["status"], broadcast["status"])}».',
                show_alert=True)
            return
        await state.update_data(broadcast_id=broadcast_id)
        await state.set_state(BroadcastStates.waiting_schedule)
        await safe_answer_callback(callback, "Укажите время отправки.")
        await safe_send_message(
            callback.bot, callback.message.chat.id,
            SCHEDULE_PROMPT.format(tz=html.escape(timezone_label(context.config.timezone))),
            reply_markup=cancel_markup(broadcast_id))
        return
    await safe_answer_callback(callback, "Неизвестная кнопка.", show_alert=True)


# ── Реестр групп ────────────────────────────────────────────────────────

@router.my_chat_member()
async def track_chat_member(event: types.ChatMemberUpdated) -> None:
    """Keep the recipient registry in sync with added, removed and changed groups."""
    chat = event.chat
    if chat.type not in ("group", "supergroup") or chat.id == context.config.work_chat_id:
        return
    member = event.new_chat_member
    can_send = True
    if member.status == "restricted":
        can_send = bool(member.is_member and member.can_send_messages)
    if member.status == 'member':
        try:
            info = await event.bot.get_chat(chat.id)
            if getattr(info, 'permissions', None):
                can_send = bool(info.permissions.can_send_messages)
        except Exception:
            can_send = False
    await context.database.apply_broadcast_chat_member(
        chat.id, chat.type, 'left' if member.status == 'restricted' and not member.is_member else member.status,
        can_send, chat.title)


async def remember_group_messages(handler, event, data):
    """Register groups the bot sees in traffic; a forwarded origin is never registered."""
    chat = getattr(event, "chat", None)
    if chat is not None and chat.type in ("group", "supergroup"):
        try:
            if event.migrate_to_chat_id:
                await context.database.migrate_broadcast_chat(chat.id, event.migrate_to_chat_id, 'supergroup', chat.title)
            elif event.migrate_from_chat_id:
                await context.database.migrate_broadcast_chat(event.migrate_from_chat_id, chat.id, 'supergroup', chat.title)
            elif chat.id != await context.database.resolve_broadcast_chat(context.config.work_chat_id):
                await context.database.register_broadcast_chat(chat.id, chat.type, chat.title, source='message')
                if await context.database.broadcast_chat_needs_verification(chat.id):
                    from services.broadcasts import BroadcastService
                    await BroadcastService(event.bot, context.database, context.config).verify_chat(chat.id)
        except Exception:
            logger.debug('Не удалось запомнить группу %s', chat.id, exc_info=True)
    return await handler(event, data)


@router.message(StateFilter(BroadcastStates.preview), F.chat.type == 'private')
async def preview_input(message: types.Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
        await message.answer(ACCESS_DENIED)
        return
    await message.answer('Черновик сохранён. Выберите действие под предпросмотром, откройте список или /cancel.')
