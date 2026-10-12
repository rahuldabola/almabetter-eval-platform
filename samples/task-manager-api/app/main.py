from fastapi import Depends, FastAPI, HTTPException

from app.auth import current_user, login
from app.schemas import TaskCreate, TaskUpdate
from app import services

app = FastAPI(title="Task Manager")


@app.post("/auth/login")
def auth_login(username: str, password: str):
    token = login(username, password)
    if not token:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"access_token": token}


@app.get("/tasks")
def list_tasks(user=Depends(current_user)):
    return services.list_tasks(user.id)


@app.post("/tasks", status_code=201)
def create_task(body: TaskCreate, user=Depends(current_user)):
    return services.create_task(user.id, body)


@app.patch("/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate, user=Depends(current_user)):
    task = services.update_task(user.id, task_id, body)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int, user=Depends(current_user)):
    if not services.delete_task(user.id, task_id):
        raise HTTPException(status_code=404, detail="task not found")
