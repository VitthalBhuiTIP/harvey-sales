"""Lead flow 'run all steps': sites → people → OpenAI → MySQL, no network."""

import pytest

from harvey import analysis, company_import
from harvey.lead_flow import run_all
from harvey.models.company import Company
from harvey.state import StateManager
from tests.test_analysis import CONFIG, _openai, _result


@pytest.mark.asyncio
async def test_run_all_chains_the_steps_and_skips_what_is_done(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    async def no_site(domain):
        return "We sell things."
    monkeypatch.setattr(analysis, "homepage_text", no_site)

    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    await state.add_company(Company(name="Acme", domain="acme.example", source="csv_import"))

    profiled, searched = [], []

    async def fake_profile(state_, companies, config=None):
        profiled.extend(c["domain"] for c in companies)
        for c in companies:
            async with __import__("aiosqlite").connect(state_.db_path) as db:
                await db.execute("UPDATE companies SET profile_checked_at = CURRENT_TIMESTAMP "
                                 "WHERE id = ?", (c["id"],))
                await db.commit()
        return 3, []

    async def fake_find(state_, company, key, people_filter, max_people):
        searched.append(company["domain"])
        from harvey.models.prospect import Prospect
        await state_.add_prospect(Prospect(
            company_id=company["id"], first_name="Pat", last_name="Example", title="CEO",
            email="pat@acme.example", email_status="verified", company_name="Acme"))
        from harvey.people import PeopleResult
        return PeopleResult(domain=company["domain"], found=1, added=1, searches_used=1, available=1)

    import harvey.collectors.profile as profile_mod
    import harvey.people as people_mod
    monkeypatch.setattr(profile_mod, "profile_companies", fake_profile)
    monkeypatch.setattr(people_mod, "find_people", fake_find)

    real_analyse = analysis.analyse
    seen = []

    async def analyse_offline(state_, config, companies, max_drafts=3, progress=lambda s: None):
        prospects = await state_.get_contacts_for_company(companies[0]["id"])
        return await real_analyse(state_, config, companies, max_drafts=max_drafts,
                                  client=_openai(_result([p.id for p in prospects]), seen=seen))
    monkeypatch.setattr(analysis, "analyse", analyse_offline)

    r = await run_all(state, CONFIG, hunter_key="k", max_people=1, sync_mysql=False)
    assert (r.profiled, r.facts, r.people_added, r.analysed, r.drafts) == (1, 3, 1, 1, 1)
    assert r.errors == []

    # Second run: site already read, enough contacts, already analysed → no spend.
    r = await run_all(state, CONFIG, hunter_key="k", max_people=1, sync_mysql=False)
    assert (r.profiled, r.people_added, r.hunter_searches, r.analysed) == (0, 0, 0, 0)
    assert profiled == ["acme.example"] and searched == ["acme.example"] and len(seen) == 1
    assert any("already read" in s for s in r.skipped)
    assert any("already analysed" in s for s in r.skipped)


@pytest.mark.asyncio
async def test_run_all_without_keys_skips_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    r = await run_all(state, CONFIG, read_sites=False, sync_mysql=False)
    assert r.errors == []
    assert any("HUNTER_API_KEY" in s for s in r.skipped)
    assert any("OPENAI_API_KEY" in s for s in r.skipped)
