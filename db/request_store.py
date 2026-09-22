"""Durable collection and transactional delivery queue for requests."""
import json
from datetime import datetime, timedelta


class RequestStore:
    def init_request_store(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_requests (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                username TEXT NOT NULL, messages TEXT NOT NULL,
                due_at TEXT NOT NULL, PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS request_payloads (
                request_id INTEGER PRIMARY KEY, messages TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_queue (
                request_id INTEGER NOT NULL, destination TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt TEXT NOT NULL DEFAULT '', last_error TEXT,
                PRIMARY KEY(request_id, destination)
            );
            CREATE INDEX IF NOT EXISTS idx_requests_user_chat_status
                ON requests(user_id, chat_id, status);
            CREATE INDEX IF NOT EXISTS idx_requests_specialist_status
                ON requests(specialist_id, status);
            CREATE INDEX IF NOT EXISTS idx_requests_status_end ON requests(status, end_time);
            CREATE INDEX IF NOT EXISTS idx_requests_status_created ON requests(status, created_time);
            CREATE INDEX IF NOT EXISTS idx_pending_due ON pending_requests(due_at);
            CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_queue(next_attempt);
        """)
        # Recover active requests whose initial notification never arrived.
        for destination, column in (("work", "work_message_id"), ("client", "bot_message_id")):
            conn.execute(f"""INSERT OR IGNORE INTO delivery_queue(request_id, destination)
                SELECT id, ? FROM requests
                WHERE status IN ('new', 'in_progress') AND {column} IS NULL""", (destination,))

    @staticmethod
    def queue_delivery(conn, request_id):
        for destination in ("work", "client"):
            conn.execute("""INSERT INTO delivery_queue(request_id, destination) VALUES (?, ?)
                ON CONFLICT(request_id, destination) DO UPDATE SET
                revision = revision + 1, attempts = 0, next_attempt = '', last_error = NULL""",
                (request_id, destination))

    def collect_message(self, chat_id, user_id, username, message, timeout):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("""SELECT 1 FROM requests WHERE chat_id=? AND user_id=?
                    AND status IN ('new', 'in_progress')""", (chat_id, user_id)).fetchone():
                return False
            row = conn.execute("SELECT messages FROM pending_requests WHERE chat_id=? AND user_id=?",
                               (chat_id, user_id)).fetchone()
            messages = json.loads(row[0]) if row else []
            if not any(m['message_id'] == message['message_id'] for m in messages):
                messages.append(message)
            conn.execute("""INSERT INTO pending_requests VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET messages=excluded.messages,
                username=excluded.username""", (chat_id, user_id, username,
                json.dumps(messages, ensure_ascii=False),
                (datetime.now() + timedelta(seconds=timeout)).isoformat()))
            return True

    def cancel_pending(self, chat_id, user_id=None):
        with self.connect() as conn:
            if user_id is None:
                result = conn.execute("DELETE FROM pending_requests WHERE chat_id=?", (chat_id,))
            else:
                result = conn.execute("DELETE FROM pending_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            return result.rowcount

    def promote_due_requests(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT chat_id, user_id, username, messages FROM pending_requests WHERE due_at<=?",
                                (datetime.now().isoformat(),)).fetchall()
            ids = []
            for chat_id, user_id, username, payload in rows:
                messages = json.loads(payload)
                existing = conn.execute("""SELECT id FROM requests WHERE chat_id=? AND user_id=?
                    AND status IN ('new', 'in_progress')""", (chat_id, user_id)).fetchone()
                if not existing and messages:
                    description = '\n'.join(m['text'] for m in messages if m.get('text')) or 'Сообщение без текста'
                    cur = conn.execute("""INSERT INTO requests
                        (user_id, username, description, status, chat_id, message_id, created_time)
                        VALUES (?, ?, ?, 'new', ?, ?, ?)""",
                        (user_id, username, description, chat_id, messages[0]['message_id'], datetime.now().isoformat()))
                    request_id = cur.lastrowid
                    conn.execute("INSERT INTO request_payloads VALUES (?, ?)", (request_id, payload))
                    self.queue_delivery(conn, request_id)
                    ids.append(request_id)
                conn.execute("DELETE FROM pending_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            return ids

    def due_deliveries(self):
        with self.connect() as conn:
            return conn.execute("""SELECT request_id, destination, revision, attempts
                FROM delivery_queue WHERE next_attempt<=?
                ORDER BY request_id, CASE destination WHEN 'work' THEN 0 ELSE 1 END LIMIT 100""",
                (datetime.now().isoformat(),)).fetchall()

    def delivery_payload(self, request_id):
        with self.connect() as conn:
            row = conn.execute("SELECT messages FROM request_payloads WHERE request_id=?", (request_id,)).fetchone()
            return json.loads(row[0]) if row else []

    def delivery_succeeded(self, request_id, destination, revision, message_id):
        column = {'work': 'work_message_id', 'client': 'bot_message_id'}[destination]
        with self.connect() as conn:
            # Save the ID even when a concurrent status change scheduled another revision.
            conn.execute(f"UPDATE requests SET {column}=? WHERE id=?", (message_id, request_id))
            conn.execute("DELETE FROM delivery_queue WHERE request_id=? AND destination=? AND revision=?",
                         (request_id, destination, revision))

    def delivery_failed(self, request_id, destination, revision, attempts, error, delay=None):
        delay = delay if delay is not None else min(3600, 5 * 2 ** min(attempts, 10))
        with self.connect() as conn:
            conn.execute("""UPDATE delivery_queue SET attempts=attempts+1, next_attempt=?, last_error=?
                WHERE request_id=? AND destination=? AND revision=?""",
                ((datetime.now() + timedelta(seconds=delay)).isoformat(), error[:500], request_id, destination, revision))

    def bind_specialist(self, request_id, specialist_id, specialist_name):
        with self.connect() as conn:
            cur = conn.execute("""UPDATE requests SET specialist_id=?, specialist=?
                WHERE id=? AND status='in_progress'""", (specialist_id, specialist_name, request_id))
            if cur.rowcount:
                self.queue_delivery(conn, request_id)
            return bool(cur.rowcount)
