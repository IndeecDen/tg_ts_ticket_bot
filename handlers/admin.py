"""Administration, settings and announcement dialogs."""
import asyncio
import html
from datetime import datetime
from aiogram import Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from . import common as context
from .common import check_user_role, NoticeStates
from utils.telegram_utils import safe_send_message
from utils.logging_config import logger

router = Router()
WEEKDAY_NAMES = {"1": "Пн", "2": "Вт", "3": "Ср", "4": "Чт", "5": "Пт", "6": "Сб", "7": "Вс"}

@router.message(Command("add_spec"))
async def add_spec_cmd(message: types.Message) -> None:
    """Добавляет специалиста по @username или числовому ID. Пользователь должен быть в рабочем чате."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await safe_send_message(
            message.bot, message.chat.id,
            "❗ Использование: /add_spec @username или /add_spec &lt;user_id&gt;\n"
            "Пользователь должен состоять в рабочем чате.",
            parse_mode="HTML"
        )
        return

    added = []
    skipped = []

    for raw in parts[1:]:
        # Определяем что передано — username или ID
        clean = raw.lstrip("@")
        try:
            numeric_id = int(clean)  # числовой ID
        except ValueError:
            numeric_id = None  # username — нужно резолвить

        try:
            if numeric_id is None:
                # Пробуем резолвить username через Telegram API
                try:
                    chat_user = await message.bot.get_chat(f"@{clean}")
                    numeric_id = chat_user.id
                except Exception:
                    # Fallback: ищем в БД по username из истории заявок
                    numeric_id = (await context.database.get_user_id_by_username(clean))
                if numeric_id is None:
                    skipped.append((str(raw), "не удалось определить ID — укажите числовой user_id"))
                    continue
            member = await message.bot.get_chat_member(context.config.work_chat_id, numeric_id)
            target_id = member.user.id
            display = member.user.username or member.user.full_name or str(target_id)
            if member.status in ("left", "kicked", "banned"):
                skipped.append((str(raw), "не состоит в рабочем чате"))
                continue
            if (await context.database.is_specialist(target_id)):
                skipped.append((str(raw), "уже специалист"))
                continue
            (await context.database.add_specialist(target_id, display))
            added.append(f"@{display} (ID: {target_id})")
        except Exception as e:
            logger.error(f"Ошибка при добавлении специалиста {raw}: {e}")
            skipped.append((str(raw), "не найден в рабочем чате — убедитесь что пользователь состоит в группе"))

    response = []
    if added:
        response.append("✅ Добавлены специалисты:")
        response += [f"• {u}" for u in added]
    if skipped:
        response.append("⚠️ Пропущены:")
        response += [f"• {u} ({r})" for u, r in skipped]
    await safe_send_message(
        message.bot, message.chat.id,
        "\n".join(response) if response else "❌ Никто не добавлен."
    )


@router.message(Command("del_spec"))
async def del_spec_cmd(message: types.Message) -> None:
    """Обрабатывает команду /del_spec."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 and not message.reply_to_message:
        await safe_send_message(
            message.bot, message.chat.id, "❗ Использование: /del_spec @username или ответ на сообщение специалиста"
        )
        return

    usernames = []
    if message.reply_to_message:
        usernames.append(message.reply_to_message.from_user.username or message.reply_to_message.from_user.full_name)
    else:
        for part in parts[1:]:
            usernames.append(part[1:] if part.startswith("@") else part)

    removed = []
    skipped = []

    for username in usernames:
        row = (await context.database.get_specialist_by_username(username))
        if not row:
            skipped.append((username, "не является специалистом"))
            continue
        user_id = row[0]
        (await context.database.delete_specialist(user_id))
        removed.append(username)

    result = []
    if removed:
        result.append("🗑 Удалены специалисты:")
        result += [f"• @{uname}" for uname in removed]
    if skipped:
        result.append("⚠️ Пропущены:")
        result += [f"• @{uname} ({reason})" for uname, reason in skipped]

    await safe_send_message(
        message.bot,
        message.chat.id,
        "\n".join(result) if result else "Нет подходящих специалистов для удаления."
    )


