"""Sales database: team columns are never overwritten, records build from SQLite."""

import pytest

from harvey.config import load_config
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.models.campaign import Campaign, EmailStep
from harvey.sales_db import TABLES, build_records, create_table_sql, relevance, upsert_sql
from harvey.state import StateManager


def test_upsert_never_writes_team_columns():
    for name, columns, _, _ in TABLES:
        sql, cols = upsert_sql(name, columns)
        team = [c for c, _, origin, _ in columns if origin == "[team]"]
        for col in team:
            assert f"`{col}`" not in sql, (name, col)
        assert cols[-1] == "synced_at"


def test_every_column_says_where_it_comes_from():
    for name, columns, pk, keys in TABLES:
        ddl = create_table_sql(name, columns, pk, keys)
        assert ddl.count("COMMENT '[") == len(columns)


def test_relevance_bands():
    assert [relevance(s) for s in (85, 70, 69, 40, 39, None)] == [
        "High", "High", "Medium", "Medium", "Low", "Low"]


@pytest.mark.asyncio
async def test_records_from_harvey_data(tmp_path):
    db = tmp_path / "harvey.db"
    state = StateManager(str(db))
    await state.init_db()
    cid = await state.add_company(Company(
        name="Acme Corp", domain="acme.example", website="https://acme.example",
        industry="Manufacturing", location="United States", source="csv_import"))
    p = Prospect(first_name="Pat", last_name="Example", email="pat@acme.example",
                 email_status="verified", title="CMO", company="Acme Corp", company_id=cid)
    await state.add_prospect(p)
    await state.add_campaign(Campaign(
        id="camp1", name="test", prospect_ids=[p.id],
        sequence=[EmailStep(step=1, subject="Hi {{first_name}}",
                            body="Note for {{company}}", delay_days=0)]))

    records = await build_records(db, load_config(), "claude:test")
    assert [c["name"] for c in records["companies"]] == ["Acme Corp"]
    assert records["contacts"][0]["email_status"] == "verified"
    draft = records["email_drafts"][0]
    assert (draft["subject"], draft["body"]) == ("Hi Pat", "Note for Acme Corp")
    assert draft["ai_model"] == "claude:test"
    kinds = {a["analysis_type"] for a in records["ai_analysis"]}
    assert kinds == {"company_fit", "contact_score"}
