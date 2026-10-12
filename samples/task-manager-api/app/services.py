"""Business rules for tasks. Every operation is scoped to the owning user."""
from app import repository


def list_tasks(user_id: int):
    return repository.find_by_owner(user_id)


def create_task(user_id: int, body):
    return repository.insert(user_id, body.title, body.due_date)


def update_task(user_id: int, task_id: int, body):
    task = repository.get(task_id)
    if task is None or task["owner_id"] != user_id:
        return None
    changes = body.model_dump(exclude_none=True)
    return repository.update(task_id, changes)


def delete_task(user_id: int, task_id: int) -> bool:
    task = repository.get(task_id)
    if task is None or task["owner_id"] != user_id:
        return False
    repository.delete(task_id)
    return True
