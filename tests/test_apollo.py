"""Apollo people finder: free search, then paid enrichment. No network."""

import json

import httpx
import pytest
import pytest_asyncio

from harvey import apollo
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.people import PeopleFilter, PeopleResult, find_people_from
from harvey.state import StateManager


def _apollo(people, matches, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["x-api-key"] == "k"
        if request.url.path.endswith("/mixed_people/api_search"):
            return httpx.Response(200, json={"total_entries": len(people), "people": people})
        if request.url.path.endswith("/people/bulk_match"):
            ids = [d["id"] for d in json.loads(request.content)["details"]]
            return httpx.Response(200, json={"matches": [matches.get(i) for i in ids]})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APOLLO_MONTHLY_CREDITS", "5")
    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await state.add_prospect(Prospect(company_id=cid, first_name="Pat", last_name="Known",
                                      title="CEO", email="pat@acme.example",
                                      email_status="verified", company_name="Acme"))
    company = (await state.companies_for_people(domains=["acme.example"]))[0]
    return state, company


PEOPLE = [
    {"id": "p1", "first_name": "Pat", "title": "CEO", "has_email": True},        # already a contact
    {"id": "p2", "first_name": "Lee", "title": "VP Sales", "has_email": True},
    {"id": "p3", "first_name": "Sam", "title": "CTO", "has_email": False},       # no email: no credit
    {"id": "p4", "first_name": "Ana", "title": "Head of Marketing", "has_email": True},
]
MATCHES = {
    "p2": {"id": "p2", "first_name": "Lee", "last_name": "Sample", "title": "VP Sales",
           "email": "lee@acme.example", "email_status": "verified", "seniority": "vp",
           "departments": ["master_sales"], "linkedin_url": "https://www.linkedin.com/in/lee-sample"},
    "p4": {"id": "p4", "first_name": "Ana", "last_name": "Example", "title": "Head of Marketing",
           "email": "email_not_unlocked@domain.com", "email_status": "unavailable"},
}


@pytest.mark.asyncio
async def test_apollo_enriches_only_new_people_with_emails(setup):
    state, company = setup
    seen = []
    r = await apollo.find_people(state, company, "k", PeopleFilter(decision_makers=True), 10,
                                 client=_apollo(PEOPLE, MATCHES, seen))
    assert (r.found, r.added, r.already_known, r.credits_used) == (1, 1, 1, 2)
    search = seen[0]
    assert "person_seniorities%5B%5D=c_suite" in str(search.url)
    assert "q_organization_domains_list%5B%5D=acme.example" in str(search.url)
    assert json.loads(seen[1].content)["details"] == [{"id": "p2"}, {"id": "p4"}]

    lee = await state.get_prospect_by_email("lee@acme.example")
    assert lee.email_status == "verified" and lee.source == "apollo_people_search"
    assert lee.department == "sales" and "p2" in lee.source_url
    assert await apollo.credits_remaining(state) == 3


@pytest.mark.asyncio
async def test_apollo_stops_at_the_monthly_budget(setup, monkeypatch):
    state, company = setup
    monkeypatch.setenv("APOLLO_MONTHLY_CREDITS", "1")
    seen = []
    r = await apollo.find_people(state, company, "k", PeopleFilter(), 10,
                                 client=_apollo(PEOPLE, MATCHES, seen))
    assert r.credits_used == 1 and json.loads(seen[1].content)["details"] == [{"id": "p2"}]
    r = await apollo.find_people(state, company, "k", PeopleFilter(), 10,
                                 client=_apollo(PEOPLE, MATCHES, seen))
    assert r.credits_used == 0 and "budget" in r.note


def test_departments_become_titles_for_apollo():
    params = apollo.search_params("acme.example", PeopleFilter(departments=["sales", "executive"]), 1, 10)
    assert ("person_titles[]", "sales") in params and ("person_seniorities[]", "c_suite") in params
    assert ("person_titles[]", "executive") not in params


@pytest.mark.asyncio
async def test_hunter_first_then_apollo_for_the_gap(setup, monkeypatch):
    state, company = setup
    import harvey.people as people_mod
    calls = []

    async def hunter(state_, company_, key, f, max_people):
        calls.append(("hunter", max_people))
        return PeopleResult(domain="acme.example", found=0, searches_used=1)

    async def apollo_find(state_, company_, key, f, max_people):
        calls.append(("apollo", max_people))
        return PeopleResult(domain="acme.example", found=2, added=2, credits_used=2)

    monkeypatch.setattr(people_mod, "find_people", hunter)
    monkeypatch.setattr(apollo, "find_people", apollo_find)
    r = await find_people_from(state, company, PeopleFilter(), 3, hunter_key="h", apollo_key="a")
    # One contact already known, so Apollo is asked for the remaining 2.
    assert calls == [("hunter", 3), ("apollo", 2)]
    assert (r.added, r.searches_used, r.credits_used, r.stop) == (2, 1, 2, False)

    calls.clear()
    r = await find_people_from(state, company, PeopleFilter(), 1, hunter_key="", apollo_key="a")
    assert calls == []  # already has 1 contact: no Apollo credit spent
