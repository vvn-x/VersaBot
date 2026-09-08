"""
كل التعامل مع قاعدة البيانات يمر من هذا الملف فقط.

- يستخدم PostgreSQL عبر psycopg2 مع Connection Pool موحّد
  (بديل SQLite + WAL mode، ويسمح بعمليات متزامنة بأمان
  عند وجود المهمة الدورية + تفاعل عدة أعضاء بنفس الوقت).
- كل الدوال sync عادية وتُستدعى عبر asyncio.to_thread من الأكواج
  (لم يتغير شيء من واجهة الاستخدام في بقية المشروع).
"""

import datetime
import contextlib

import psycopg2
import psycopg2.pool

import config

MAX_SHARED_MEMBERS = config.MAX_SHARED_MEMBERS

# Connection pool موحّد لكل التطبيق (min=1, max=10 قابلة للتعديل حسب الحاجة)
_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=config.DATABASE_URL,
)


@contextlib.contextmanager
def get_connection():
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subs (
                user_id BIGINT,
                guild_id BIGINT,
                role_id BIGINT,
                end_date TEXT,
                dm_message_id BIGINT,
                log_message_id BIGINT,
                start_date TEXT,
                PRIMARY KEY (user_id, guild_id, role_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shared_members (
                owner_id BIGINT,
                guild_id BIGINT,
                role_id BIGINT,
                member_id BIGINT,
                PRIMARY KEY (owner_id, guild_id, role_id, member_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                actor_id BIGINT,
                actor_name TEXT,
                action TEXT,
                target_id BIGINT,
                role_id BIGINT,
                details TEXT,
                created_at TEXT
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log (guild_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log (target_id)")


# ---------------------------------------------------------------------------
# اشتراكات (subs)
# ---------------------------------------------------------------------------

def save_sub(user_id, guild_id, role_id, end_date_str, dm_msg_id, log_msg_id, start_date_str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO subs
            (user_id, guild_id, role_id, end_date, dm_message_id, log_message_id, start_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, guild_id, role_id) DO UPDATE SET
                end_date = EXCLUDED.end_date,
                dm_message_id = EXCLUDED.dm_message_id,
                log_message_id = EXCLUDED.log_message_id,
                start_date = EXCLUDED.start_date
        ''', (user_id, guild_id, role_id, end_date_str, dm_msg_id, log_msg_id, start_date_str))


def get_and_delete_sub(user_id, guild_id, role_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT dm_message_id, log_message_id FROM subs WHERE user_id = %s AND guild_id = %s AND role_id = %s',
            (user_id, guild_id, role_id)
        )
        row = cursor.fetchone()
        cursor.execute(
            'DELETE FROM subs WHERE user_id = %s AND guild_id = %s AND role_id = %s',
            (user_id, guild_id, role_id)
        )
        return row


def get_sub(user_id, guild_id, role_id):
    """يرجع بيانات اشتراك واحد محدد (بدون حذفه) - تُستخدم لأوامر التجديد وزيادة الأيام."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT end_date, dm_message_id, log_message_id, start_date FROM subs '
            'WHERE user_id = %s AND guild_id = %s AND role_id = %s',
            (user_id, guild_id, role_id)
        )
        return cursor.fetchone()


def update_sub_end_date(user_id, guild_id, role_id, new_end_date_str):
    """يحدّث تاريخ انتهاء اشتراك موجود فقط (تجديد أو تصحيح مدة) بدون لمس بقية بياناته."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE subs SET end_date = %s WHERE user_id = %s AND guild_id = %s AND role_id = %s',
            (new_end_date_str, user_id, guild_id, role_id)
        )


def get_guild_subs(guild_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, role_id, end_date FROM subs WHERE guild_id = %s', (guild_id,))
        return cursor.fetchall()


def get_expired_subs(now_str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT user_id, guild_id, role_id, dm_message_id, log_message_id FROM subs WHERE end_date <= %s',
            (now_str,)
        )
        return cursor.fetchall()


def delete_subs_bulk(rows):
    if not rows:
        return
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            'DELETE FROM subs WHERE user_id = %s AND guild_id = %s AND role_id = %s', rows
        )


def get_owner_role_ids(guild_id, user_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT role_id FROM subs WHERE user_id = %s AND guild_id = %s', (user_id, guild_id))
        return [row[0] for row in cursor.fetchall()]


def get_owner_sub_info(guild_id, user_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT role_id, start_date, end_date FROM subs WHERE user_id = %s AND guild_id = %s',
            (user_id, guild_id)
        )
        return cursor.fetchall()


# ---------------------------------------------------------------------------
# الأعضاء المشاركون على الرتبة (shared_members)
# ---------------------------------------------------------------------------

def get_shared_members(owner_id, guild_id, role_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT member_id FROM shared_members WHERE owner_id = %s AND guild_id = %s AND role_id = %s',
            (owner_id, guild_id, role_id)
        )
        return [row[0] for row in cursor.fetchall()]


def check_shared_member_slot(owner_id, guild_id, role_id, target_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT COUNT(*) FROM shared_members WHERE owner_id = %s AND guild_id = %s AND role_id = %s',
            (owner_id, guild_id, role_id)
        )
        count = cursor.fetchone()[0]
        cursor.execute(
            'SELECT 1 FROM shared_members WHERE owner_id = %s AND guild_id = %s AND role_id = %s AND member_id = %s',
            (owner_id, guild_id, role_id, target_id)
        )
        already_added = cursor.fetchone() is not None
        return count, already_added


def add_shared_member(owner_id, guild_id, role_id, target_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO shared_members (owner_id, guild_id, role_id, member_id)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (owner_id, guild_id, role_id, member_id) DO NOTHING''',
            (owner_id, guild_id, role_id, target_id)
        )


def remove_shared_member(owner_id, guild_id, role_id, target_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT 1 FROM shared_members WHERE owner_id = %s AND guild_id = %s AND role_id = %s AND member_id = %s',
            (owner_id, guild_id, role_id, target_id)
        )
        exists = cursor.fetchone() is not None
        if exists:
            cursor.execute(
                'DELETE FROM shared_members WHERE owner_id = %s AND guild_id = %s AND role_id = %s AND member_id = %s',
                (owner_id, guild_id, role_id, target_id)
            )
        return exists


def delete_shared_members_for_role(owner_id, guild_id, role_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM shared_members WHERE owner_id = %s AND guild_id = %s AND role_id = %s',
            (owner_id, guild_id, role_id)
        )


# ---------------------------------------------------------------------------
# سجل التدقيق (audit_log)
# ---------------------------------------------------------------------------

def insert_audit_log(guild_id, actor_id, actor_name, action, target_id=None, role_id=None, details=None):
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO audit_log (guild_id, actor_id, actor_name, action, target_id, role_id, details, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (guild_id, actor_id, actor_name, action, target_id, role_id, details, now_str))


def get_audit_log(guild_id, member_id=None, limit=10, offset=0):
    with get_connection() as conn:
        cursor = conn.cursor()
        if member_id:
            cursor.execute('''
                SELECT actor_id, actor_name, action, target_id, role_id, details, created_at
                FROM audit_log
                WHERE guild_id = %s AND (actor_id = %s OR target_id = %s)
                ORDER BY id DESC LIMIT %s OFFSET %s
            ''', (guild_id, member_id, member_id, limit, offset))
        else:
            cursor.execute('''
                SELECT actor_id, actor_name, action, target_id, role_id, details, created_at
                FROM audit_log
                WHERE guild_id = %s
                ORDER BY id DESC LIMIT %s OFFSET %s
            ''', (guild_id, limit, offset))
        return cursor.fetchall()


def count_audit_log(guild_id, member_id=None):
    with get_connection() as conn:
        cursor = conn.cursor()
        if member_id:
            cursor.execute(
                'SELECT COUNT(*) FROM audit_log WHERE guild_id = %s AND (actor_id = %s OR target_id = %s)',
                (guild_id, member_id, member_id)
            )
        else:
            cursor.execute('SELECT COUNT(*) FROM audit_log WHERE guild_id = %s', (guild_id,))
        return cursor.fetchone()[0]
