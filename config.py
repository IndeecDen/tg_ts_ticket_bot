import os
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import set_key
from utils.logging_config import logger

BASE_DIR = Path(__file__).resolve().parent


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.environ.get('BOT_ENV_FILE', BASE_DIR / '.env'), env_file_encoding='utf-8', extra='ignore')
    bot_token: str = Field(min_length=1, repr=False)
    work_chat_id: int
    response_timeout: int = Field(default=300, gt=0)
    database_path: str = str(BASE_DIR / 'bot.db')
    log_level: str = 'INFO'
    log_path: str = str(BASE_DIR / 'bot.log')
    # Часовой пояс бота для расписаний (IANA, например Europe/Moscow).
    timezone: str = 'UTC'
    # Пауза между отправками рассылки в секундах: ограничение скорости Telegram.
    broadcast_send_interval: float = Field(default=0.05, ge=0.05, le=60)

    @field_validator('timezone')
    @classmethod
    def validate_timezone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('TIMEZONE должен быть доступным часовым поясом IANA; установите tzdata')
        return value

    @field_validator('database_path', 'log_path')
    @classmethod
    def resolve_path(cls, value):
        path = Path(value)
        return str(path if path.is_absolute() else BASE_DIR / path)

    def update_response_timeout(self, new_timeout: int) -> bool:
        if new_timeout <= 0:
            return False
        try:
            saved, _, _ = set_key(str(self.model_config['env_file']), 'RESPONSE_TIMEOUT', str(new_timeout))
            if not saved:
                return False
        except OSError:
            logger.exception('Не удалось сохранить RESPONSE_TIMEOUT')
            return False
        self.response_timeout = new_timeout
        return True
