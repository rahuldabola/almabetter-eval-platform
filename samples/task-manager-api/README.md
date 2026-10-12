# Task Manager API

A small REST API for managing tasks, built with FastAPI and SQLite.

## Setup

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Endpoints

| Method | Path | Description |
|---|---|---|
| POST | /auth/login | Get a JWT |
| GET | /tasks | List the current user's tasks |
| POST | /tasks | Create a task |
| PATCH | /tasks/{id} | Update status/title |
| DELETE | /tasks/{id} | Delete a task |

## Design notes

Routes are thin; business rules live in `app/services.py`; persistence in `app/repository.py`.
Passwords are hashed with bcrypt. Tasks are scoped to the owner on every query.
