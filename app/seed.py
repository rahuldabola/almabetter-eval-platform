"""Demo data: one course, staff + student users, an assignment and the rubric from
the assignment brief (Functionality 25 / Code Quality 20 / Architecture 20 /
Problem Solving 15 / Documentation 10 / Innovation-AI Usage 10)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import issue_token
from app.models import Assignment, Course, CourseMembership, Role, Rubric, RubricCriterion, User


def _bands(top: str, mid: str, low: str) -> list[dict]:
    return [
        {"min": 8, "max": 10, "descriptor": top},
        {"min": 5, "max": 7.9, "descriptor": mid},
        {"min": 0, "max": 4.9, "descriptor": low},
    ]


DEFAULT_RUBRIC = [
    ("functionality", "Functionality", 0.25, ["code", "tests", "docs"],
     "Does the project implement the required features end to end, and do they work?",
     _bands("All required features implemented and wired end to end; edge cases handled",
            "Core features present but some missing, stubbed or broken",
            "Most required features missing or non-functional")),
    ("code_quality", "Code Quality", 0.20, ["code", "tests"],
     "Readability, naming, modularity, error handling, tests, absence of duplication and dead code.",
     _bands("Clean, idiomatic, well-structured code with error handling and meaningful tests",
            "Readable but with duplication, weak error handling or few tests",
            "Hard to follow, no error handling, no tests")),
    ("architecture", "Architecture", 0.20, ["architecture"],
     "Separation of concerns, layering, data model, API design, configuration and deployment setup.",
     _bands("Clear layers/modules, sound data model and API design, config externalised",
            "Some structure but mixed responsibilities or questionable design choices",
            "Everything in one place, no discernible design")),
    ("problem_solving", "Problem Solving", 0.15, ["code", "docs"],
     "Quality of the approach to the core problem: algorithms, trade-offs, handling of non-trivial cases.",
     _bands("Thoughtful approach with justified trade-offs and non-trivial cases handled",
            "Works for the happy path; limited reasoning about alternatives",
            "Naive or incorrect approach")),
    ("documentation", "Documentation", 0.10, ["docs"],
     "README with setup/run instructions, explanation of design decisions, API docs where relevant.",
     _bands("Complete README (setup, usage, design notes), API documented",
            "README exists but is incomplete",
            "No meaningful documentation")),
    ("innovation_ai_usage", "Innovation / AI Usage", 0.10, ["code", "docs"],
     "Creative features, or meaningful and well-engineered use of AI/LLMs where relevant.",
     _bands("Original features or well-engineered AI integration (prompting, evaluation, guardrails)",
            "Some extras or basic AI usage",
            "No innovation beyond the brief")),
]


def seed(s: Session) -> dict:
    course = Course(code="FSWD-2026", title="Full Stack Web Development")
    admin = User(email="admin@almabetter.example", name="Admin", role=Role.admin)
    instructor = User(email="instructor@almabetter.example", name="Instructor", role=Role.instructor)
    evaluator = User(email="evaluator@almabetter.example", name="Evaluator", role=Role.evaluator)
    student = User(email="student@almabetter.example", name="Student One", role=Role.student)
    other = User(email="student2@almabetter.example", name="Student Two", role=Role.student)
    s.add_all([course, admin, instructor, evaluator, student, other])
    s.flush()
    for u in (instructor, evaluator, student, other):
        s.add(CourseMembership(course_id=course.id, user_id=u.id, role=u.role))
    asg = Assignment(course_id=course.id, title="Capstone: Task Manager API",
                     description="Build a task-management REST API with authentication, persistence, tests and docs.")
    s.add(asg)
    s.flush()
    rubric = Rubric(assignment_id=asg.id, version=1, is_published=True, is_active=True, created_by=instructor.id)
    for i, (key, title, w, sources, desc, guide) in enumerate(DEFAULT_RUBRIC):
        rubric.criteria.append(RubricCriterion(key=key, title=title, weight=w, description=desc, max_score=10,
                                               scoring_guide=guide, evidence_sources=sources, position=i))
    s.add(rubric)
    s.commit()
    return {
        "course_id": course.id, "assignment_id": asg.id, "rubric_id": rubric.id,
        "users": {u.role.value if u is not other else "student2": u.id for u in (admin, instructor, evaluator, student, other)},
        "tokens": {name: issue_token(u.id, u.role.value) for name, u in
                   {"admin": admin, "instructor": instructor, "evaluator": evaluator, "student": student,
                    "student2": other}.items()},
    }


if __name__ == "__main__":
    import json

    from app import db

    db.init_db()
    with db.SessionLocal() as s:
        print(json.dumps(seed(s), indent=2))
