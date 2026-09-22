"""
Агрегатор callback-роутеров. Подключает callbacks_requests и callbacks_stats.
"""
from aiogram import Router
from . import callbacks_requests, callbacks_stats
from db import database

router = Router()
router.include_router(callbacks_requests.router)
router.include_router(callbacks_stats.router)
