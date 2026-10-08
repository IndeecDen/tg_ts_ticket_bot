"""Durable recipient registry, drafts, schedules and delivery jobs for broadcasts.

Broadcast jobs live in their own tables, alongside the revision-based ticket
queue. Group migrations also update ticket delivery addresses while preserving
their original chat association.
"""
import json
from datetime import datetime, timedelta, timezone

RECIPIENT_CHAT_TYPES = ('group', 'supergroup')
MEMBER_STATUS_ACTIVE = ('creator', 'administrator', 'member', 'restricted')
BROADCAST_EDITABLE_STATUSES = ('draft', 'scheduled')
JOB_ACTIVE_STATUSES = ('pending', 'sending')
JOB_TERMINAL_ERRORS = ('failed', 'blocked', 'uncertain')


def utcnow():
    """Current UTC time as a sortable ISO string (all broadcasts use UTC)."""
    return datetime.now(timezone.utc).isoformat()


def _as_dict(cursor, row):
    if row is None:
        return None
    return dict(zip([column[0] for column in cursor.description], row))


def _loads(raw):
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _dumps(value):
    return json.dumps(value or [], ensure_ascii=False)


class BroadcastStore:
    def init_broadcast_store(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS broadcast_chats (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                can_send INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                source TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_broadcast_chats_active
                ON broadcast_chats(active, can_send, chat_type);
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                admin_name TEXT,
                admin_chat_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                text TEXT,
                entities TEXT,
                media_type TEXT,
                media_file_id TEXT,
                caption TEXT,
                caption_entities TEXT,
                timezone TEXT,
                scheduled_at TEXT,
                recipients INTEGER NOT NULL DEFAULT 0,
                preview_message_id INTEGER,
                reason TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_broadcasts_status ON broadcasts(status, scheduled_at);
            CREATE TABLE IF NOT EXISTS broadcast_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt TEXT NOT NULL DEFAULT '',
                last_error TEXT,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(broadcast_id, chat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_due
                ON broadcast_jobs(status, next_attempt);
            CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_broadcast
                ON broadcast_jobs(broadcast_id, status);
            CREATE TABLE IF NOT EXISTS broadcast_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                broadcast_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broadcast_chat_aliases (
                old_chat_id INTEGER PRIMARY KEY, new_chat_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_chat_origins (
                request_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broadcast_throttle (
                chat_id INTEGER PRIMARY KEY, until_at TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(broadcasts)')}
        if 'options' not in columns:
            conn.execute("ALTER TABLE broadcasts ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")
        if 'source_message_id' not in columns:
            conn.execute('ALTER TABLE broadcasts ADD COLUMN source_message_id INTEGER')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_source ON broadcasts(admin_chat_id, source_message_id)')
        # Known client chats from earlier requests are only candidates: their type is
        # unknown, so they stay out of every recipient list until the worker verifies them.
        for (chat_id,) in conn.execute(
                """SELECT chat_id FROM requests WHERE chat_id < 0
                   UNION SELECT chat_id FROM pending_requests WHERE chat_id < 0
                   UNION SELECT chat_id FROM active_timers WHERE chat_id < 0""").fetchall():
            self.add_broadcast_chat_candidate(conn, chat_id)

    # ── Recipient registry ────────────────────────────────────────────────

    def register_broadcast_chat(self, chat_id, chat_type, title=None, source='message'):
        """Remember a group the bot talks to. Never registers private chats or channels."""
        if chat_type not in RECIPIENT_CHAT_TYPES:
            return False
        now = utcnow()
        with self.connect() as conn:
            conn.execute("""INSERT INTO broadcast_chats
                    (chat_id, chat_type, title, status, can_send, active, source, first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, 'unknown', 1, 1, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_type=excluded.chat_type,
                    title=COALESCE(excluded.title, broadcast_chats.title),
                    can_send=CASE WHEN broadcast_chats.chat_type='unknown' THEN 1
                                 ELSE broadcast_chats.can_send END,
                    last_seen=excluded.last_seen,
                    updated_at=excluded.updated_at""",
                (chat_id, chat_type, title, source, now, now, now))
        return True

    def apply_broadcast_chat_member(self, chat_id, chat_type, status, can_send=True,
                                   title=None, source='my_chat_member'):
        """Store the bot's membership status reported by my_chat_member."""
        if chat_type not in RECIPIENT_CHAT_TYPES:
            return False
        if self.resolve_broadcast_chat(chat_id) != chat_id:
            return False
        now = utcnow()
        active = 1 if status in MEMBER_STATUS_ACTIVE else 0
        with self.connect() as conn:
            conn.execute("""INSERT INTO broadcast_chats
                    (chat_id, chat_type, title, status, can_send, active, source, first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_type=excluded.chat_type,
                    title=COALESCE(excluded.title, broadcast_chats.title),
                    status=excluded.status,
                    can_send=excluded.can_send,
                    active=excluded.active,
                    last_seen=excluded.last_seen,
                    updated_at=excluded.updated_at""",
                (chat_id, chat_type, title, status, 1 if can_send else 0, active, source, now, now, now))
        return True

    def set_broadcast_chat_can_send(self, chat_id, can_send):
        """Temporary access problems must not deactivate a chat forever."""
        with self.connect() as conn:
            conn.execute("UPDATE broadcast_chats SET can_send=?, updated_at=? WHERE chat_id=?",
                         (1 if can_send else 0, utcnow(), chat_id))

    @staticmethod
    def add_broadcast_chat_candidate(conn, chat_id, source='legacy_requests'):
        """Store a chat whose type is still unknown. It never becomes a recipient as is."""
        now = utcnow()
        conn.execute("""INSERT INTO broadcast_chats
                (chat_id, chat_type, status, can_send, active, source, first_seen, last_seen, updated_at)
            VALUES (?, 'unknown', 'unknown', 0, 1, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO NOTHING""", (chat_id, source, now, now, now))

    def get_broadcast_chat_candidates(self, limit=3):
        with self.connect() as conn:
            return [row[0] for row in conn.execute(
                """SELECT chat_id FROM broadcast_chats WHERE status='unknown' AND active=1
                    ORDER BY updated_at, chat_id LIMIT ?""", (limit,)).fetchall()]

    def broadcast_chat_needs_verification(self, chat_id):
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM broadcast_chats WHERE chat_id=?", (chat_id,)).fetchone()
            return row is not None and row[0] == 'unknown'

    def disable_broadcast_chat(self, chat_id, reason='not_a_recipient'):
        """Confirmed non-recipient (channel, private chat) or removed bot."""
        with self.connect() as conn:
            conn.execute("UPDATE broadcast_chats SET active=0, can_send=0, status=?, updated_at=? WHERE chat_id=?",
                         (reason, utcnow(), chat_id))

    def count_broadcast_chats(self, exclude_chat_ids=()):
        exclude_chat_ids = tuple(self.resolve_broadcast_chat(cid) for cid in exclude_chat_ids)
        where, params = self._recipient_filter(exclude_chat_ids)
        with self.connect() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM broadcast_chats WHERE {where}", params).fetchone()[0]

    def get_broadcast_chats(self, exclude_chat_ids=(), limit=None):
        exclude_chat_ids = tuple(self.resolve_broadcast_chat(cid) for cid in exclude_chat_ids)
        where, params = self._recipient_filter(exclude_chat_ids)
        sql = f"SELECT chat_id, chat_type, title, status FROM broadcast_chats WHERE {where} ORDER BY chat_id"
        if limit:
            sql += " LIMIT ?"
            params = params + (limit,)
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    @staticmethod
    def _recipient_filter(exclude_chat_ids=()):
        params = []
        sql = "active=1 AND can_send=1 AND chat_type IN ('group', 'supergroup')"
        ids = [int(chat_id) for chat_id in exclude_chat_ids or ()]
        if ids:
            sql += " AND chat_id NOT IN (" + ",".join("?" * len(ids)) + ")"
            params.extend(ids)
        return sql, tuple(params)

    def migrate_broadcast_chat(self, old_chat_id, new_chat_id, new_chat_type, title=None):
        """Follow a group→supergroup migration without duplicating recipients."""
        if int(old_chat_id) == int(new_chat_id):
            return False
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO broadcast_chat_aliases VALUES (?, ?)",
                         (old_chat_id, new_chat_id))
            conn.execute("UPDATE broadcast_chat_aliases SET new_chat_id=? WHERE new_chat_id=?",
                         (new_chat_id, old_chat_id))
            conn.execute("""INSERT OR IGNORE INTO broadcast_chats
                (chat_id, chat_type, title, status, can_send, active, source, first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, 'member', 1, 1, 'migration', ?, ?, ?)""",
                (new_chat_id, new_chat_type, title, now, now, now))
            conn.execute("""UPDATE broadcast_chats SET active=0, can_send=0, status='migrated', updated_at=?
                WHERE chat_id=?""", (now, old_chat_id))
            # Keep a permanent original-chat association before changing delivery addresses.
            conn.execute("""INSERT OR IGNORE INTO request_chat_origins(request_id, chat_id)
                SELECT id, chat_id FROM requests WHERE chat_id=?""", (old_chat_id,))
            conn.execute("UPDATE requests SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
            # Keep terminal outcomes, and suppress only unsent duplicate jobs.
            conn.execute("""UPDATE broadcast_jobs SET status='blocked', last_error='migration_duplicate', updated_at=?
                WHERE chat_id=? AND status='pending' AND broadcast_id IN
                (SELECT broadcast_id FROM broadcast_jobs WHERE chat_id=? AND status IN ('sent', 'uncertain', 'sending'))""",
                (now, new_chat_id, old_chat_id))
            conn.execute("""UPDATE broadcast_jobs SET status='blocked', last_error='migration_duplicate', updated_at=?
                WHERE chat_id=? AND status IN ('pending', 'sending') AND broadcast_id IN
                (SELECT broadcast_id FROM broadcast_jobs WHERE chat_id=?)""", (now, old_chat_id, new_chat_id))
            conn.execute("""UPDATE OR IGNORE broadcast_jobs SET chat_id=?, updated_at=?
                WHERE chat_id=? AND status IN ('pending', 'sending')""",
                         (new_chat_id, now, old_chat_id))
            # Pending ticket text must survive migration, even if both IDs were observed.
            for user_id, username, raw, due in conn.execute(
                    'SELECT user_id, username, messages, due_at FROM pending_requests WHERE chat_id=?',
                    (old_chat_id,)).fetchall():
                existing = conn.execute('SELECT messages, due_at FROM pending_requests WHERE chat_id=? AND user_id=?',
                                        (new_chat_id, user_id)).fetchone()
                messages = json.loads(raw) + (json.loads(existing[0]) if existing else [])
                conn.execute('INSERT OR REPLACE INTO pending_requests VALUES (?, ?, ?, ?, ?)',
                             (new_chat_id, user_id, username, json.dumps(messages, ensure_ascii=False),
                              min(due, existing[1]) if existing else due))
            conn.execute('DELETE FROM pending_requests WHERE chat_id=?', (old_chat_id,))
            conn.commit()
        return True

    def resolve_broadcast_chat(self, chat_id):
        with self.connect() as conn:
            seen = set()
            while chat_id not in seen:
                seen.add(chat_id)
                row = conn.execute('SELECT new_chat_id FROM broadcast_chat_aliases WHERE old_chat_id=?',
                                   (chat_id,)).fetchone()
                if not row:
                    break
                chat_id = row[0]
            return chat_id

    def request_original_chat(self, request_id):
        with self.connect() as conn:
            row = conn.execute('SELECT chat_id FROM request_chat_origins WHERE request_id=?', (request_id,)).fetchone()
            return row[0] if row else None

    # ── Drafts and schedules ──────────────────────────────────────────────

    def create_broadcast(self, admin_id, admin_name, admin_chat_id, text=None, entities=None,
                         media_type=None, media_file_id=None, caption=None, caption_entities=None,
                         timezone_name=None, options=None, source_message_id=None):
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute("""INSERT OR IGNORE INTO broadcasts
                    (admin_id, admin_name, admin_chat_id, status, text, entities, media_type,
                      media_file_id, caption, caption_entities, timezone, created_at, options, source_message_id)
                VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (admin_id, admin_name, admin_chat_id, text, _dumps(entities), media_type,
                 media_file_id, caption, _dumps(caption_entities), timezone_name, now, json.dumps(options or {}), source_message_id))
            if not cursor.rowcount:
                return conn.execute('SELECT id FROM broadcasts WHERE admin_chat_id=? AND source_message_id=?',
                                    (admin_chat_id, source_message_id)).fetchone()[0]
            return cursor.lastrowid

    def get_broadcast(self, broadcast_id):
        with self.connect() as conn:
            cursor = conn.execute("SELECT * FROM broadcasts WHERE id=?", (broadcast_id,))
            row = _as_dict(cursor, cursor.fetchone())
        if row:
            row['entities'] = _loads(row['entities'])
            row['caption_entities'] = _loads(row['caption_entities'])
            row['options'] = json.loads(row['options'])
        return row

    def set_broadcast_preview(self, broadcast_id, message_id):
        with self.connect() as conn:
            conn.execute("UPDATE broadcasts SET preview_message_id=? WHERE id=?", (message_id, broadcast_id))

    def schedule_broadcast(self, broadcast_id, scheduled_at, timezone_name):
        """Persist a one-shot schedule; the draft itself is kept in the database."""
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute("""UPDATE broadcasts SET status='scheduled', scheduled_at=?, timezone=?
                WHERE id=? AND status IN ('draft', 'scheduled')""",
                (scheduled_at, timezone_name, broadcast_id))
            return bool(cursor.rowcount)

    def due_broadcasts(self, now=None):
        with self.connect() as conn:
            return [row[0] for row in conn.execute(
                """SELECT id FROM broadcasts WHERE status='scheduled' AND scheduled_at IS NOT NULL
                    AND scheduled_at<=? ORDER BY id""", (now or utcnow(),)).fetchall()]

    def start_broadcast(self, broadcast_id, exclude_chat_ids=(), expected_schedule=None):
        """Atomically freeze the recipient list and queue one job per chat.

        Returns ``'running'``, ``'no_recipients'`` or ``None`` when the broadcast
        was already started or finished, which makes repeated updates harmless.
        """
        now = utcnow()
        exclude_chat_ids = tuple(self.resolve_broadcast_chat(cid) for cid in exclude_chat_ids)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, scheduled_at FROM broadcasts WHERE id=?", (broadcast_id,)).fetchone()
            if row is None or row[0] not in BROADCAST_EDITABLE_STATUSES:
                conn.commit()
                return None
            if expected_schedule is not None and (row[0] != 'scheduled' or row[1] != expected_schedule or row[1] > now):
                return None
            where, params = self._recipient_filter(exclude_chat_ids)
            chats = [item[0] for item in conn.execute(
                f"SELECT chat_id FROM broadcast_chats WHERE {where} ORDER BY chat_id", params).fetchall()]
            if not chats:
                conn.execute("""UPDATE broadcasts SET status='no_recipients', started_at=?, finished_at=?,
                    reason='no_known_groups' WHERE id=?""", (now, now, broadcast_id))
                conn.commit()
                return 'no_recipients'
            conn.executemany("""INSERT OR IGNORE INTO broadcast_jobs
                    (broadcast_id, chat_id, status, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)""",
                [(broadcast_id, chat_id, now, now) for chat_id in chats])
            total = conn.execute("SELECT COUNT(*) FROM broadcast_jobs WHERE broadcast_id=?",
                                 (broadcast_id,)).fetchone()[0]
            conn.execute("UPDATE broadcasts SET status='running', started_at=?, recipients=? WHERE id=?",
                         (now, total, broadcast_id))
            conn.commit()
            return 'running'

    def cancel_broadcast(self, broadcast_id, reason=None):
        """Cancel a draft or a scheduled broadcast. Safe to call twice."""
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM broadcasts WHERE id=?", (broadcast_id,)).fetchone()
            if row is None:
                conn.commit()
                return 'missing'
            if row[0] not in BROADCAST_EDITABLE_STATUSES:
                conn.commit()
                return row[0]
            conn.execute("""UPDATE broadcasts SET status='cancelled', finished_at=?, reason=COALESCE(?, reason)
                WHERE id=?""", (now, reason, broadcast_id))
            conn.execute("""UPDATE broadcast_jobs SET status='cancelled', updated_at=?
                WHERE broadcast_id=? AND status IN ('pending', 'sending')""", (now, broadcast_id))
            conn.commit()
            return 'cancelled'

    def list_broadcasts(self, limit=10, offset=0):
        with self.connect() as conn:
            cursor = conn.execute("""SELECT * FROM broadcasts ORDER BY id DESC LIMIT ? OFFSET ?""",
                                  (limit, offset))
            rows = [_as_dict(cursor, row) for row in cursor.fetchall()]
        for row in rows:
            row['entities'] = _loads(row['entities'])
            row['caption_entities'] = _loads(row['caption_entities'])
            row['options'] = json.loads(row['options'])
        return rows

    def count_broadcasts(self):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]

    def broadcast_counts(self, broadcast_id):
        counts = {'total': 0, 'sent': 0, 'pending': 0, 'sending': 0, 'failed': 0,
                  'blocked': 0, 'uncertain': 0, 'cancelled': 0}
        with self.connect() as conn:
            for status, total in conn.execute(
                    """SELECT status, COUNT(*) FROM broadcast_jobs WHERE broadcast_id=?
                        GROUP BY status""", (broadcast_id,)).fetchall():
                counts[status] = counts.get(status, 0) + total
                counts['total'] += total
        return counts

    # ── Delivery queue ───────────────────────────────────────────────────

    def claim_broadcast_job(self, now=None, interval=0.05):
        """Take one due job exclusively. Nothing is sent while the lock is held."""
        now = now or utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT j.id, j.chat_id, j.attempts, b.text, b.entities, b.media_type,
                    b.media_file_id, b.caption, b.caption_entities, b.options
                FROM broadcast_jobs j JOIN broadcasts b ON b.id = j.broadcast_id
                WHERE j.status='pending' AND j.next_attempt<=? AND b.status='running'
                    AND NOT EXISTS (SELECT 1 FROM broadcast_throttle t
                        WHERE t.chat_id IN (0, j.chat_id) AND t.until_at>?)
                ORDER BY j.id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                conn.commit()
                return None
            cursor = conn.execute("UPDATE broadcast_jobs SET status='sending', updated_at=? WHERE id=?",
                                  (now, row[0]))
            claimed = bool(cursor.rowcount)
            for chat_id, seconds in ((0, max(0.05, interval)), (row[1], 3.1)):
                until = (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat()
                conn.execute('INSERT OR REPLACE INTO broadcast_throttle VALUES (?, ?)', (chat_id, until))
            conn.commit()
        if not claimed:
            return None
        return {'job_id': row[0], 'chat_id': row[1], 'attempts': row[2], 'text': row[3],
                'entities': _loads(row[4]), 'media_type': row[5], 'media_file_id': row[6],
                'caption': row[7], 'caption_entities': _loads(row[8]), 'options': json.loads(row[9])}

    def pause_broadcast_delivery(self, seconds):
        until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        with self.connect() as conn:
            conn.execute("""INSERT INTO broadcast_throttle VALUES (0, ?)
                ON CONFLICT(chat_id) DO UPDATE SET until_at=MAX(until_at, excluded.until_at)""", (until,))

    def broadcast_delivery_paused(self):
        with self.connect() as conn:
            return bool(conn.execute('SELECT 1 FROM broadcast_throttle WHERE chat_id=0 AND until_at>?',
                                     (utcnow(),)).fetchone())

    def broadcast_errors(self, broadcast_id, limit=10, offset=0):
        with self.connect() as conn:
            return conn.execute("""SELECT chat_id, status, last_error FROM broadcast_jobs
                WHERE broadcast_id=? AND status IN ('failed', 'blocked', 'uncertain')
                ORDER BY id LIMIT ? OFFSET ?""", (broadcast_id, limit, offset)).fetchall()

    def set_broadcast_job_result(self, job_id, status, error=None, message_id=None, retry_in=None):
        now = utcnow()
        with self.connect() as conn:
            if status == 'pending':
                next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=max(1, retry_in or 5))).isoformat()
                conn.execute("""UPDATE broadcast_jobs SET status='pending', attempts=attempts+1,
                    next_attempt=?, last_error=?, updated_at=? WHERE id=? AND status='sending'""",
                    (next_attempt, (error or '')[:500], now, job_id))
            else:
                conn.execute("""UPDATE broadcast_jobs SET status=?, last_error=?, message_id=COALESCE(?, message_id),
                    next_attempt='', updated_at=? WHERE id=?""",
                     (status, (error or '')[:500] or None, message_id, now, job_id))
                row = conn.execute('SELECT chat_id FROM broadcast_jobs WHERE id=?', (job_id,)).fetchone()
                if row:
                    until = (datetime.now(timezone.utc) + timedelta(seconds=3.1)).isoformat()
                    conn.execute("""INSERT INTO broadcast_throttle VALUES (?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET until_at=MAX(until_at, excluded.until_at)""",
                        (row[0], until))

    def finalize_broadcasts(self):
        """Completion is derived from real job states, never from the queueing moment."""
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute("SELECT id FROM broadcasts WHERE status='running'").fetchall()
            for (broadcast_id,) in running:
                active = conn.execute(f"""SELECT COUNT(*) FROM broadcast_jobs WHERE broadcast_id=?
                    AND status IN ({','.join('?' * len(JOB_ACTIVE_STATUSES))})""",
                    (broadcast_id, *JOB_ACTIVE_STATUSES)).fetchone()[0]
                if active:
                    continue
                broken = conn.execute(f"""SELECT COUNT(*) FROM broadcast_jobs WHERE broadcast_id=?
                    AND status IN ({','.join('?' * len(JOB_TERMINAL_ERRORS))})""",
                    (broadcast_id, *JOB_TERMINAL_ERRORS)).fetchone()[0]
                conn.execute("UPDATE broadcasts SET status=?, finished_at=? WHERE id=?",
                             ('completed_with_errors' if broken else 'completed', now, broadcast_id))
            conn.commit()

    def restore_broadcasts_on_start(self):
        """Interrupted sends have an unknown outcome: keep them for manual review."""
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute("""UPDATE broadcast_jobs SET status='uncertain', last_error='interrupted',
                updated_at=? WHERE status='sending'""", (now,))
            return cursor.rowcount

    # ── Audit ────────────────────────────────────────────────────────────

    def log_broadcast_audit(self, admin_id, broadcast_id, action, details=None):
        with self.connect() as conn:
            conn.execute("""INSERT INTO broadcast_audit (admin_id, broadcast_id, action, details, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (admin_id, broadcast_id, action, details, utcnow()))

    def get_broadcast_audit(self, limit=20):
        with self.connect() as conn:
            return conn.execute("""SELECT admin_id, broadcast_id, action, details, created_at
                FROM broadcast_audit ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