@router.message(Command("list_specs"))
async def list_specs_cmd(message: types.Message) -> None:
    """Обрабатывает команду /list_specs."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    rows = (await context.database.get_all_specialists())

    if not rows:
        await safe_send_message(message.bot, message.chat.id, "Список специалистов пуст.")
        return

    response = "👨‍💻 Список специалистов:\n"
    for uid, uname in rows:
        response += f"• @{uname} (ID: {uid})\n"

    await safe_send_message(message.bot, message.chat.id, response.strip())


@router.message(Command("add_botadm"))
async def add_botadm_cmd(message: types.Message) -> None:
    """Добавляет ботадмина по @username или числовому ID. Пользователь должен быть в рабочем чате."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await safe_send_message(
            message.bot, message.chat.id,
            "❗ Использование: /add_botadm @username или /add_botadm &lt;user_id&gt;\n"
            "Пользователь должен состоять в рабочем чате.",
            parse_mode="HTML"
        )
        return

    added = []
    skipped = []

    for raw in parts[1:]:
        clean = raw.lstrip("@")
        try:
            numeric_id = int(clean)
        except ValueError:
            numeric_id = None

        try:
            if numeric_id is None:
                try:
                    chat_user = await message.bot.get_chat(f"@{clean}")
                    numeric_id = chat_user.id
                except Exception:
                    numeric_id = (await context.database.get_user_id_by_username(clean))
                if numeric_id is None:
                    skipped.append((str(raw), "не удалось определить ID — укажите числовой user_id"))
                    continue
            member = await message.bot.get_chat_member(context.config.work_chat_id, numeric_id)
            target_id = member.user.id
            display = member.user.username or member.user.full_name or str(target_id)
            if member.status in ("left", "kicked", "banned"):
                skipped.append((str(raw), "не состоит в рабочем чате"))
                continue
            if (await context.database.is_botadmin(target_id)):
                skipped.append((str(raw), "уже ботадмин"))
                continue
            (await context.database.add_botadmin(target_id, display))
            added.append(f"@{display} (ID: {target_id})")
        except Exception as e:
            logger.error(f"Ошибка при добавлении ботадмина {raw}: {e}")
            skipped.append((str(raw), "не найден в рабочем чате — убедитесь что пользователь состоит в группе"))

    response = []
    if added:
        response.append("✅ Добавлены ботадмины:")
        response += [f"• {u}" for u in added]
    if skipped:
        response.append("⚠️ Пропущены:")
        response += [f"• {u} ({r})" for u, r in skipped]
    await safe_send_message(
        message.bot, message.chat.id,
        "\n".join(response) if response else "❌ Никто не добавлен."
    )


@router.message(Command("del_botadm"))
async def del_botadm_cmd(message: types.Message) -> None:
    """Обрабатывает команду /del_botadm."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2 and not message.reply_to_message:
        await safe_send_message(
            message.bot, message.chat.id, "❗ Использование: /del_botadm @username или ответ на сообщение ботадмина"
        )
        return

    usernames = []
    if message.reply_to_message:
        usernames.append(message.reply_to_message.from_user.username or message.reply_to_message.from_user.full_name)
    else:
        for part in parts[1:]:
            usernames.append(part[1:] if part.startswith("@") else part)

    removed = []
    skipped = []

    for username in usernames:
        row = (await context.database.get_botadmin_by_username(username))
        if not row:
            skipped.append((username, "не является ботадмином"))
            continue
        user_id = row[0]
        (await context.database.delete_botadmin(user_id))
        removed.append(username)

    result = []
    if removed:
        result.append("🗑 Удалены ботадмины:")
        result += [f"• @{uname}" for uname in removed]
    if skipped:
        result.append("⚠️ Пропущены:")
        result += [f"• @{uname} ({reason})" for uname, reason in skipped]

    await safe_send_message(
        message.bot,
        message.chat.id,
        "\n".join(result) if result else "Нет подходящих ботадминов для удаления."
    )


@router.message(Command("list_botadm"))
async def list_botadm_cmd(message: types.Message) -> None:
    """Обрабатывает команду /list_botadm."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    rows = (await context.database.get_all_botadmins())
    if not rows:
        await safe_send_message(message.bot, message.chat.id, "Список ботадминов пуст.")
        return

    response = "🤖 Список ботадминов:\n"
    for uid, uname in rows:
        response += f"• @{uname} (ID: {uid})\n"

    await safe_send_message(message.bot, message.chat.id, response.strip())


