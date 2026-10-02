import pytest

from app import config, db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    # Tests must not depend on, or touch, the developer's .env or real database.
    monkeypatch.setattr(config, "DB_FILE", tmp_path / "test.db")
    monkeypatch.setattr(config, "VAPI_WEBHOOK_SECRET", "")
    db.init()
    yield
