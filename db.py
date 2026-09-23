import datetime
import sqlite3
import json
from contextlib import contextmanager

DB_PATH = "candidates.db"


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                current_step TEXT DEFAULT 'new',
                quiz_question_index INTEGER DEFAULT -1,
                quiz_correct_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new',
                quiz_results TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_user_id INTEGER NOT NULL,
                owner_name TEXT,
                owner_phone TEXT,
                address TEXT,
                price TEXT,
                deposit TEXT,
                showing_time TEXT,
                tenant_criteria TEXT,
                notes TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Реестр начислений агентам. Одна строка = одно событие ("объект
        # принят в работу" / "объект сдан"). paid_at NULL — ещё не
        # выплачено, попадает в "ближайшую выплату" агента. Отдельные
        # строки на "принята"/"сдана" — так объект, принятый на одной
        # неделе и сданный на следующей, корректно приносит деньги дважды
        # в разные выплаты, без задвоения и без сложной логики на objects.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_user_id INTEGER NOT NULL,
                object_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                amount INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT
            )
        """)

        # Миграция: добавляем колонки для трекинга активности агентов, если
        # их ещё нет (для баз, созданных до этой фичи).
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
        if "last_active_at" not in existing:
            conn.execute("ALTER TABLE candidates ADD COLUMN last_active_at TEXT")
        if "warned_inactive_at" not in existing:
            conn.execute("ALTER TABLE candidates ADD COLUMN warned_inactive_at TEXT")
        # id закреплённого в личке сообщения с кнопкой онлайн-офиса. Храним,
        # чтобы повторный /start не плодил дубли, а просто перезакреплял то же
        # сообщение, если агент его открепил.
        if "office_pin_message_id" not in existing:
            conn.execute("ALTER TABLE candidates ADD COLUMN office_pin_message_id INTEGER")


def get_candidate(user_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM candidates WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_candidate(user_id, username, full_name):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO candidates (user_id, username, full_name) VALUES (?, ?, ?)",
            (user_id, username or "", full_name or ""),
        )


def update_candidate(user_id, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = CURRENT_TIMESTAMP"
    values = list(fields.values()) + [user_id]
    with _connect() as conn:
        conn.execute(f"UPDATE candidates SET {set_clause} WHERE user_id = ?", values)


def list_candidates(status=None):
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE status = ? ORDER BY updated_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM candidates ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]


def save_quiz_result(user_id, step_id, correct, total):
    cand = get_candidate(user_id)
    results = json.loads(cand["quiz_results"]) if cand and cand["quiz_results"] else {}
    attempts = results.get(step_id, {}).get("attempts", 0) + 1
    results[step_id] = {"correct": correct, "total": total, "attempts": attempts}
    update_candidate(user_id, quiz_results=json.dumps(results, ensure_ascii=False))


def create_object(agent_user_id, **fields):
    columns = ["agent_user_id"] + list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    values = [agent_user_id] + list(fields.values())
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO objects ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        return cur.lastrowid


def get_object(object_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM objects WHERE id = ?", (object_id,)).fetchone()
        return dict(row) if row else None


def update_object_status(object_id, status):
    with _connect() as conn:
        conn.execute(
            "UPDATE objects SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, object_id),
        )


def count_objects_by_status(agent_user_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM objects WHERE agent_user_id = ? GROUP BY status",
            (agent_user_id,),
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}


def touch_active(user_id):
    """Отмечает агента как активного прямо сейчас и снимает предупреждение об инактиве."""
    with _connect() as conn:
        conn.execute(
            "UPDATE candidates SET last_active_at = CURRENT_TIMESTAMP, warned_inactive_at = NULL "
            "WHERE user_id = ?",
            (user_id,),
        )


def list_candidates_to_warn(inactive_after_days):
    """Прошедшие обучение, неактивные дольше порога и ещё не предупреждённые."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM candidates
            WHERE status = 'passed'
              AND warned_inactive_at IS NULL
              AND julianday('now') - julianday(COALESCE(last_active_at, updated_at)) >= ?
            """,
            (inactive_after_days,),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_warned(user_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE candidates SET warned_inactive_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,),
        )


def list_candidates_to_remove(grace_period_days):
    """Предупреждённые, у кого прошёл льготный период без ответа админа."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM candidates
            WHERE status = 'passed'
              AND warned_inactive_at IS NOT NULL
              AND julianday('now') - julianday(warned_inactive_at) >= ?
            """,
            (grace_period_days,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_inactive_candidates(min_days):
    """Прошедшие обучение, неактивные от min_days дней — для команды /kick.
    У каждой строки есть поле days_inactive, отсортировано по убыванию."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *,
                   CAST(julianday('now') - julianday(COALESCE(last_active_at, updated_at)) AS INTEGER)
                     AS days_inactive
            FROM candidates
            WHERE status = 'passed'
              AND julianday('now') - julianday(COALESCE(last_active_at, updated_at)) >= ?
            ORDER BY days_inactive DESC
            """,
            (min_days,),
        ).fetchall()
        return [dict(row) for row in rows]


def soft_remove_candidate(user_id):
    """Не удаляет запись целиком — помечает статусом removed, чтобы /unkick
    мог найти и восстановить агента (имя, история тестов и т.д. не теряются)."""
    with _connect() as conn:
        conn.execute("UPDATE candidates SET status = 'removed' WHERE user_id = ?", (user_id,))


def add_payment(agent_user_id, object_id, kind, amount):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO payments (agent_user_id, object_id, kind, amount) VALUES (?, ?, ?, ?)",
            (agent_user_id, object_id, kind, amount),
        )


def get_unpaid_total(agent_user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payments "
            "WHERE agent_user_id = ? AND paid_at IS NULL",
            (agent_user_id,),
        ).fetchone()
        return row["total"]


def list_agents_with_in_progress():
    """Агенты, у которых есть объекты в статусе in_progress — список для /kv.
    У каждой строки есть in_progress_count и unpaid_total."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT c.user_id, c.username, c.full_name,
                   (SELECT COUNT(*) FROM objects o
                     WHERE o.agent_user_id = c.user_id AND o.status = 'in_progress') AS in_progress_count,
                   (SELECT COALESCE(SUM(amount), 0) FROM payments p
                     WHERE p.agent_user_id = c.user_id AND p.paid_at IS NULL) AS unpaid_total
            FROM candidates c
            WHERE EXISTS (
                SELECT 1 FROM objects o
                WHERE o.agent_user_id = c.user_id AND o.status = 'in_progress'
            )
            ORDER BY c.full_name
        """).fetchall()
        return [dict(row) for row in rows]


def list_objects_for_agent(agent_user_id, status):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM objects WHERE agent_user_id = ? AND status = ? ORDER BY created_at",
            (agent_user_id, status),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_agent_paid(agent_user_id):
    """Помечает все неоплаченные начисления агента выплаченными. Возвращает
    выплаченную сумму (0, если платить было нечего)."""
    total = get_unpaid_total(agent_user_id)
    if total:
        with _connect() as conn:
            conn.execute(
                "UPDATE payments SET paid_at = CURRENT_TIMESTAMP "
                "WHERE agent_user_id = ? AND paid_at IS NULL",
                (agent_user_id,),
            )
    return total


def get_payment_totals(agent_user_id):
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(amount), 0) AS lifetime_earned,
                COALESCE(SUM(CASE WHEN paid_at IS NOT NULL THEN amount ELSE 0 END), 0) AS lifetime_paid,
                COALESCE(SUM(CASE WHEN julianday('now') - julianday(created_at) <= 30
                                  THEN amount ELSE 0 END), 0) AS last_30_days
            FROM payments
            WHERE agent_user_id = ?
            """,
            (agent_user_id,),
        ).fetchone()
        return dict(row)


