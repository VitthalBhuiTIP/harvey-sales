"""The Lead flow tab: step counts, ICP save, draft review and export.
Never sends: the drafts table is not the Outbox."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import harvey.config as harvey_config
import harvey.dashboard as dash
from harvey.analysis import ensure_tables
from harvey.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
CONFIG = SimpleNamespace(
    icp=SimpleNamespace(industries=["IT services"], company_size="50-500", titles=["CIO"],
                        geography=["United States"], target_market=""),
    product=SimpleNamespace(name="Example Tool", description="Tracks things"),
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: CONFIG)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")

    async def _init():
        await StateManager(db_path=str(db)).init_db()
        await ensure_tables(str(db))
    asyncio.run(_init())
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO companies (id, name, domain, source) VALUES ('c1', 'Acme', 'acme.example', 'csv_import')")
        c.execute("INSERT INTO prospects (id, company_id, first_name, title, email, email_status) "
                  "VALUES ('p1', 'c1', 'Pat', 'CIO', 'pat@acme.example', 'verified')")
        c.execute("INSERT INTO ai_analyses (company_id, model, relevance_score, relevance, "
                  "opportunities_json) VALUES ('c1', 'm', 82, 'High', '[\"Cloud move\"]')")
        c.execute("INSERT INTO ai_drafts (id, company_id, prospect_id, to_email, subject, body) "
                  "VALUES ('d1', 'c1', 'p1', 'pat@acme.example', 'hi', '=cmd')")
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        yield c


def test_flow_counts(client):
    f = client.get("/api/flow").json()
    assert (f["companies"], f["imported"], f["deliverable"], f["analysed"], f["high_fit"]) == (1, 1, 1, 1, 1)
    assert f["drafts"] == {"pending_review": 1} and f["icp"]["industries"] == ["IT services"]


def test_review_and_export(client):
    r = client.post("/api/drafts/d1", json={"status": "approved", "body": "Edited"}).json()
    assert r["success"] and r["draft"]["status"] == "approved" and r["draft"]["edited"] == 1
    assert not client.post("/api/drafts/d1", json={"status": "sent"}).json()["success"]
    assert client.post("/api/drafts/nope", json={}).status_code == 404
    drafts = client.get("/api/drafts?status=approved").json()["drafts"]
    assert [d["id"] for d in drafts] == ["d1"]
    csv_text = client.get("/api/drafts.csv").text
    assert "Edited" in csv_text and "pat@acme.example" in csv_text
    a = client.get("/api/analysis/c1").json()
    assert a["opportunities"] == ["Cloud move"]


def test_analysis_needs_a_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(dash, "load_dotenv", lambda *a, **k: None)
    r = client.post("/api/analysis/run", json={}).json()
    assert not r["success"] and "OPENAI_API_KEY" in r["message"]