@router.message(Command("close_all"))
async def close_all_cmd(message: types.Message) -> None:
    """Обрабатывает команду /close_all."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    count = (await context.database.close_all_requests())
    await safe_send_message(message.bot, message.chat.id, f"✅ Завершено заявок: {count}")


@router.message(Command("set_timeout"))
async def set_timeout_cmd(message: types.Message) -> None:
    """Обрабатывает команду /set_timeout для изменения RESPONSE_TIMEOUT."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        await safe_send_message(
            message.bot,
            message.chat.id,
            "❗ Использование: /set_timeout <время_в_секундах>"
        )
        return
    try:
        new_timeout = int(parts[1])
        if new_timeout <= 0:
            raise ValueError("Таймаут должен быть положительным числом")
    except ValueError:
        await safe_send_message(
            message.bot,
            message.chat.id,
            "❌ Неверный формат. Укажите положительное число в секундах (например, /set_timeout 300)."
        )
        return
    if await asyncio.to_thread(context.config.update_response_timeout, new_timeout):
        await safe_send_message(
            message.bot,
            message.chat.id,
            f"✅ RESPONSE_TIMEOUT успешно изменён на {new_timeout} секунд."
        )
    else:
        await safe_send_message(
            message.bot,
            message.chat.id,
            "❌ Ошибка при изменении RESPONSE_TIMEOUT. Проверьте логи."
        )


@router.message(Command("get_timeout"))
async def get_timeout_cmd(message: types.Message) -> None:
    """Обрабатывает команду /get_timeout для просмотра текущего RESPONSE_TIMEOUT."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    await safe_send_message(
        message.bot,
        message.chat.id,
        f"Текущий RESPONSE_TIMEOUT: {context.config.response_timeout} секунд."
    )


@router.message(Command("set_notice"))
async def set_notice_cmd(message: types.Message, state: FSMContext) -> None:
    """Добавляет новое объявление к заявкам. Можно добавить несколько."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    await safe_send_message(
        message.bot, message.chat.id,
        "📝 Введите текст объявления, которое будет добавлено к каждой новой заявке:"
    )
    await state.set_state(NoticeStates.waiting_for_text)


@router.message(StateFilter(NoticeStates.waiting_for_text))
async def notice_get_text(message: types.Message, state: FSMContext) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    data = await state.get_data()
    allowed = roles["is_admin_or_botadmin"]
    if not allowed:
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    text = message.text and message.text.strip()
    if not text:
        await safe_send_message(message.bot, message.chat.id, "❗ Текст не может быть пустым. Введите текст объявления:")
        return
    await state.update_data(notice_text=text)
    await safe_send_message(
        message.bot, message.chat.id,
        "📅 Введите дату и время окончания в формате ДД.ММ.ГГГГ ЧЧ:ММ\n"
        "(например: <code>10.01.2026 18:00</code>)\n\n"
        "Или отправьте <code>-</code> чтобы установить объявление без срока.",
        parse_mode="HTML"
    )
    await state.set_state(NoticeStates.waiting_for_expires)


@router.message(StateFilter(NoticeStates.waiting_for_expires))
async def notice_get_expires(message: types.Message, state: FSMContext) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    data = await state.get_data()
    allowed = roles["is_admin_or_botadmin"]
    if not allowed:
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    raw = message.text and message.text.strip()
    expires_at = None
    if raw and raw != "-":
        try:
            expires_dt = datetime.strptime(raw, "%d.%m.%Y %H:%M")
            if expires_dt <= datetime.now():
                await safe_send_message(
                    message.bot, message.chat.id,
                    "❗ Дата окончания должна быть в будущем. Попробуйте ещё раз:"
                )
                return
            expires_at = expires_dt.isoformat()
        except ValueError:
            await safe_send_message(
                message.bot, message.chat.id,
                "❗ Неверный формат. Введите дату в виде ДД.ММ.ГГГГ ЧЧ:ММ или <code>-</code>:",
                parse_mode="HTML"
            )
            return

    await state.update_data(notice_expires=expires_at)
    await safe_send_message(
        message.bot, message.chat.id,
        "🕐 Укажите временной диапазон показа объявления в формате <code>ЧЧ:ММ-ЧЧ:ММ</code>\n"
        "(например: <code>17:00-08:00</code> — каждый день с 17:00 до 08:00)\n\n"
        "Или отправьте <code>-</code> чтобы показывать всегда.",
        parse_mode="HTML"
    )
    await state.set_state(NoticeStates.waiting_for_time_range)


