import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger('ticket_bot')


def configure_logging(level='INFO', path='bot.log'):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[RotatingFileHandler(path, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8'),
                  logging.StreamHandler()],
        force=True,
    )
