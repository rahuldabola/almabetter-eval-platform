from app import services


class Body:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=False):
        return {k: v for k, v in self.__dict__.items() if v is not None}


def test_update_rejects_other_users_task(monkeypatch):
    monkeypatch.setattr(services.repository, "get", lambda tid: {"id": tid, "owner_id": 2})
    assert services.update_task(1, 5, Body(title="x")) is None


def test_delete_own_task(monkeypatch):
    deleted = []
    monkeypatch.setattr(services.repository, "get", lambda tid: {"id": tid, "owner_id": 1})
    monkeypatch.setattr(services.repository, "delete", deleted.append)
    assert services.delete_task(1, 5) is True and deleted == [5]
