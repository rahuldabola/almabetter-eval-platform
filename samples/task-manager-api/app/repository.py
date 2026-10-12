import sqlite3

DB = "tasks.db"


def _conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def find_by_owner(owner_id: int):
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE owner_id = ?", (owner_id,))]


def get(task_id: int):
    with _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def insert(owner_id: int, title: str, due_date):
    with _conn() as c:
        cur = c.execute("INSERT INTO tasks (owner_id, title, status, due_date) VALUES (?, ?, 'todo', ?)",
                        (owner_id, title, due_date))
        return get(cur.lastrowid)


def update(task_id: int, changes: dict):
    cols = ", ".join(f"{k} = ?" for k in changes)
    with _conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*changes.values(), task_id))
    return get(task_id)


def delete(task_id: int):
    with _conn() as c:
        c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
