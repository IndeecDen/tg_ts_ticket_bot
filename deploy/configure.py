"""Interactive configuration; never print credentials or pass them in argv."""
import getpass
import os
from pathlib import Path
import re
import sys


def ask(prompt, pattern, default=None, secret=False):
    while True:
        label = f'{prompt}' + (f' [{default}]' if default else '') + ': '
        value = (getpass.getpass(label) if secret else input(label)).strip()
        value = value or default or ''
        if re.fullmatch(pattern, value):
            return value
        print('Некорректный формат. Повторите ввод.')


def main():
    target = Path(sys.argv[1])
    if target.exists():
        raise SystemExit('Файл настроек уже существует; перезапись запрещена.')
    token = ask('BOT_TOKEN из BotFather (ввод скрыт)', r'[0-9]+:[A-Za-z0-9_-]{30,}', secret=True)
    chat = ask('WORK_CHAT_ID рабочего группового чата', r'-[1-9][0-9]*')
    timeout = ask('Ожидание ответа, секунд', r'[1-9][0-9]{0,7}', '300')
    content = (f'BOT_TOKEN={token}\nWORK_CHAT_ID={chat}\nRESPONSE_TIMEOUT={timeout}\n'
               'DATABASE_PATH=/var/lib/tg_ts_ticket_bot/bot.db\n'
               'LOG_PATH=/var/log/tg_ts_ticket_bot/bot.log\nLOG_LEVEL=INFO\n')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(content)
    print('Настройки сохранены. Действительность токена проверяется при запуске бота.')


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('\nНастройка отменена.')
