"""Importing a list of people (emails) from CSV or Excel. No network."""

import base64
import io

import pytest
import pytest_asyncio

from harvey.contact_import import import_contacts, names_from_email, parse_contacts, xlsx_to_csv
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager

CSV = """Work Email,First Name,Last Name,Job Title,Company,Status
pat.example@acme.example,PAT,EXAMPLE,CTO,Acme Inc,
lee@acme.example,Lee,Sample,VP Sales,Acme Inc,active
info@beta.example,,,,Beta,
sam.sample@beta.example,,,,,
someone@gmail.com,Some,One,CEO,Home,
bad-address,No,Email,,,
gone@acme.example,Gone,Person,,Acme Inc,Unsubscribed
pat.example@acme.example,Pat,Again,,,
"""


def test_parse_rules():
    rows, skipped = parse_contacts(CSV, "Software")
    assert [r.email for r in rows] == ["pat.example@acme.example", "lee@acme.example",
                                       "info@beta.example", "sam.sample@beta.example"]
    pat, lee, info, sam = rows
    assert (pat.first_name, pat.last_name, pat.title, pat.domain) == ("Pat", "Example", "CTO", "acme.example")
    assert info.role_inbox and info.first_name == ""
    assert (sam.first_name, sam.last_name) == ("Sam", "Sample")      # from the address
    assert sam.industry == "Software"
    reasons = " | ".join(skipped)
    assert "personal mailbox" in reasons and "no valid email" in reasons
    assert "marked Unsubscribed" in reasons and "duplicate" in reasons


def test_a_bare_list_of_emails_works():
    rows, skipped = parse_contacts("pat@acme.example\nlee@beta.example\n")
    assert [r.email for r in rows] == ["pat@acme.example", "lee@beta.example"] and skipped == []


def test_unknown_header_with_emails_in_rows():
    rows, _ = parse_contacts("Contact Point,Notes\npat@acme.example,met at expo\n")
    assert [r.email for r in rows] == ["pat@acme.example"]


def test_no_email_column_is_an_error():
    with pytest.raises(ValueError, match="email column"):
        parse_contacts("Name,Company\nPat,Acme\n")


def test_names_from_email():
    assert names_from_email("pat.example") == ("Pat", "Example")
    assert names_from_email("pexample") == ("", "")
    assert names_from_email("pat.j.example") == ("", "")


def test_xlsx_to_csv():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Email", "First Name", "Company"])
    ws.append(["pat@acme.example", "Pat", "Acme"])
    ws.append([None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    rows, _ = parse_contacts(xlsx_to_csv(buf.getvalue()))
    assert [(r.email, r.first_name, r.company) for r in rows] == [("pat@acme.example", "Pat", "Acme")]
    with pytest.raises(ValueError):
        xlsx_to_csv(b"not a workbook")


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "harvey.db"))
    await s.init_db()
    return s


@pytest.mark.asyncio
async def test_import_creates_companies_and_keeps_existing_people(state):
    cid = await state.add_company(Company(name="Acme Inc", domain="acme.example", source="discover"))
    await state.add_prospect(Prospect(company_id=cid, first_name="Lee", last_name="Sample",
                                      title="VP Sales", email="lee@acme.example",
                                      email_status="verified", company_name="Acme"))
    await state.update_prospect_status((await state.get_prospect_by_email("lee@acme.example")).id,
                                       "opted_out")
    rows, _ = parse_contacts(CSV)
    r = await import_contacts(state, rows)
    assert (r.added, r.companies_added) == (3, 1)          # beta.example is new
    assert any("lee@acme.example: already a contact" in s for s in r.skipped)

    lee = await state.get_prospect_by_email("lee@acme.example")
    assert lee.status == "opted_out" and lee.email_status == "verified"   # untouched
    pat = await state.get_prospect_by_email("pat.example@acme.example")
    assert pat.email_status == "guess" and pat.company_id == cid and pat.source == "contact_import"
    info = await state.get_prospect_by_email("info@beta.example")
    assert info.source == "company_inbox" and info.title == "General inbox"
    beta = await state.get_company_by_domain("beta.example")
    assert beta.source == "csv_import" and beta.name == "Beta"


def test_dashboard_endpoint_imports_excel(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    import asyncio
    import time
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import harvey.config as harvey_config
    import harvey.dashboard as dash
    import harvey.lead_flow as lead_flow

    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: SimpleNamespace())
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    asyncio.run(StateManager(db_path=str(db)).init_db())
    called = {}

    async def fake_run_all(state, config, **kw):
        called.update(kw)
        return lead_flow.FlowReport(emails_verified=1)
    monkeypatch.setattr(lead_flow, "run_all", fake_run_all)

    wb = openpyxl.Workbook()
    wb.active.append(["Email"])
    wb.active.append(["pat.example@acme.example"])
    buf = io.BytesIO()
    wb.save(buf)
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as client:
        resp = client.post("/api/contacts/import",
                           json={"xlsx_b64": base64.b64encode(buf.getvalue()).decode()}).json()
        assert resp["success"] and resp["added"] == 1 and resp["companies_added"] == 1
        for _ in range(100):
            job = client.get("/api/contacts/import").json()
            if not job["running"]:
                break
            time.sleep(0.05)
    assert job["report"]["emails_verified"] == 1 and job["report"]["added"] == 1
    assert called["find_people"] is False and called["analyse_leads"] is False
    assert called["source"] == "csv_import" and called["verify_emails"] is True
