#!/usr/bin/env bash
# Fresh install for Debian 12 / Ubuntu 24.04 with systemd.
set -Eeuo pipefail
umask 077
REPO=https://github.com/IndeecDen/tg_ts_ticket_bot.git
BRANCH=main
APP=/opt/tg_ts_ticket_bot
DATA=/var/lib/tg_ts_ticket_bot
LOG=/var/log/tg_ts_ticket_bot
UNIT=/etc/systemd/system/tg_ts_ticket_bot.service
while (($#)); do
    case "$1" in
        --branch) [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { echo 'Missing branch'; exit 1; }; BRANCH=$2; shift 2 ;;
        --help|-h) echo 'sudo bash install.sh [--branch main]'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo 'Запустите через sudo bash install.sh'; exit 1; }
[[ -d /run/systemd/system ]] && command -v apt-get >/dev/null || {
    echo 'Нужны Debian/Ubuntu и работающий systemd.'; exit 1;
}
[[ -r /dev/tty ]] || { echo 'Нужен интерактивный терминал для ввода настроек.'; exit 1; }
for path in "$APP" "$DATA" "$LOG" "$UNIT"; do
    [[ ! -e "$path" && ! -L "$path" ]] || {
        echo "Установка остановлена: уже существует $path. См. обновление в README."; exit 1;
    }
done
if id ticketbot >/dev/null 2>&1 || getent group ticketbot >/dev/null; then
    echo 'Пользователь или группа ticketbot уже существует; проверьте предыдущую установку.'; exit 1
fi
if systemctl cat tg_ts_ticket_bot.service >/dev/null 2>&1; then
    echo 'Служба уже существует; установка остановлена.'; exit 1
fi
trap 'echo "Установка прервана. Созданные файлы сохранены для диагностики; настройки не перезаписываются." >&2' ERR
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates git python3 python3-venv
python3 -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,12), "Нужен Python 3.11 или 3.12 (Debian 12 / Ubuntu 24.04)"'
git clone --depth 1 --single-branch --branch "$BRANCH" -- "$REPO" "$APP"
# Never install runtime data accidentally committed to the repository.
if find "$APP" -path "$APP/.git" -prune -o -type f \( -name '.env' -o -name '*.db*' -o -name '*.sqlite*' -o -name '*.log*' -o -name '*.pem' -o -name '*.key' \) -print | grep -q .; then
    echo 'В скачанном проекте найдены рабочие данные или ключи. Установка остановлена.'; exit 1
fi
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install --disable-pip-version-check -r "$APP/requirements.txt"
useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin ticketbot
install -d -m 700 -o ticketbot -g ticketbot "$DATA" "$LOG"
python3 "$APP/deploy/configure.py" "$DATA/.env" </dev/tty
chown ticketbot:ticketbot "$DATA/.env"
# Source and dependencies remain root-owned; only data and logs are writable.
chmod -R u=rwX,go=rX "$APP"
install -m 644 "$APP/deploy/ticket-bot.service.example" "$UNIT"
systemctl daemon-reload
systemctl enable --now tg_ts_ticket_bot.service
sleep 5
if ! systemctl is-active --quiet tg_ts_ticket_bot.service; then
    echo 'Служба не запустилась. Проверьте: journalctl -u tg_ts_ticket_bot -n 50'; exit 1
fi
echo 'Установлено. Автозапуск включён. Проверьте /help в Telegram.'
echo 'Статус: sudo systemctl status tg_ts_ticket_bot'
echo 'Журнал: sudo journalctl -u tg_ts_ticket_bot -f'
