"""Healthcheck: 200 con la DB arriba, 503 con la DB caida (fase 1)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_health_200_con_db_arriba(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def db_ok() -> bool:
        return True

    monkeypatch.setattr("app.main.check_database", db_ok)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "up"


def test_health_503_con_db_caida(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def db_down() -> bool:
        return False

    monkeypatch.setattr("app.main.check_database", db_down)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "down"
