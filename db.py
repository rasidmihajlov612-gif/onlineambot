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


def save_quiz_result(user_id, step_id, correct, total):
    cand = get_candidate(user_id)
    results = json.loads(cand["quiz_results"]) if cand and cand["quiz_results"] else {}
    attempts = results.get(step_id, {}).get("attempts", 0) + 1
    results[step_id] = {"correct": correct, "total": total, "attempts": attempts}
    update_candidate(user_id, quiz_results=json.dumps(results, ensure_ascii=False))