@router.message(StateFilter(NoticeStates.waiting_for_time_range))
async def notice_get_time_range(message: types.Message, state: FSMContext) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    data = await state.get_data()
    allowed = roles["is_admin_or_botadmin"]
    if not allowed:
        await state.clear()
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    raw = message.text and message.text.strip()
    active_from = active_to = None

    if raw and raw != "-":
        try:
            parts = raw.split("-")
            if len(parts) != 2:
                raise ValueError
            tf = datetime.strptime(parts[0].strip(), "%H:%M").strftime("%H:%M")
            tt = datetime.strptime(parts[1].strip(), "%H:%M").strftime("%H:%M")
            if tf == tt:
                await safe_send_message(
                    message.bot, message.chat.id,
                    "❗ Начало и конец диапазона совпадают. Введите корректный диапазон или <code>-</code>:",
                    parse_mode="HTML"
                )
                return
            active_from, active_to = tf, tt
        except ValueError:
            await safe_send_message(
                message.bot, message.chat.id,
                "❗ Неверный формат. Введите диапазон вида <code>17:00-08:00</code> или <code>-</code>:",
                parse_mode="HTML"
            )
            return

    data = await state.get_data()
    notice_text = data["notice_text"]
    expires_at = data.get("notice_expires")
    created_by = message.from_user.full_name
    ann_id = (await context.database.add_announcement(notice_text, expires_at, created_by, active_from, active_to))
    await state.clear()

    expires_str = datetime.fromisoformat(expires_at).strftime("%d.%m.%Y %H:%M") if expires_at else "бессрочно"
    time_str = f"{active_from}–{active_to}" if active_from else "всегда"
    await safe_send_message(
        message.bot, message.chat.id,
        f"✅ Объявление <b>#{ann_id}</b> добавлено\n"
        f"📅 Действует до: {expires_str}\n"
        f"🕐 Показывать: {time_str}\n\n"
        f"<i>{html.escape(notice_text)}</i>\n\n"
        f"Удалить: /del_notice {ann_id}",
        parse_mode="HTML"
    )


@router.message(Command("del_notice"))
async def del_notice_cmd(message: types.Message) -> None:
    """Удаляет объявление по номеру или все сразу.
    /del_notice 2 — удалить объявление #2
    /del_notice all — удалить все объявления
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        # Показываем список если нет аргумента
        announcements = (await context.database.get_all_announcements())
        if not announcements:
            await safe_send_message(message.bot, message.chat.id, "ℹ️ Объявлений нет.")
            return
        lines = ["📢 <b>Все объявления:</b>\n"]
        for ann_id, text, expires_at, created_by, active_from, active_to in announcements:
            expires_str = datetime.fromisoformat(expires_at).strftime("%d.%m.%Y %H:%M") if expires_at else "бессрочно"
            time_str = f"🕐 {active_from}–{active_to}" if active_from else "🕐 всегда"
            lines.append(
                f"<b>#{ann_id}</b> (до {expires_str}) {time_str}: "
                f"<i>{html.escape(text[:60])}{'...' if len(text) > 60 else ''}</i>"
            )
        lines.append("\nУдалить: /del_notice &lt;номер&gt; или /del_notice all")
        await safe_send_message(message.bot, message.chat.id, "\n".join(lines), parse_mode="HTML")
        return

    arg = parts[1].lower()
    if arg == "all":
        count = (await context.database.delete_all_announcements())
        await safe_send_message(message.bot, message.chat.id,
            f"✅ Удалено объявлений: {count}." if count else "ℹ️ Активных объявлений нет.")
        return

    try:
        ann_id = int(arg)
    except ValueError:
        await safe_send_message(message.bot, message.chat.id,
            "❗ Укажите номер объявления или all. Пример: /del_notice 2")
        return

    if (await context.database.delete_announcement_by_id(ann_id)):
        await safe_send_message(message.bot, message.chat.id, f"✅ Объявление #{ann_id} удалено.")
    else:
        await safe_send_message(message.bot, message.chat.id, f"❌ Объявление #{ann_id} не найдено.")


@router.message(Command("get_notice"))
async def get_notice_cmd(message: types.Message) -> None:
    """Показывает все активные объявления."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    announcements = (await context.database.get_all_announcements())
    if not announcements:
        await safe_send_message(message.bot, message.chat.id,
            "ℹ️ Объявлений нет.\n\nДобавить: /set_notice")
        return
    lines = [f"📢 <b>Объявления ({len(announcements)}):</b>\n"]
    for ann_id, text, expires_at, created_by, active_from, active_to in announcements:
        expires_str = datetime.fromisoformat(expires_at).strftime("%d.%m.%Y %H:%M") if expires_at else "бессрочно"
        time_str = f"{active_from}–{active_to}" if active_from else "всегда"
        lines.append(
            f"<b>#{ann_id}</b> · до {expires_str} · 🕐 {time_str} · {html.escape(created_by or '')}\n"
            f"<i>{html.escape(text)}</i>"
        )
    lines.append("\nДобавить: /set_notice  |  Удалить: /del_notice &lt;номер&gt;")
    await safe_send_message(message.bot, message.chat.id, "\n\n".join(lines), parse_mode="HTML")


