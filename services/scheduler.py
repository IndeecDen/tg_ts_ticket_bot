import asyncio
import html
from datetime import datetime
from typing import Optional
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import Config
from db.database import Database
from utils.logging_config import logger
from utils.telegram_utils import safe_send_message



async def daily_reminder_loop(bot: Bot, db: Database, config: Config) -> None:
    logger.info("Фоновая задача ежедневного дайджеста запущена.")
    last_sent_date = None
    while True:
        try:
            await asyncio.sleep(30)
            s = (await db.get_daily_reminder_settings())
            if not s["enabled"]:
                continue
            now = datetime.now()
            if now.isoweekday() not in s["weekdays"]:
                continue
            remind_h, remind_m = map(int, s["remind_time"].split(":"))
            today = now.date()
            if today == last_sent_date:
                continue
            if not (now.hour == remind_h and now.minute == remind_m):
                continue
            requests = (await db.get_inprogress_requests_for_daily())
            if not requests:
                continue
            bare_work_chat_id = str(config.work_chat_id).removeprefix("-100")
            lines = ["📋 <b>Незакрытые заявки:</b>\n"]
            for req_id, specialist, specialist_id, start_time, description, user_id, username, chat_id, work_message_id, message_id in requests:
                try:
                    elapsed = (datetime.now() - datetime.fromisoformat(start_time)).total_seconds()
                    hours = int(elapsed // 3600)
                    mins = int((elapsed % 3600) // 60)
                    elapsed_str = f"{hours}ч {mins}мин" if hours else f"{mins}мин"
                except Exception:
                    elapsed_str = "н/д"
                spec_tag = (f'<a href="tg://user?id={specialist_id}">{html.escape(specialist)}</a>'
                            if specialist_id else html.escape(specialist or "н/д"))
                author_tag = (f'<a href="tg://user?id={user_id}">{html.escape(username or "клиент")}</a>'
                              if user_id else html.escape(username or "клиент"))
                # #номер → сообщение в чате специалистов
                msg_link = (f'<a href="https://t.me/c/{bare_work_chat_id}/{work_message_id}">#{req_id}</a>'
                            if work_message_id else f"#{req_id}")
                # 💬 Название чата → исходное сообщение пользователя в клиентском чате
                try:
                    chat_info = await bot.get_chat(chat_id)
                    chat_title = html.escape(chat_info.title or f"Чат {chat_id}")
                    bare_client_chat_id = str(chat_id).removeprefix("-100")
                    chat_link = (f'<a href="https://t.me/c/{bare_client_chat_id}/{message_id}">{chat_title}</a>'
                                 if message_id and str(chat_id).startswith("-100") else chat_title)
                except Exception:
                    chat_link = f"Чат {chat_id}"
                desc_short = html.escape((description or "")[:80])
                lines.append(
                    f"• {msg_link} | в работе {elapsed_str}\n"
                    f"  👨‍🔧 {spec_tag} | 👤 {author_tag} | 💬 {chat_link}\n"
                    f"  {desc_short}"
                )
            sent = await safe_send_message(bot, on_sent=db.log_work_message, chat_id=config.work_chat_id, text="\n".join(lines),
                                          parse_mode="HTML", disable_web_page_preview=True)
            last_sent_date = today
            logger.info(f"Ежедневный дайджест отправлен: {len(requests)} незакрытых заявок.")
        except asyncio.CancelledError:
            logger.info("Фоновая задача ежедневного дайджеста остановлена.")
            break
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче ежедневного дайджеста: {e}")


async def unassigned_reminder_loop(bot: Bot, db: Database, config: Config) -> None:
    """
    Напоминание о заявках без исполнителя каждые interval_minutes
    в рабочие часы (work_start..work_end) по заданным дням недели.
    """
    logger.info("Фоновая задача напоминания о брошенных заявках запущена.")
    last_sent: Optional[datetime] = None
    while True:
        try:
            await asyncio.sleep(60)
            s = (await db.get_unassigned_reminder_settings())
            if not s["enabled"]:
                continue

            now = datetime.now()

            # Проверяем день недели
            if now.isoweekday() not in s["weekdays"]:
                continue

            # Проверяем рабочие часы
            work_start_h, work_start_m = map(int, s["work_start"].split(":"))
            work_end_h, work_end_m = map(int, s["work_end"].split(":"))
            now_minutes = now.hour * 60 + now.minute
            if not (work_start_h * 60 + work_start_m <= now_minutes < work_end_h * 60 + work_end_m):
                continue

            # Проверяем интервал с момента последней отправки
            if last_sent is not None:
                elapsed_min = (now - last_sent).total_seconds() / 60
                if elapsed_min < s["interval_minutes"]:
                    continue

            requests = (await db.get_unassigned_requests_overdue(s["threshold_minutes"]))
            if not requests:
                continue

            bare_work_chat_id = str(config.work_chat_id).removeprefix("-100")
            lines = [f"🔔 <b>Заявки без исполнителя (более {s['threshold_minutes']} мин.):</b>\n"]
            for req_id, username, description, created_time, user_id, chat_id, work_message_id, message_id in requests:
                try:
                    elapsed_sec = (now - datetime.fromisoformat(created_time)).total_seconds()
                    hours = int(elapsed_sec // 3600)
                    mins = int((elapsed_sec % 3600) // 60)
                    elapsed_str = f"{hours}ч {mins}мин" if hours else f"{mins}мин"
                except Exception:
                    elapsed_str = "н/д"
                desc_short = html.escape((description or "")[:60])

                # #номер → сообщение в чате специалистов
                msg_link = (f'<a href="https://t.me/c/{bare_work_chat_id}/{work_message_id}">#{req_id}</a>'
                            if work_message_id else f"#{req_id}")

                # Ссылка на пользователя
                user_name_escaped = html.escape(username or "н/д")
                user_link = (f'<a href="tg://user?id={user_id}">@{user_name_escaped}</a>'
                             if user_id else f"@{user_name_escaped}")

                # 💬 Название чата → исходное сообщение пользователя в клиентском чате
                try:
                    chat_info = await bot.get_chat(chat_id)
                    chat_title = html.escape(chat_info.title or f"Чат {chat_id}")
                    bare_client_chat_id = str(chat_id).removeprefix("-100")
                    chat_link = (f'<a href="https://t.me/c/{bare_client_chat_id}/{message_id}">{chat_title}</a>'
                                 if message_id and str(chat_id).startswith("-100") else chat_title)
                except Exception:
                    chat_link = f"Чат {chat_id}"

                lines.append(
                    f"• {msg_link} от {user_link}, ждёт {elapsed_str}\n"
                    f"  💬 {chat_link}\n"
                    f"  {desc_short}"
                )

            sent = await safe_send_message(bot, on_sent=db.log_work_message,
                chat_id=config.work_chat_id,
                text="\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            last_sent = now
            logger.info(f"Напоминание о брошенных заявках отправлено: {len(requests)} заявок.")

        except asyncio.CancelledError:
            logger.info("Фоновая задача напоминания о брошенных заявках остановлена.")
            break
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче брошенных заявок: {e}")


async def autoclean_loop(bot: Bot, db: Database, config: Config) -> None:
    """
    Автоочистка: если все заявки закрыты, удаляет сообщения бота в чатах
    в указанное время по указанным дням недели.
    """
    logger.info("Фоновая задача автоочистки запущена.")
    last_cleaned_date = None
    while True:
        try:
            await asyncio.sleep(30)
            s = (await db.get_autoclean_settings())
            if not s["enabled"]:
                continue

            now = datetime.now()
            if now.isoweekday() not in s["weekdays"]:
                continue

            clean_h, clean_m = map(int, s["clean_time"].split(":"))
            today = now.date()
            if today == last_cleaned_date:
                continue
            if not (now.hour == clean_h and now.minute == clean_m):
                continue

            if (await db.has_open_requests()):
                logger.info("Автоочистка: пропущено — есть незакрытые заявки.")
                continue

            last_cleaned_date = today
            deleted = 0
            already_gone = 0
            failed_msgs = []

            async def _delete(cid, mid):
                nonlocal deleted, already_gone
                try:
                    await bot.delete_message(cid, mid)
                    deleted += 1
                    (await db.mark_message_deleted(cid, mid, config.work_chat_id))
                except Exception as e:
                    err = str(e).lower()
                    if "not found" in err or "message to delete not found" in err:
                        already_gone += 1
                        (await db.mark_message_deleted(cid, mid, config.work_chat_id))
                    else:
                        failed_msgs.append((cid, mid, str(e)))

            # 1. Сообщения из клиентских чатов и заявки из рабочего чата
            for chat_id, bot_message_id, work_message_id in (await db.get_all_bot_messages()):
                if bot_message_id and chat_id:
                    await _delete(chat_id, bot_message_id)
                if work_message_id:
                    await _delete(config.work_chat_id, work_message_id)

            # 2. Дайджесты, напоминания и прочие сообщения бота в рабочем чате
            for message_id in (await db.get_work_log_messages()):
                await _delete(config.work_chat_id, message_id)
            # Failed deletions remain available for the next cleanup.

            logger.info(
                f"Автоочистка выполнена: удалено {deleted}, "
                f"уже отсутствовали {already_gone}, ошибок {len(failed_msgs)}."
            )
            if failed_msgs:
                from collections import Counter
                for reason, count in Counter(r for _, _, r in failed_msgs).most_common():
                    logger.warning(f"Автоочистка: {count} сообщений не удалено — {reason}")

        except asyncio.CancelledError:
            logger.info("Фоновая задача автоочистки остановлена.")
            break
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче автоочистки: {e}")
