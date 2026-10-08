import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path
from db.request_store import RequestStore
from db.broadcast_store import BroadcastStore
from datetime import datetime, timedelta
from typing import List, Tuple, Optional
from utils.logging_config import logger

SCHEMA_VERSION = 2


class Database(BroadcastStore, RequestStore):
    def __init__(self, db_path: str):
        self.db_path = db_path
        path = Path(db_path)
        if path.is_file():
            with self.connect() as source:
                if source.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                    backup_path = str(path) + datetime.now().strftime(".backup-%Y%m%d-%H%M%S-%f")
                    with closing(sqlite3.connect(backup_path)) as target:
                        source.backup(target)
                    logger.info("Создана резервная копия БД перед миграцией: %s", backup_path)
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    description TEXT,
                    status TEXT,
                    specialist TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    chat_id INTEGER,
                    message_id INTEGER,
                    bot_message_id INTEGER,
                    work_message_id INTEGER,
                    created_time TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS specialists (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS botadmins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ignored_words (
                    word TEXT PRIMARY KEY
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    expires_at TEXT,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    active_from TEXT,
                    active_to TEXT
                )
            """)
            # Миграция: добавляем поля active_from/active_to если их нет
            for col in ("active_from", "active_to"):
                columns = {r[1] for r in cursor.execute("PRAGMA table_info(announcements)")}
                if col not in columns:
                    cursor.execute(f"ALTER TABLE announcements ADD COLUMN {col} TEXT")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS active_timers (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    started_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reminder_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    interval_seconds INTEGER NOT NULL DEFAULT 3600
                )
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO reminder_settings (id, interval_seconds) VALUES (1, 3600)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS request_reminders (
                    request_id INTEGER PRIMARY KEY,
                    reminder_count INTEGER NOT NULL DEFAULT 0,
                    last_reminded_at TEXT
                )
            """)
            # Настройки ежедневного напоминания о незакрытых заявках (тип 1)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS work_chat_messages_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS autoclean_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    clean_time TEXT NOT NULL DEFAULT '23:00',
                    weekdays TEXT NOT NULL DEFAULT '1,2,3,4,5,6,7'
                )
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO autoclean_settings (id, enabled, clean_time, weekdays) "
                "VALUES (1, 0, '23:00', '1,2,3,4,5,6,7')"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_reminder_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    remind_time TEXT NOT NULL DEFAULT '09:00',
                    weekdays TEXT NOT NULL DEFAULT '1,2,3,4,5'
                )
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO daily_reminder_settings (id, enabled, remind_time, weekdays) VALUES (1, 0, '09:00', '1,2,3,4,5')"
            )
            # Настройки напоминания о брошенных заявках (тип 2)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS unassigned_reminder_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    work_start TEXT NOT NULL DEFAULT '08:00',
                    work_end TEXT NOT NULL DEFAULT '17:00',
                    weekdays TEXT NOT NULL DEFAULT '1,2,3,4,5',
                    threshold_minutes INTEGER NOT NULL DEFAULT 30
                )
            """)
            columns = {r[1] for r in cursor.execute("PRAGMA table_info(unassigned_reminder_settings)")}
            for column, declaration in (
                ("interval_minutes", "INTEGER NOT NULL DEFAULT 60"),
                ("work_start", "TEXT NOT NULL DEFAULT '08:00'"),
                ("work_end", "TEXT NOT NULL DEFAULT '17:00'"),
            ):
                if column not in columns:
                    cursor.execute(f"ALTER TABLE unassigned_reminder_settings ADD COLUMN {column} {declaration}")
            cursor.execute(
                "INSERT OR IGNORE INTO unassigned_reminder_settings "
                "(id, enabled, interval_minutes, work_start, work_end, weekdays, threshold_minutes) "
                "VALUES (1, 0, 60, '08:00', '17:00', '1,2,3,4,5', 30)"
            )
            columns = {r[1] for r in cursor.execute("PRAGMA table_info(requests)")}
            if "specialist_id" not in columns:
                cursor.execute("ALTER TABLE requests ADD COLUMN specialist_id INTEGER")
            self.init_request_store(conn)
            self.init_broadcast_store(conn)
            legacy_count = cursor.execute("SELECT COUNT(*) FROM active_timers").fetchone()[0]
            if legacy_count:
                logger.warning("В БД %s старых таймеров без текста. Они сохранены для ручной проверки; восстановить потерянный текст невозможно.", legacy_count)
            cursor.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            cursor.execute("PRAGMA journal_mode = WAL")

    def create_request(self, user_id: int, username: str, description: str, chat_id: int, message_id: int) -> int:
        with self.connect() as conn:
            cursor = conn.cursor()
            created_time = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO requests (user_id, username, description, status, chat_id, message_id, created_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, description, "new", chat_id, message_id, created_time)
            )
            conn.commit()
            return cursor.lastrowid

    def update_work_message_id(self, request_id: int, work_message_id: int) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE requests SET work_message_id = ? WHERE id = ?",
                (work_message_id, request_id)
            )
            conn.commit()

    def update_bot_message_id(self, request_id: int, bot_message_id: int) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE requests SET bot_message_id = ? WHERE id = ?",
                (bot_message_id, request_id)
            )
            conn.commit()

    def assign_specialist_if_new(self, request_id: int, specialist: str, specialist_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("""UPDATE requests SET status='in_progress', specialist=?,
                specialist_id=?, start_time=? WHERE id=? AND status='new'""",
                (specialist, specialist_id, datetime.now().isoformat(), request_id))
            if cur.rowcount:
                conn.execute("INSERT OR REPLACE INTO request_reminders VALUES (?, 0, NULL)", (request_id,))
                self.queue_delivery(conn, request_id)
            return bool(cur.rowcount)

    def close_request(self, request_id: int, specialist_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("""UPDATE requests SET status='closed', end_time=?
                WHERE id=? AND status='in_progress' AND specialist_id=?""",
                (datetime.now().isoformat(), request_id, specialist_id))
            if cur.rowcount:
                self.queue_delivery(conn, request_id)
            return bool(cur.rowcount)

    def cancel_request(self, user_id: int, chat_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id FROM requests WHERE user_id=? AND chat_id=? AND status='new'",
                                (user_id, chat_id)).fetchall()
            conn.execute("UPDATE requests SET status='canceled', end_time=? WHERE user_id=? AND chat_id=? AND status='new'",
                         (datetime.now().isoformat(), user_id, chat_id))
            for (request_id,) in rows:
                self.queue_delivery(conn, request_id)
            pending = conn.execute("DELETE FROM pending_requests WHERE user_id=? AND chat_id=?", (user_id, chat_id))
            return bool(rows or pending.rowcount)

    def cancel_request_by_id(self, request_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("UPDATE requests SET status='canceled', end_time=? WHERE id=? AND status='new'",
                               (datetime.now().isoformat(), request_id))
            if cur.rowcount:
                self.queue_delivery(conn, request_id)
            return bool(cur.rowcount)

    def get_request_by_id(self, request_id: int) -> Optional[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
            return cursor.fetchone()

    def add_specialist(self, user_id: int, username: str) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO specialists (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()

    def delete_specialist(self, user_id: int) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM specialists WHERE user_id = ?", (user_id,))
            conn.commit()

    def is_specialist(self, user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM specialists WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None

    def add_botadmin(self, user_id: int, username: str) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO botadmins (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()

    def delete_botadmin(self, user_id: int) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM botadmins WHERE user_id = ?", (user_id,))
            conn.commit()

    def is_botadmin(self, user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM botadmins WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None

    def get_all_botadmins(self) -> List[Tuple[int, str]]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username FROM botadmins ORDER BY username")
            return cursor.fetchall()

    def get_all_closed_requests(self) -> List[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT user_id, username, description, specialist, start_time, end_time
                FROM requests
                WHERE status = 'closed'
                ORDER BY end_time DESC
                """
            )
            return cursor.fetchall()

    def get_specialist_statistics(self, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None, specialist_id: Optional[int] = None) -> List[Tuple[str, int, int, float]]:
        with self.connect() as conn:
            cursor = conn.cursor()
            query = """
                SELECT specialist || CASE WHEN specialist_id IS NOT NULL THEN ' [ID ' || specialist_id || ']' ELSE ' [архив без ID]' END, chat_id, COUNT(*) as total_requests,
                       SUM(CASE WHEN start_time IS NOT NULL AND end_time IS NOT NULL
                               THEN (julianday(end_time) - julianday(start_time)) * 86400.0
                               ELSE 0 END) as total_time
                FROM requests
                WHERE status = 'closed' AND specialist IS NOT NULL
            """
            params = []
            if specialist_id is not None:
                query += " AND specialist_id = ?"
                params.append(specialist_id)
            if period == "custom" and start_date and end_date:
                query += " AND end_time >= ? AND end_time <= ?"
                params.extend([f"{start_date}T00:00:00", f"{end_date}T23:59:59.999"])
            elif period == "day":
                query += " AND end_time >= ?"
                params.append((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)).isoformat())
            elif period == "week":
                query += " AND end_time >= ?"
                params.append((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)).isoformat())
            elif period == "month":
                query += " AND end_time >= ?"
                params.append((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30)).isoformat())

            query += " GROUP BY COALESCE(specialist_id, specialist), chat_id ORDER BY COALESCE(specialist_id, specialist), chat_id"
            logger.debug(f"Executing query: {query} with params: {params}")
            cursor.execute(query, params)
            results = cursor.fetchall()
            logger.debug(f"Specialist statistics results: {results}")
            return results

    def get_client_chat_statistics(self, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[Tuple[int, str, int, float, float]]:
        with self.connect() as conn:
            cursor = conn.cursor()
            query = """
                SELECT chat_id, specialist || CASE WHEN specialist_id IS NOT NULL THEN ' [ID ' || specialist_id || ']' ELSE ' [архив без ID]' END, COUNT(*) as total_requests,
                       SUM(CASE WHEN start_time IS NOT NULL AND end_time IS NOT NULL
                               THEN (julianday(end_time) - julianday(start_time)) * 86400.0
                               ELSE 0 END) as total_execution_time,
                       SUM(CASE WHEN created_time IS NOT NULL AND start_time IS NOT NULL
                               THEN (julianday(start_time) - julianday(created_time)) * 86400.0
                               ELSE 0 END) as total_waiting_time
                FROM requests
                WHERE status = 'closed' AND chat_id IS NOT NULL AND specialist IS NOT NULL
            """
            params = []
            if period == "custom" and start_date and end_date:
                query += " AND end_time >= ? AND end_time <= ?"
                params.extend([f"{start_date}T00:00:00", f"{end_date}T23:59:59.999"])
            elif period == "day":
                query += " AND end_time >= ?"
                params.append((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)).isoformat())
            elif period == "week":
                query += " AND end_time >= ?"
                params.append((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)).isoformat())
            elif period == "month":
                query += " AND end_time >= ?"
                params.append((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30)).isoformat())

            query += " GROUP BY chat_id, COALESCE(specialist_id, specialist) ORDER BY chat_id, COALESCE(specialist_id, specialist)"
            logger.debug(f"Executing query: {query} with params: {params}")
            cursor.execute(query, params)
            results = cursor.fetchall()
            logger.debug(f"Client chat statistics results: {results}")
            return results

    def get_specialist_by_username(self, username: str) -> Optional[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username FROM specialists WHERE username = ?", (username,))
            return cursor.fetchone()

    def get_botadmin_by_username(self, username: str) -> Optional[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username FROM botadmins WHERE username = ?", (username,))
            return cursor.fetchone()

    def get_user_id_by_username(self, username: str) -> Optional[int]:
        """Ищет user_id по username в таблице requests (без @)."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM requests WHERE username = ? LIMIT 1",
                (username,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def get_all_specialists(self) -> List[Tuple[int, str]]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username FROM specialists ORDER BY username")
            return cursor.fetchall()

    def close_all_requests(self) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id FROM requests WHERE status IN ('new', 'in_progress')").fetchall()
            conn.execute("UPDATE requests SET status='closed', end_time=? WHERE status IN ('new', 'in_progress')",
                         (datetime.now().isoformat(),))
            for (request_id,) in rows:
                self.queue_delivery(conn, request_id)
            return len(rows)

    def get_open_requests(self) -> List[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, username, description, status, specialist
                FROM requests
                WHERE status IN ('new', 'in_progress')
                ORDER BY id ASC
                """
            )
            return cursor.fetchall()

    def get_unassigned_requests(self) -> List[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, username, description, status
                FROM requests
                WHERE status = 'new'
                ORDER BY id ASC
                """
            )
            return cursor.fetchall()

    def get_active_requests_by_specialist(self, specialist_id: int) -> List[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, username, description, status
                FROM requests
                WHERE status = 'in_progress' AND specialist_id = ?
                ORDER BY id ASC
                """,
                (specialist_id,)
            )
            return cursor.fetchall()

    def get_active_request_by_user_chat(self, user_id: int, chat_id: int) -> Optional[Tuple]:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM requests WHERE user_id = ? AND chat_id = ? AND status IN ('new', 'in_progress')",
                (user_id, chat_id)
            )
            return cursor.fetchone()

    def get_daily_reminder_settings(self) -> dict:
        """Возвращает настройки ежедневного напоминания о незакрытых заявках."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT enabled, remind_time, weekdays FROM daily_reminder_settings WHERE id = 1")
            row = cursor.fetchone()
            if not row:
                return {"enabled": False, "remind_time": "09:00", "weekdays": [1,2,3,4,5]}
            return {
                "enabled": bool(row[0]),
                "remind_time": row[1],
                "weekdays": [int(d) for d in row[2].split(",") if d.strip()]
            }

    def set_daily_reminder_settings(self, enabled: bool, remind_time: str, weekdays: list) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO daily_reminder_settings (id, enabled, remind_time, weekdays) VALUES (1, ?, ?, ?)",
                (1 if enabled else 0, remind_time, ",".join(str(d) for d in weekdays))
            )
            conn.commit()

    def get_unassigned_reminder_settings(self) -> dict:
        """Возвращает настройки напоминания о брошенных (new) заявках."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT enabled, interval_minutes, work_start, work_end, weekdays, threshold_minutes "
                "FROM unassigned_reminder_settings WHERE id = 1"
            )
            row = cursor.fetchone()
            if not row:
                return {"enabled": False, "interval_minutes": 60, "work_start": "08:00",
                        "work_end": "17:00", "weekdays": [1,2,3,4,5], "threshold_minutes": 30}
            return {
                "enabled": bool(row[0]),
                "interval_minutes": row[1] or 60,
                "work_start": row[2] or "08:00",
                "work_end": row[3] or "17:00",
                "weekdays": [int(d) for d in row[4].split(",") if d.strip()],
                "threshold_minutes": row[5] or 30,
            }

    def set_unassigned_reminder_settings(self, enabled: bool, interval_minutes: int,
                                          work_start: str, work_end: str,
                                          weekdays: list, threshold_minutes: int) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO unassigned_reminder_settings "
                "(id, enabled, interval_minutes, work_start, work_end, weekdays, threshold_minutes) "
                "VALUES (1, ?, ?, ?, ?, ?, ?)",
                (1 if enabled else 0, interval_minutes, work_start, work_end,
                 ",".join(str(d) for d in weekdays), threshold_minutes)
            )
            conn.commit()

    def get_inprogress_requests_for_daily(self) -> List[Tuple]:
        """Все заявки in_progress для ежедневного дайджеста.
        Возвращает: (id, specialist, specialist_id, start_time, description, user_id, username, chat_id, work_message_id)
        """
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, specialist, specialist_id, start_time, description,
                       user_id, username, chat_id, work_message_id, message_id
                FROM requests
                WHERE status = 'in_progress' AND specialist IS NOT NULL
                ORDER BY start_time ASC
                """
            )
            return cursor.fetchall()

    def get_unassigned_requests_overdue(self, threshold_minutes: int) -> List[Tuple]:
        """Заявки со статусом new которые висят дольше threshold_minutes."""
        with self.connect() as conn:
            cursor = conn.cursor()
            threshold = (datetime.now() - __import__('datetime').timedelta(minutes=threshold_minutes)).isoformat()
            cursor.execute(
                """
                SELECT id, username, description, created_time, user_id, chat_id, work_message_id, message_id
                FROM requests
                WHERE status = 'new' AND created_time <= ?
                ORDER BY created_time ASC
                """,
                (threshold,)
            )
            return cursor.fetchall()

    def get_reminder_settings(self) -> Tuple[int, int]:
        """Возвращает (interval_seconds, 0) — второй элемент оставлен для совместимости."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT interval_seconds FROM reminder_settings WHERE id = 1")
            row = cursor.fetchone()
            return (row[0], 0) if row else (3600, 0)

    def set_reminder_settings(self, interval_seconds: int, max_reminders: int = 0) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO reminder_settings (id, interval_seconds) VALUES (1, ?)",
                (interval_seconds,)
            )
            conn.commit()

    def get_overdue_requests(self, interval_seconds: int) -> List[Tuple]:
        """
        Возвращает заявки in_progress которым пора отправить напоминание.
        Структура: (id, specialist, specialist_id, work_message_id, reminder_count, start_time)
        """
        with self.connect() as conn:
            cursor = conn.cursor()
            threshold = (datetime.now() - __import__('datetime').timedelta(seconds=interval_seconds)).isoformat()
            cursor.execute(
                """
                SELECT r.id, r.specialist, r.specialist_id, r.work_message_id,
                       COALESCE(rr.reminder_count, 0), r.start_time
                FROM requests r
                LEFT JOIN request_reminders rr ON r.id = rr.request_id
                WHERE r.status = 'in_progress'
                  AND r.start_time IS NOT NULL
                  AND r.start_time <= ?
                  AND (rr.last_reminded_at IS NULL OR rr.last_reminded_at <= ?)
                """,
                (threshold, threshold)
            )
            return cursor.fetchall()

    def increment_reminder_count(self, request_id: int) -> int:
        """Увеличивает счётчик напоминаний и возвращает новое значение."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO request_reminders (request_id, reminder_count, last_reminded_at)
                VALUES (?, 1, ?)
                ON CONFLICT(request_id) DO UPDATE
                SET reminder_count = reminder_count + 1,
                    last_reminded_at = excluded.last_reminded_at
                """,
                (request_id, datetime.now().isoformat())
            )
            conn.commit()
            cursor.execute("SELECT reminder_count FROM request_reminders WHERE request_id = ?", (request_id,))
            row = cursor.fetchone()
            return row[0] if row else 1

    def save_timer(self, chat_id: int, user_id: int, username: str) -> None:
        """Сохраняет активный таймер в БД."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO active_timers (chat_id, user_id, username, started_at) VALUES (?, ?, ?, ?)",
                (chat_id, user_id, username, datetime.now().isoformat())
            )
            conn.commit()

    def delete_timer(self, chat_id: int, user_id: int) -> None:
        """Удаляет таймер из БД."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM active_timers WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id)
            )
            conn.commit()

    def delete_timers_by_chat(self, chat_id: int) -> None:
        """Удаляет все таймеры для чата (когда специалист ответил)."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM active_timers WHERE chat_id = ?", (chat_id,))
            conn.commit()

    def log_work_message(self, message_id: int) -> None:
        """Логирует сообщение бота в рабочем чате для последующего удаления."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO work_chat_messages_log (message_id, sent_at) VALUES (?, ?)",
                (message_id, datetime.now().isoformat())
            )
            conn.commit()

    def get_work_log_messages(self) -> List[int]:
        """Возвращает все залогированные message_id из рабочего чата."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT message_id FROM work_chat_messages_log")
            return [row[0] for row in cursor.fetchall()]

    def clear_work_log(self) -> None:
        """Очищает лог сообщений рабочего чата после удаления."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM work_chat_messages_log")
            conn.commit()

    def get_autoclean_settings(self) -> dict:
        """Возвращает настройки автоочистки сообщений бота."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT enabled, clean_time, weekdays FROM autoclean_settings WHERE id = 1")
            row = cursor.fetchone()
            if not row:
                return {"enabled": False, "clean_time": "23:00", "weekdays": [1,2,3,4,5,6,7]}
            return {
                "enabled": bool(row[0]),
                "clean_time": row[1],
                "weekdays": [int(d) for d in row[2].split(",") if d.strip()]
            }

    def set_autoclean_settings(self, enabled: bool, clean_time: str, weekdays: list) -> None:
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO autoclean_settings (id, enabled, clean_time, weekdays) VALUES (1, ?, ?, ?)",
                (1 if enabled else 0, clean_time, ",".join(str(d) for d in weekdays))
            )
            conn.commit()

    def has_open_requests(self) -> bool:
        """Возвращает True если есть заявки со статусом new или in_progress."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM requests WHERE status IN ('new', 'in_progress') UNION ALL SELECT 1 FROM pending_requests UNION ALL SELECT 1 FROM delivery_queue LIMIT 1")
            return cursor.fetchone() is not None

    def get_all_bot_messages(self) -> List[Tuple]:
        """Возвращает все (chat_id, bot_message_id, work_message_id) из всех заявок для удаления."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT chat_id, bot_message_id, work_message_id FROM requests "
                "WHERE status IN ('closed', 'canceled') AND (bot_message_id IS NOT NULL OR work_message_id IS NOT NULL) "
                "AND id NOT IN (SELECT request_id FROM delivery_queue)"
            )
            return cursor.fetchall()

    def get_all_timers(self) -> List[Tuple]:
        """Возвращает все сохранённые таймеры для восстановления при старте."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, user_id, username, started_at FROM active_timers")
            return cursor.fetchall()

    def add_announcement(self, text: str, expires_at: Optional[str], created_by: str,
                          active_from: Optional[str] = None, active_to: Optional[str] = None) -> int:
        """Добавляет новое объявление. Возвращает его ID."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO announcements (text, expires_at, created_by, created_at, active_from, active_to) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (text, expires_at, created_by, datetime.now().isoformat(), active_from, active_to)
            )
            conn.commit()
            return cursor.lastrowid

    @staticmethod
    def _in_time_range(active_from: str, active_to: str) -> bool:
        """Проверяет, попадает ли текущее время в диапазон. Поддерживает ночные диапазоны (17:00–08:00)."""
        now_str = datetime.now().strftime("%H:%M")
        if active_from <= active_to:
            return active_from <= now_str < active_to
        else:  # ночной диапазон: например 17:00–08:00
            return now_str >= active_from or now_str < active_to

    def get_active_announcements(self) -> List[Tuple]:
        """Возвращает активные объявления с учётом срока действия и временного диапазона.
        Возвращает: (id, text, expires_at, created_by, active_from, active_to)
        """
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, text, expires_at, created_by, active_from, active_to "
                "FROM announcements ORDER BY id ASC"
            )
            rows = cursor.fetchall()
        now = datetime.now()
        result = []
        for r in rows:
            ann_id, text, expires_at, created_by, active_from, active_to = r
            # Проверяем срок действия
            if expires_at and datetime.fromisoformat(expires_at) < now:
                continue
            # Проверяем временной диапазон
            if active_from and active_to and not self._in_time_range(active_from, active_to):
                continue
            result.append(r)
        return result

    def get_all_announcements(self) -> List[Tuple]:
        """Возвращает все объявления (для просмотра в настройках, без фильтрации по времени).
        Возвращает: (id, text, expires_at, created_by, active_from, active_to)
        """
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, text, expires_at, created_by, active_from, active_to "
                "FROM announcements ORDER BY id ASC"
            )
            return cursor.fetchall()

    def get_active_announcement(self) -> Optional[Tuple]:
        """Обратная совместимость — возвращает первое активное объявление."""
        active = self.get_active_announcements()
        return active[0] if active else None

    def delete_announcement_by_id(self, ann_id: int) -> bool:
        """Удаляет объявление по ID."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_all_announcements(self) -> int:
        """Удаляет все объявления. Возвращает количество удалённых."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM announcements")
            conn.commit()
            return cursor.rowcount

    def delete_announcement(self) -> bool:
        """Обратная совместимость — удаляет все объявления."""
        return self.delete_all_announcements() > 0

    def add_ignored_word(self, word: str) -> bool:
        """Добавляет слово в список игнорируемых."""
        with self.connect() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("INSERT INTO ignored_words (word) VALUES (?)", (word.lower(),))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def delete_ignored_word(self, word: str) -> bool:
        """Удаляет слово из списка игнорируемых."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ignored_words WHERE word = ?", (word.lower(),))
            conn.commit()
            return cursor.rowcount > 0

    def get_ignored_words(self) -> List[str]:
        """Возвращает список всех игнорируемых слов."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT word FROM ignored_words ORDER BY word")
            return [row[0] for row in cursor.fetchall()]

    def is_ignored_message(self, message: str) -> bool:
        """Проверяет, состоит ли сообщение только из игнорируемых слов."""
        if not message:
            return False
        # Удаляем пунктуацию и приводим к нижнему регистру
        import string
        message = message.translate(str.maketrans("", "", string.punctuation)).lower().strip()
        words = message.split()
        if not words:
            return False
        ignored_words = set(self.get_ignored_words())
        return all(word in ignored_words for word in words)
    def mark_message_deleted(self, chat_id, message_id, work_chat_id):
        with self.connect() as conn:
            conn.execute("UPDATE requests SET bot_message_id=NULL WHERE chat_id=? AND bot_message_id=?", (chat_id, message_id))
            if chat_id == work_chat_id:
                conn.execute("UPDATE requests SET work_message_id=NULL WHERE work_message_id=?", (message_id,))
                conn.execute("DELETE FROM work_chat_messages_log WHERE message_id=?", (message_id,))