def _fmt_weekdays(weekdays: list) -> str:
    return ", ".join(WEEKDAY_NAMES.get(str(d), str(d)) for d in sorted(weekdays))


def _parse_weekdays(raw: str) -> list:
    """Парсит строку дней '1,2,3' или 'пн,вт,ср'. Возвращает список int 1-7."""
    ru_map = {"пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 7}
    result = []
    for part in raw.replace(" ", "").split(","):
        part = part.lower()
        if part in ru_map:
            result.append(ru_map[part])
        elif part.isdigit() and 1 <= int(part) <= 7:
            result.append(int(part))
    return sorted(set(result))


@router.message(Command("set_daily_reminder"))
async def set_daily_reminder_cmd(message: types.Message) -> None:
    """Настраивает ежедневное напоминание о незакрытых заявках в рабочем чате.
    Использование: /set_daily_reminder <ЧЧ:ММ> <дни>
    Пример: /set_daily_reminder 09:00 1,2,3,4,5
    Дни: 1=Пн 2=Вт 3=Ср 4=Чт 5=Пт 6=Сб 7=Вс
    Отключить: /set_daily_reminder off
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return

    parts = message.text.strip().split(maxsplit=2)

    if len(parts) >= 2 and parts[1].lower() == "off":
        s = (await context.database.get_daily_reminder_settings())
        (await context.database.set_daily_reminder_settings(False, s["remind_time"], s["weekdays"]))
        await safe_send_message(message.bot, message.chat.id, "✅ Ежедневный дайджест незакрытых заявок отключён.")
        return

    if len(parts) < 3:
        await safe_send_message(
            message.bot, message.chat.id,
            "❗ Использование: /set_daily_reminder &lt;ЧЧ:ММ&gt; &lt;дни&gt;\n\n"
            "Пример: /set_daily_reminder 09:00 1,2,3,4,5\n\n"
            "Дни недели:\n"
            "1 — Пн, 2 — Вт, 3 — Ср, 4 — Чт\n"
            "5 — Пт, 6 — Сб, 7 — Вс\n\n"
            "Отключить: /set_daily_reminder off",
            parse_mode="HTML"
        )
        return

    remind_time = parts[1]
    try:
        datetime.strptime(remind_time, "%H:%M")
    except ValueError:
        await safe_send_message(message.bot, message.chat.id, "❗ Время должно быть в формате ЧЧ:ММ, например 09:00")
        return

    weekdays = _parse_weekdays(parts[2])
    if not weekdays:
        await safe_send_message(message.bot, message.chat.id, "❗ Укажите хотя бы один день, например: 1,2,3,4,5")
        return

    (await context.database.set_daily_reminder_settings(True, remind_time, weekdays))
    await safe_send_message(
        message.bot, message.chat.id,
        f"✅ Ежедневный дайджест незакрытых заявок настроен:\n"
        f"• Время: {remind_time}\n"
        f"• Дни: {_fmt_weekdays(weekdays)}\n\n"
        f"Каждый день в указанное время в рабочий чат придёт список незакрытых заявок с тегами специалистов."
    )


@router.message(Command("get_daily_reminder"))
async def get_daily_reminder_cmd(message: types.Message) -> None:
    """Показывает настройки ежедневного дайджеста незакрытых заявок."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return

    s = (await context.database.get_daily_reminder_settings())
    if not s["enabled"]:
        await safe_send_message(
            message.bot, message.chat.id,
            "ℹ️ Ежедневный дайджест незакрытых заявок <b>отключён</b>.\n\n"
            "Включить: /set_daily_reminder &lt;ЧЧ:ММ&gt; &lt;дни&gt;",
            parse_mode="HTML"
        )
    else:
        await safe_send_message(
            message.bot, message.chat.id,
            f"📋 Дайджест незакрытых заявок:\n"
            f"• Статус: <b>включён</b>\n"
            f"• Время: {s['remind_time']}\n"
            f"• Дни: {_fmt_weekdays(s['weekdays'])}\n\n"
            f"Изменить: /set_daily_reminder &lt;ЧЧ:ММ&gt; &lt;дни&gt;\n"
            f"Отключить: /set_daily_reminder off",
            parse_mode="HTML"
        )


@router.message(Command("set_unassigned_reminder"))
async def set_unassigned_reminder_cmd(message: types.Message) -> None:
    """Настраивает повторяющееся напоминание о заявках без исполнителя.
    Использование: /set_unassigned_reminder <интервал_мин> <дни> <начало> <конец> <порог_мин>
    Пример: /set_unassigned_reminder 60 1,2,3,4,5 08:00 17:00 30
    Отключить: /set_unassigned_reminder off
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return

    parts = message.text.strip().split(maxsplit=5)

    if len(parts) >= 2 and parts[1].lower() == "off":
        s = (await context.database.get_unassigned_reminder_settings())
        (await context.database.set_unassigned_reminder_settings(
            False, s["interval_minutes"], s["work_start"], s["work_end"],
            s["weekdays"], s["threshold_minutes"]
        ))
        await safe_send_message(message.bot, message.chat.id, "✅ Напоминание о брошенных заявках отключено.")
        return

    if len(parts) < 6:
        await safe_send_message(
            message.bot, message.chat.id,
            "❗ Использование:\n"
            "/set_unassigned_reminder &lt;интервал_мин&gt; &lt;дни&gt; &lt;начало&gt; &lt;конец&gt; &lt;порог_мин&gt;\n\n"
            "Пример: /set_unassigned_reminder 60 1,2,3,4,5 08:00 17:00 30\n"
            "— каждые 60 мин, пн-пт, с 8:00 до 17:00, заявки старше 30 мин\n\n"
            "Дни: 1=Пн 2=Вт 3=Ср 4=Чт 5=Пт 6=Сб 7=Вс\n"
            "Отключить: /set_unassigned_reminder off",
            parse_mode="HTML"
        )
        return

    try:
        interval_minutes = int(parts[1])
        if interval_minutes <= 0:
            raise ValueError
    except ValueError:
        await safe_send_message(message.bot, message.chat.id, "❗ Интервал должен быть положительным числом минут.")
        return

    weekdays = _parse_weekdays(parts[2])
    if not weekdays:
        await safe_send_message(message.bot, message.chat.id, "❗ Укажите хотя бы один день, например: 1,2,3,4,5")
        return

    work_start = parts[3]
    work_end = parts[4]
    for t in (work_start, work_end):
        try:
            datetime.strptime(t, "%H:%M")
        except ValueError:
            await safe_send_message(message.bot, message.chat.id, f"❗ Время '{t}' должно быть в формате ЧЧ:ММ")
            return

    try:
        threshold = int(parts[5])
        if threshold <= 0:
            raise ValueError
    except ValueError:
        await safe_send_message(message.bot, message.chat.id, "❗ Порог должен быть положительным числом минут.")
        return

    (await context.database.set_unassigned_reminder_settings(True, interval_minutes, work_start, work_end, weekdays, threshold))
    await safe_send_message(
        message.bot, message.chat.id,
        f"✅ Напоминание о брошенных заявках настроено:\n"
        f"• Интервал: каждые {interval_minutes} мин.\n"
        f"• Дни: {_fmt_weekdays(weekdays)}\n"
        f"• Рабочее время: {work_start} — {work_end}\n"
        f"• Порог: заявка без исполнителя более {threshold} мин.\n\n"
        f"Бот будет слать дайджест в рабочий чат каждые {interval_minutes} мин. "
        f"в рабочее время если есть брошенные заявки."
    )


@router.message(Command("get_unassigned_reminder"))
async def get_unassigned_reminder_cmd(message: types.Message) -> None:
    """Показывает настройки напоминания о брошенных заявках."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return

    s = (await context.database.get_unassigned_reminder_settings())
    if not s["enabled"]:
        await safe_send_message(
            message.bot, message.chat.id,
            "ℹ️ Напоминание о брошенных заявках <b>отключено</b>.\n\n"
            "Включить: /set_unassigned_reminder &lt;интервал_мин&gt; &lt;дни&gt; &lt;начало&gt; &lt;конец&gt; &lt;порог_мин&gt;\n"
            "Пример: /set_unassigned_reminder 60 1,2,3,4,5 08:00 17:00 30",
            parse_mode="HTML"
        )
    else:
        await safe_send_message(
            message.bot, message.chat.id,
            f"🔔 Напоминание о брошенных заявках:\n"
            f"• Статус: <b>включено</b>\n"
            f"• Интервал: каждые {s['interval_minutes']} мин.\n"
            f"• Дни: {_fmt_weekdays(s['weekdays'])}\n"
            f"• Рабочее время: {s['work_start']} — {s['work_end']}\n"
            f"• Порог: {s['threshold_minutes']} мин. без исполнителя\n\n"
            f"Изменить: /set_unassigned_reminder &lt;интервал_мин&gt; &lt;дни&gt; &lt;начало&gt; &lt;конец&gt; &lt;порог_мин&gt;\n"
            f"Отключить: /set_unassigned_reminder off",
            parse_mode="HTML"
        )


@router.message(Command("set_autoclean"))
async def set_autoclean_cmd(message: types.Message) -> None:
    """Настраивает автоочистку сообщений бота (если все заявки закрыты).
    Использование: /set_autoclean <ЧЧ:ММ> <дни>
    Пример: /set_autoclean 23:00 1,2,3,4,5,6,7
    Отключить: /set_autoclean off
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return

    parts = message.text.strip().split(maxsplit=2)

    if len(parts) >= 2 and parts[1].lower() == "off":
        s = (await context.database.get_autoclean_settings())
        (await context.database.set_autoclean_settings(False, s["clean_time"], s["weekdays"]))
        await safe_send_message(message.bot, message.chat.id, "✅ Автоочистка отключена.")
        return

    if len(parts) < 3:
        await safe_send_message(
            message.bot, message.chat.id,
            "❗ Использование: /set_autoclean &lt;ЧЧ:ММ&gt; &lt;дни&gt;\n\n"
            "Пример: /set_autoclean 23:00 1,2,3,4,5,6,7\n\n"
            "Дни недели:\n1 — Пн, 2 — Вт, 3 — Ср, 4 — Чт\n5 — Пт, 6 — Сб, 7 — Вс\n\n"
            "Бот удалит все свои сообщения в чатах в указанное время,\n"
            "<b>только если все заявки к тому моменту закрыты.</b>\n\n"
            "Отключить: /set_autoclean off",
            parse_mode="HTML"
        )
        return

    clean_time = parts[1]
    try:
        datetime.strptime(clean_time, "%H:%M")
    except ValueError:
        await safe_send_message(message.bot, message.chat.id, "❗ Время должно быть в формате ЧЧ:ММ, например 23:00")
        return

    weekdays = _parse_weekdays(parts[2])
    if not weekdays:
        await safe_send_message(message.bot, message.chat.id, "❗ Укажите хотя бы один день, например: 1,2,3,4,5,6,7")
        return

    (await context.database.set_autoclean_settings(True, clean_time, weekdays))
    await safe_send_message(
        message.bot, message.chat.id,
        f"✅ Автоочистка настроена:\n"
        f"• Время: {clean_time}\n"
        f"• Дни: {_fmt_weekdays(weekdays)}\n\n"
        f"Бот будет удалять все свои сообщения в чатах в указанное время, "
        f"если все заявки закрыты."
    )


@router.message(Command("get_autoclean"))
async def get_autoclean_cmd(message: types.Message) -> None:
    """Показывает настройки автоочистки."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return

    s = (await context.database.get_autoclean_settings())
    if not s["enabled"]:
        await safe_send_message(
            message.bot, message.chat.id,
            "ℹ️ Автоочистка <b>отключена</b>.\n\n"
            "Включить: /set_autoclean &lt;ЧЧ:ММ&gt; &lt;дни&gt;\n"
            "Пример: /set_autoclean 23:00 1,2,3,4,5,6,7",
            parse_mode="HTML"
        )
    else:
        await safe_send_message(
            message.bot, message.chat.id,
            f"🧹 Автоочистка:\n"
            f"• Статус: <b>включена</b>\n"
            f"• Время: {s['clean_time']}\n"
            f"• Дни: {_fmt_weekdays(s['weekdays'])}\n\n"
            f"Изменить: /set_autoclean &lt;ЧЧ:ММ&gt; &lt;дни&gt;\n"
            f"Отключить: /set_autoclean off",
            parse_mode="HTML"
        )


@router.message(Command("add_ignore"))
async def add_ignore_cmd(message: types.Message) -> None:
    """Обрабатывает команду /add_ignore для добавления игнорируемых слов."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await safe_send_message(
            message.bot, message.chat.id, "❗ Использование: /add_ignore <слово> [<слово> ...]"
        )
        return

    added = []
    skipped = []

    for word in parts[1:]:
        if not word.isalpha():
            skipped.append((word, "должно содержать только буквы"))
            continue
        if (await context.database.add_ignored_word(word)):
            added.append(word)
        else:
            skipped.append((word, "уже в списке"))

    response = []
    if added:
        response.append("✅ Добавлены игнорируемые слова:")
        response += [f"• {word}" for word in added]
    if skipped:
        response.append("⚠️ Пропущены:")
        response += [f"• {word} ({reason})" for word, reason in skipped]

    await safe_send_message(
        message.bot,
        message.chat.id,
        "\n".join(response) if response else "❌ Ничего не добавлено."
    )


@router.message(Command("del_ignore"))
async def del_ignore_cmd(message: types.Message) -> None:
    """Обрабатывает команду /del_ignore для удаления игнорируемых слов."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await safe_send_message(
            message.bot, message.chat.id, "❗ Использование: /del_ignore <слово> [<слово> ...]"
        )
        return

    removed = []
    skipped = []

    for word in parts[1:]:
        if (await context.database.delete_ignored_word(word)):
            removed.append(word)
        else:
            skipped.append((word, "не найдено в списке"))

    response = []
    if removed:
        response.append("🗑 Удалены игнорируемые слова:")
        response += [f"• {word}" for word in removed]
    if skipped:
        response.append("⚠️ Пропущены:")
        response += [f"• {word} ({reason})" for word, reason in skipped]

    await safe_send_message(
        message.bot,
        message.chat.id,
        "\n".join(response) if response else "❌ Ничего не удалено."
    )


