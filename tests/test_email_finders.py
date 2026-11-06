"""Findymail, Snov.io and Lusha email finders. No network."""

import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey import email_check, email_finders
from harvey.email_finders import Finder
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path, monkeypatch):
    for k in ("FINDYMAIL_API_KEY", "SNOV_API_KEY", "LUSHA_API_KEY", "SNOV_CLIENT_ID",
              "SNOV_CLIENT_SECRET", "HUNTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    s = StateManager(str(tmp_path / "harvey.db"))
    await s.init_db()
    return s


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_configured_order_and_snov_pair(monkeypatch):
    for k in ("FINDYMAIL_API_KEY", "SNOV_API_KEY", "LUSHA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LUSHA_API_KEY", "l")
    monkeypatch.setenv("SNOV_CLIENT_ID", "id")
    monkeypatch.setenv("SNOV_CLIENT_SECRET", "secret")
    monkeypatch.setenv("FINDYMAIL_API_KEY", "f")
    assert [(f.name, f.key) for f in email_finders.configured()] == [
        ("findymail", "f"), ("snov", "id:secret"), ("lusha", "l")]


@pytest.mark.asyncio
async def test_findymail():
    def handler(request):
        assert request.headers["authorization"] == "Bearer f"
        assert json.loads(request.content) == {"name": "Pat Example", "domain": "acme.example"}
        return httpx.Response(200, json={"contact": {"email": "Pat@Acme.example"}})
    assert await email_finders.findymail(_client(handler), "Pat", "Example", "acme.example", "f") == \
        ("pat@acme.example", "guess", "")


@pytest.mark.asyncio
async def test_snov_polls_and_reads_smtp_status():
    email_finders._snov_tokens.clear()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer t"
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"data": {"task_hash": "h"}})
        status = "in_progress" if calls.count(request.url.path) == 1 else "completed"
        return httpx.Response(200, json={"status": status, "data": [{"people": "Pat Example", "result": [
            {"email": "pat@acme.example", "smtp_status": "valid"}]}]})
    r = await email_finders.snov(_client(handler), "Pat", "Example", "acme.example", "id:secret",
                                 poll_delay=0)
    assert r == ("pat@acme.example", "verified", "")
    assert calls.count("/v2/emails-by-domain-by-name/result") == 2


@pytest.mark.asyncio
async def test_snov_needs_id_and_secret():
    email_finders._snov_tokens.clear()
    r = await email_finders.snov(_client(lambda r: httpx.Response(500)), "a", "b", "c", "justakey")
    assert "client_id:client_secret" in r[2]


@pytest.mark.asyncio
async def test_lusha_searches_then_reveals_a_work_email():
    def handler(request):
        assert request.headers["api_key"] == "l"
        body = json.loads(request.content)
        if request.url.path.endswith("/contacts/search"):
            assert body["contacts"][0]["companyDomain"] == "acme.example"
            return httpx.Response(200, json={"results": [{"id": "c1", "firstName": "Pat"}]})
        assert body == {"ids": ["c1"], "reveal": ["emails"]}
        return httpx.Response(200, json={"results": [{"id": "c1", "emails": [
            {"email": "pat@home.example", "type": "private"},
            {"email": "pat@acme.example", "type": "work"}]}]})
    assert await email_finders.lusha(_client(handler), "Pat", "Example", "acme.example", "l") == \
        ("pat@acme.example", "guess", "")


@pytest.mark.asyncio
async def test_find_falls_through_and_drops_failing_finders(state, monkeypatch):
    monkeypatch.setenv("FINDYMAIL_MONTHLY_CREDITS", "5")

    def handler(request):
        if "findymail" in request.url.host:
            return httpx.Response(402, json={"error": "Not enough credits"})
        if request.url.path.endswith("/contacts/search"):
            return httpx.Response(200, json={"results": [{"id": "c1"}]})
        return httpx.Response(200, json={"results": [{"emails": [{"email": "pat@acme.example", "type": "work"}]}]})
    finders = [Finder("findymail", "Findymail", "f"), Finder("lusha", "Lusha", "l")]
    email, status, by, errors = await email_finders.find(state, _client(handler), finders,
                                                         "Pat", "Example", "acme.example")
    assert (email, by) == ("pat@acme.example", "lusha")
    assert errors == ["Findymail 402: Not enough credits"]
    assert [f.name for f in finders] == ["lusha"]
    assert await email_finders.credits_remaining(state, "findymail") == 4


@pytest.mark.asyncio
async def test_check_emails_uses_other_finders_without_hunter(state, monkeypatch):
    monkeypatch.setenv("FINDYMAIL_API_KEY", "f")
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    p = Prospect(company_id=cid, first_name="Pat", last_name="Example", title="CEO",
                 email="", company_name="Acme")
    await state.add_prospect(p)

    def handler(request):
        return httpx.Response(200, json={"contact": {"email": "pat@acme.example"}})

    async def verify(email, env=None):
        return "verified"
    monkeypatch.setattr(email_check, "verify_published_address", verify)
    r = await email_check.check_emails(
        state, SimpleNamespace(reoon_api_key="r", zerobounce_api_key=""), client=_client(handler))
    # Found by Findymail as a guess, then graded by the verifier.
    assert (r.found, r.found_by, r.verified, r.hunter_searches) == (1, {"findymail": 1}, 1, 0)
    assert (await state.get_prospect_by_email("pat@acme.example")).email_status == "verified"
