"""Authentication + authorization.

Production: users sign in through the existing AlmaBetter identity provider (or
Amazon Cognito); the API only verifies short-lived RS256 JWTs against the IdP's
JWKS. The PoC uses HS256 with a local secret so it runs offline - the claims and
the authorization checks are the same.

Authorization is two-layered:
  1. role (RBAC):     what kind of action may this user do at all
  2. ownership/scope: is this row inside the user's tenancy (own submission, or a
                      course they teach/evaluate). Checked on every read and write.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Assignment, CourseMembership, Role, Submission, User

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-secret-change-me")
JWT_TTL_S = 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user_id: str, role: str, ttl: int = JWT_TTL_S) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"sub": user_id, "role": role, "exp": int(time.time()) + ttl}).encode())
    sig = _b64(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_token(token: str) -> dict:
    try:
        header, payload, sig = token.split(".")
        if json.loads(_unb64(header)).get("alg") != "HS256":  # never accept alg=none
            raise ValueError("bad alg")
        expected = _b64(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        claims = json.loads(_unb64(payload))
        if claims["exp"] < time.time():
            raise ValueError("expired")
        return claims
    except Exception as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from e


def current_user(authorization: str = Header(default=""), s: Session = Depends(get_session)) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    claims = verify_token(authorization.removeprefix("Bearer "))
    user = s.get(User, claims["sub"])
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return user


def require_role(*roles: Role):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles and user.role != Role.admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user

    return dep


def is_course_staff(s: Session, user: User, course_id: str) -> bool:
    if user.role == Role.admin:
        return True
    m = s.get(CourseMembership, (course_id, user.id))
    return bool(m and m.role in (Role.instructor, Role.evaluator))


def assert_can_read_submission(s: Session, user: User, sub: Submission) -> None:
    """Students see only their own submissions; staff only within their courses.
    Returns 404 (not 403) to avoid leaking which ids exist."""
    if sub.student_id == user.id:
        return
    course_id = s.scalar(select(Assignment.course_id).where(Assignment.id == sub.assignment_id))
    if not is_course_staff(s, user, course_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")


# ------------------------------------------------------------------ rate limiting


class SlidingWindowLimiter:
    """In-process limiter for the PoC. Production: the same algorithm in Redis
    (shared across API replicas) plus coarse limits at the ALB/WAF."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded",
                                headers={"Retry-After": str(int(self.window - (now - q[0])) + 1)})
        q.append(now)


write_limiter = SlidingWindowLimiter(limit=int(os.getenv("WRITE_RATE_LIMIT", "30")), window_s=60)
evaluation_limiter = SlidingWindowLimiter(limit=int(os.getenv("EVAL_RATE_LIMIT", "5")), window_s=3600)
