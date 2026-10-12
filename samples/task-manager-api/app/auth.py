import os
import time

import jwt
from fastapi import Header, HTTPException
from passlib.hash import bcrypt

SECRET = os.environ.get("JWT_SECRET", "change-me")
USERS = {"demo": {"id": 1, "password_hash": bcrypt.hash("demo123")}}


class User:
    def __init__(self, id: int):
        self.id = id


def login(username: str, password: str):
    u = USERS.get(username)
    if not u or not bcrypt.verify(password, u["password_hash"]):
        return None
    return jwt.encode({"sub": str(u["id"]), "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")


def current_user(authorization: str = Header(...)):
    try:
        claims = jwt.decode(authorization.removeprefix("Bearer "), SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    return User(int(claims["sub"]))
