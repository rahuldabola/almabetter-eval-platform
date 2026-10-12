CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','doing','done')),
  due_date TEXT
);
CREATE INDEX ix_tasks_owner ON tasks(owner_id);
