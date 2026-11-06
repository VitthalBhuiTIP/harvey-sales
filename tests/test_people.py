"""`harvey people`: every Hunter result saved, filters, and the monthly budget."""

import pytest
import pytest_asyncio

from harvey import people
from harvey.models.company import Company
from harvey.people import PeopleFilter, find_people, to_prospect
from harvey.state import StateManager


def _person(first, last, position, status="valid", kind="personal", **extra):
    email = f"{first.lower()}@acme.example" if first else "info@acme.example"
    return {"value": email, "type": kind, "first_name": first, "last_name": last,
            "position": position, "verification": {"status": status}, **extra}


PEOPLE = [
    _person("Pat", "Example", "Head of Strategy", seniority="executive", department="management"),
    _person("Sam", "Sample", "Software Engineer", status="accept_all"),
    _person("", "", "", kind="generic", department="support"),
]


@pytest_asyncio.fixture
async def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "2")
    s = StateManager(str(tmp_path / "harvey.db"))
    await s.init_db()
    return s


async def _company(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.example", source="csv_import"))
    return (await state.companies_for_people(domains=["acme.example"]))[0] | {"id": cid}


def test_filter_params_and_validation():
    f = PeopleFilter.from_args("personal", True, "sales,it", "strategy, ops")
    assert f.params() == {"type": "personal", "seniority": "senior,executive", "department": "sales,it"}
    assert f.keeps({"position": "VP Strategy"}) and not f.keeps({"position": "Engineer"})
    with pytest.raises(ValueError):
        PeopleFilter.from_args(departments="nonsense")


def test_every_title_kept_and_generic_becomes_inbox():
    company = {"id": "c1", "name": "Acme", "domain": "acme.example"}
    engineer = to_prospect(PEOPLE[1], company)
    assert (engineer.title, engineer.email_status, engineer.is_valid()) == ("Software Engineer", "risky", True)
    inbox = to_prospect(PEOPLE[2], company)
    assert inbox.is_company_inbox and inbox.is_valid()


@pytest.mark.asyncio
async def test_saves_everyone_and_stops_at_budget(state, monkeypatch):
    calls = []

    async def fake_page(domain, key, offset, limit, f):
        calls.append(offset)
        return PEOPLE[offset:offset + limit], 25, ""

    monkeypatch.setattr(people, "hunter_page", fake_page)
    monkeypatch.setenv("HUNTER_PAGE_SIZE", "1")
    company = await _company(state)

    r = await find_people(state, company, "key", PeopleFilter(), max_people=10)
    assert (r.added, r.available, r.searches_used) == (2, 25, 2)
    assert "budget" in r.note
    assert calls == [0, 1]

    r = await find_people(state, company, "key", PeopleFilter(), max_people=10)
    assert r.searches_used == 0 and "budget" in r.note