def get_weekly_earnings(agent_user_id, weeks=8):
    """Начисления по неделям (пн-вс) за последние `weeks` недель, старые
    сначала — удобно сразу отдавать в график."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT amount, created_at FROM payments WHERE agent_user_id = ?",
            (agent_user_id,),
        ).fetchall()

    buckets = {}
    for row in rows:
        dt = datetime.datetime.fromisoformat(row["created_at"])
        monday = dt.date() - datetime.timedelta(days=dt.weekday())
        buckets[monday] = buckets.get(monday, 0) + row["amount"]

    today = datetime.date.today()
    this_monday = today - datetime.timedelta(days=today.weekday())
    result = []
    for i in range(weeks - 1, -1, -1):
        monday = this_monday - datetime.timedelta(weeks=i)
        result.append({"label": monday.strftime("%d.%m"), "amount": buckets.get(monday, 0)})
    return result


def get_payout_history(agent_user_id, limit=10):
    """Прошлые выплаты, сгруппированные по моменту нажатия "Выплачено"
    (в один вызов mark_agent_paid все строки получают один и тот же
    paid_at — CURRENT_TIMESTAMP один раз на весь UPDATE)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT paid_at, SUM(amount) AS total
            FROM payments
            WHERE agent_user_id = ? AND paid_at IS NOT NULL
            GROUP BY paid_at
            ORDER BY paid_at DESC
            LIMIT ?
            """,
            (agent_user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def list_agents_with_unpaid():
    """Все агенты с неоплаченными начислениями — сводка для /money."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT c.user_id, c.username, c.full_name,
                   SUM(p.amount) AS unpaid_total
            FROM candidates c
            JOIN payments p ON p.agent_user_id = c.user_id AND p.paid_at IS NULL
            GROUP BY c.user_id
            ORDER BY c.full_name
        """).fetchall()
        return [dict(row) for row in rows]


def list_unpaid_payments_for_agent(agent_user_id):
    """Неоплаченные начисления агента вместе с адресом объекта — для /money."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT p.*, o.address
            FROM payments p
            JOIN objects o ON o.id = p.object_id
            WHERE p.agent_user_id = ? AND p.paid_at IS NULL
            ORDER BY p.created_at
        """, (agent_user_id,)).fetchall()
        return [dict(row) for row in rows]


def list_removed_candidates():
    """Удалённые (soft-delete) агенты для раздела «Баны» в панели куратора.

    Тянем заодно число переданных объектов — куратор по нему понимает, кого
    именно восстанавливает, не переключаясь на другой раздел.
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT c.*,
                   (SELECT COUNT(*) FROM objects o WHERE o.agent_user_id = c.user_id)
                       AS objects_count
            FROM candidates c
            WHERE c.status = 'removed'
            ORDER BY c.updated_at DESC
        """).fetchall()
        return [dict(row) for row in rows]


def list_all_objects(status=None, limit=300):
    """Все квартиры всех агентов для панели куратора.

    LEFT JOIN, а не INNER: объект не должен пропадать из списка, если строку
    агента почему-то не найти — адрес и телефон собственника куратору нужны
    в любом случае.
    """
    sql = """
        SELECT o.*, c.full_name AS agent_name, c.username AS agent_username
        FROM objects o
        LEFT JOIN candidates c ON c.user_id = o.agent_user_id
    """
    params = []
    if status:
        sql += " WHERE o.status = ?"
        params.append(status)
    sql += " ORDER BY o.id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def count_all_objects_by_status():
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM objects GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}
