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

        # Миграция: добавляем колонки для трекинга активности агентов, если
        # их ещё нет (для баз, созданных до этой фичи).
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
        if "last_active_at" not in existing:
            conn.execute("ALTER TABLE candidates ADD COLUMN last_active_at TEXT")
        if "warned_inactive_at" not in existing:
            conn.execute("ALTER TABLE candidates ADD COLUMN warned_inactive_at TEXT")


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
