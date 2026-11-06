"""People Data Labs enrichment and Hunter find-and-verify. No network."""

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey import email_check, pdl
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PDL_MONTHLY_CREDITS", "10")
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "10")
    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    cid = await state.add_company(Company(name="Acme", domain="acme.example", industry="Software"))
    pat = Prospect(company_id=cid, first_name="Pat", last_name="Example", title="Employee",
                   email="pat@acme.example", email_status="verified", company_name="Acme")
    lee = Prospect(company_id=cid, first_name="Lee", last_name="Sample", title="CTO",
                   email="lee@acme.example", email_status="guess", company_name="Acme")
    ana = Prospect(company_id=cid, first_name="Ana", last_name="Example", title="CFO",
                   email="", email_status="", company_name="Acme")
    for p in (pat, lee, ana):
        await state.add_prospect(p)
    return state, cid, pat.id, lee.id, ana.id


def _pdl(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["x-api-key"] == "k"
        if request.url.path.endswith("/company/enrich"):
            return httpx.Response(200, json={"status": 200, "likelihood": 8, "data": {
                "name": "acme", "size": "51-200", "industry": "computer software",
                "founded": 2015, "summary": "Makes things.", "tags": ["saas", True],
                "location": {"locality": "Austin", "region": "Texas", "country": "united states"},
                "total_funding_raised": 5000000, "latest_funding_stage": "series_a"}})
        return httpx.Response(200, json={"status": 200, "likelihood": 7, "data": {
            "job_title": "head of growth", "job_title_role": "marketing",
            "job_title_levels": ["director"], "linkedin_url": "linkedin.com/in/pat-example",
            "work_email": True, "skills": ["seo", "content"], "headline": "Growth lead"}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_pdl_fills_empty_fields_only_and_never_twice(setup):
    state, cid, pat, lee, ana = setup
    seen = []
    r = await pdl.enrich(state, "k", client=_pdl(seen))
    # One company, one contact with a usable email (Lee is a guess, Ana has none).
    assert (r.companies, r.people, r.credits, r.errors) == (1, 1, 2, [])

    company = (await state.companies_for_people(domains=["acme.example"]))[0]
    assert company["industry"] == "Software"          # kept: Harvey already knew it
    assert company["company_size"] == "51-200" and "Austin" in company["location"]
    p = await state.get_prospect_by_email("pat@acme.example")
    assert p.title == "head of growth" and p.seniority == "executive" and p.department == "marketing"
    assert p.linkedin_url.endswith("linkedin.com/in/pat-example")

    record = (await pdl.records(state.db_path, "person", [pat]))[pat]
    assert "work_email" not in record and record["skills"] == ["seo", "content"]
    assert (await pdl.records(state.db_path, "company", [cid]))[cid]["tags"] == ["saas"]

    r = await pdl.enrich(state, "k", client=_pdl(seen))
    assert (r.companies, r.people, len(seen)) == (0, 0, 2)
    assert await pdl.credits_remaining(state) == 8


@pytest.mark.asyncio
async def test_pdl_no_match_is_free_and_errors_stop(setup):
    state, *_ = setup

    def nomatch(request):
        return httpx.Response(404, json={"status": 404, "error": {"type": "not_found"}})
    r = await pdl.enrich(state, "k", client=httpx.AsyncClient(transport=httpx.MockTransport(nomatch)))
    assert (r.no_match, r.credits) == (2, 0)

    def broke(request):
        return httpx.Response(402, json={"status": 402, "error": {"message": "out of credits"}})
    await state.add_company(Company(name="Beta", domain="beta.example"))
    r = await pdl.enrich(state, "k", client=httpx.AsyncClient(transport=httpx.MockTransport(broke)))
    assert r.errors == ["PDL 402: out of credits"]


@pytest.mark.asyncio
async def test_analysis_input_carries_pdl_data(setup):
    from harvey.analysis import lead_input
    from tests.test_analysis import CONFIG
    state, cid, pat, *_ = setup
    await pdl.enrich(state, "k", client=_pdl([]))
    company = (await state.companies_for_people(domains=["acme.example"]))[0]
    payload, _ = await lead_input(state.db_path, company, CONFIG, "")
    assert payload["company_data"]["latest_funding_stage"] == "series_a"
    profile = next(c for c in payload["contacts"] if c["contact_id"] == pat)["profile"]
    assert profile["headline"] == "Growth lead"


@pytest.mark.asyncio
async def test_check_emails_finds_missing_and_grades_guesses(setup, monkeypatch):
    state, cid, pat, lee, ana = setup

    def hunter(request: httpx.Request) -> httpx.Response:
        assert request.url.params["first_name"] == "Ana"
        return httpx.Response(200, json={"data": {"email": "ana@acme.example", "score": 91,
                                                  "verification": {"status": "valid"}}})
    graded = []

    async def verify(email, env=None):
        graded.append(email)
        return "invalid"
    monkeypatch.setattr(email_check, "verify_published_address", verify)

    r = await email_check.check_emails(
        state, SimpleNamespace(reoon_api_key="", zerobounce_api_key=""), hunter_key="h",
        client=httpx.AsyncClient(transport=httpx.MockTransport(hunter)))
    assert (r.found, r.hunter_searches, r.checked, r.invalid) == (1, 1, 1, 1)
    assert graded == ["lee@acme.example"]       # Ana came back verified, so isn't re-checked
    assert (await state.get_prospect_by_email("ana@acme.example")).email_status == "verified"
    assert (await state.get_prospect_by_email("lee@acme.example")).email_status == "invalid"


@pytest.mark.asyncio
async def test_check_emails_without_any_key_skips(setup):
    state, *_ = setup
    r = await email_check.check_emails(state, SimpleNamespace(reoon_api_key="", zerobounce_api_key=""))
    assert r.checked == 0 and len(r.skipped) == 2