@router.message(Command("list_ignore"))
async def list_ignore_cmd(message: types.Message) -> None:
    """Обрабатывает команду /list_ignore для просмотра списка игнорируемых слов."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    roles = await check_user_role(message.bot, user_id, chat_id, chat_type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Эта команда недоступна для вашей роли.")
        return
    words = (await context.database.get_ignored_words())
    if not words:
        await safe_send_message(message.bot, message.chat.id, "📜 Список игнорируемых слов пуст.")
        return

    response = "📜 Игнорируемые слова:\n"
    response += "\n".join(f"• {word}" for word in words)
    await safe_send_message(message.bot, message.chat.id, response)


@router.message(Command("assign_request"))
async def assign_request_cmd(message: types.Message) -> None:
    roles = await check_user_role(message.bot, message.from_user.id, message.chat.id, message.chat.type)
    if not roles["is_admin_or_botadmin"]:
        await safe_send_message(message.bot, message.chat.id, "⛔ Доступ запрещён.")
        return
    try:
        _, request_id, user_id = message.text.split()
        request_id, user_id = int(request_id), int(user_id)
        member = await message.bot.get_chat_member(context.config.work_chat_id, user_id)
        target_roles = await check_user_role(message.bot, user_id, context.config.work_chat_id, "supergroup")
        if not (target_roles["is_specialist"] or target_roles["is_admin_or_botadmin"]):
            raise ValueError("Исполнитель должен быть специалистом или администратором")
    except (ValueError, TypeError):
        await safe_send_message(message.bot, message.chat.id,
            "Использование: /assign_request &lt;номер заявки&gt; &lt;ID специалиста&gt;")
        return
    success = (await context.database.bind_specialist(request_id, user_id, member.user.full_name))
    await safe_send_message(message.bot, message.chat.id,
        "✅ Исполнитель назначен по ID." if success else "Заявка не найдена или не находится в работе.")
