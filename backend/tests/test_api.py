"""Integration tests for FastAPI endpoints using TestClient + SQLite."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database as db_module
from database import Base, Campaign
from main import app, get_db

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestSession = sessionmaker(bind=engine)


def override_get_db():
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_tables():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_stats_today_empty(client):
    r = client.get("/stats/today")
    assert r.status_code == 200
    data = r.json()
    assert data["messages_sent_today"] == 0
    assert data["daily_limit_remaining"] == 250


def test_create_and_list_campaign(client):
    payload = {"name": "Austin Q3", "target_area": "Austin", "min_price": 200000, "max_price": 600000}
    r = client.post("/campaigns", json=payload)
    assert r.status_code == 201
    campaign = r.json()
    assert campaign["name"] == "Austin Q3"
    assert campaign["is_active"] is True

    r2 = client.get("/campaigns")
    assert r2.status_code == 200
    assert len(r2.json()) == 1


def test_update_campaign(client):
    r = client.post("/campaigns", json={"name": "Test", "target_area": "Dallas"})
    cid = r.json()["id"]

    r2 = client.patch(f"/campaigns/{cid}", json={"is_active": False})
    assert r2.status_code == 200
    assert r2.json()["is_active"] is False


def test_list_leads_empty(client):
    r = client.get("/leads")
    assert r.status_code == 200
    assert r.json()["total"] == 0
