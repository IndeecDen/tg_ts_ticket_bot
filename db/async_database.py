"""Run each short SQLite operation outside the Telegram event loop."""
import asyncio
from functools import partial


class AsyncDatabase:
    def __init__(self, database):
        self.database = database

    def __getattr__(self, name):
        method = getattr(self.database, name)
        async def call(*args, **kwargs):
            return await asyncio.to_thread(partial(method, *args, **kwargs))
        return call
