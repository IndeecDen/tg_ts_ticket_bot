import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import Config
from db.database import Database
from db.async_database import AsyncDatabase
from handlers import common, commands, callbacks, callbacks_requests, callbacks_stats, keyboards
from services.requests import RequestService
from services.scheduler import daily_reminder_loop, unassigned_reminder_loop, autoclean_loop
from utils.logging_config import logger, configure_logging


def build_dispatcher(db, config, service):
    common.database = db
    common.config = config
    callbacks_requests.database = db
    callbacks_stats.database = db
    dp = Dispatcher(storage=MemoryStorage())
    dp["work_chat_id"] = config.work_chat_id
    dp["request_service"] = service
    dp.include_router(keyboards.router)
    dp.include_router(commands.router)
    dp.include_router(callbacks.router)
    return dp


async def main():
    config = Config()
    configure_logging(config.log_level, config.log_path)
    db = AsyncDatabase(await asyncio.to_thread(Database, config.database_path))
    bot = Bot(token=config.bot_token)
    service = RequestService(bot, db, config)
    dp = build_dispatcher(db, config, service)
    tasks = []
    try:
        tasks = [asyncio.create_task(job) for job in (
            service.run(), daily_reminder_loop(bot, db, config),
            unassigned_reminder_loop(bot, db, config), autoclean_loop(bot, db, config))]
        logger.info("Бот запущен; сохранённые обращения и доставки будут продолжены")
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dp.storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
