import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from config import Config
from db.database import Database
from db.async_database import AsyncDatabase
from handlers import broadcast, callbacks, callbacks_requests, callbacks_stats, common, commands, keyboards
from services.broadcasts import BroadcastService
from services.requests import RequestService
from services.scheduler import daily_reminder_loop, unassigned_reminder_loop, autoclean_loop
from utils.logging_config import logger, configure_logging
from utils.process_lock import bot_process_lock


def build_dispatcher(db, config, service, broadcast_service=None):
    common.database = db
    common.config = config
    callbacks_requests.database = db
    callbacks_stats.database = db
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp["work_chat_id"] = config.work_chat_id
    dp["request_service"] = service
    dp["broadcast_service"] = broadcast_service or BroadcastService(
        getattr(service, "bot", None), db, config)
    dp.message.outer_middleware(broadcast.remember_group_messages)
    # The broadcast router must see message input before the catch-all request handlers.
    dp.include_router(keyboards.router)
    dp.include_router(broadcast.router)
    dp.include_router(commands.router)
    dp.include_router(callbacks.router)
    return dp


def polling_updates(dp: Dispatcher) -> list:
    """Ask Telegram for every event the handlers use plus my_chat_member.

    The recipient registry uses membership events in addition to messages.
    """
    updates = list(dp.resolve_used_update_types())
    if 'my_chat_member' not in updates:
        updates.append('my_chat_member')
    return updates


async def main():
    config = Config()
    with bot_process_lock(config.database_path):
        await run_bot(config)


async def run_bot(config):
    configure_logging(config.log_level, config.log_path)
    db = AsyncDatabase(await asyncio.to_thread(Database, config.database_path))
    bot = Bot(token=config.bot_token)
    service = RequestService(bot, db, config)
    broadcast_service = BroadcastService(bot, db, config)
    dp = build_dispatcher(db, config, service, broadcast_service)
    tasks = []
    try:
        tasks = [asyncio.create_task(job) for job in (
            service.run(), broadcast_service.run(), daily_reminder_loop(bot, db, config),
            unassigned_reminder_loop(bot, db, config), autoclean_loop(bot, db, config))]
        logger.info("Бот запущен; сохранённые обращения, рассылки и доставки будут продолжены")
        await dp.start_polling(bot, allowed_updates=polling_updates(dp), close_bot_session=False)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dp.storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
