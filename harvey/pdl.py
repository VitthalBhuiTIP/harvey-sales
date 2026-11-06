"""People Data Labs: more about each company and each contact.

- **Company Enrichment** (by website): size, employee count, founded year,
  industry, location, funding, tags, summary.
- **Person Enrichment** (by LinkedIn URL, email, or name + company): current
  title, role, level, location, skills, headline.

PDL bills per match only (a 404 "no match" is free). Harvey still caps
matches per calendar month (PDL_MONTHLY_CREDITS, default 100) and records
every lookup, match or not, in ``pdl_records`` so nothing is looked up twice.
What it learns fills empty company and contact fields (never overwrites what
Harvey already has) and goes into the OpenAI lead analysis. Free PDL plans
return contact fields (emails, phones) as true/false, so Harvey never takes
emails from PDL. Nothing here sends email.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiosqlite
import httpx

logger = logging.getLogger("harvey.pdl")

PDL_URL = "https://api.peopledatalabs.com/v5"
DEFAULT_MONTHLY_CREDITS = 100
MIN_LIKELIHOOD = 6
COMPANY_FIELDS = ("name", "display_name", "size", "employee_count", "founded", "industry",
                  "type", "ticker", "website", "linkedin_url", "headline", "summary", "tags",
                  "inferred_revenue", "total_funding_raised", "latest_funding_stage",
                  "last_funding_date", "number_funding_rounds")
PERSON_FIELDS = ("full_name", "first_name", "last_name", "job_title", "job_title_role", "job_title_sub_role",
                 "job_title_levels", "job_company_name", "job_start_date", "linkedin_url",
                 "location_name", "location_country", "headline", "skills")
# PDL levels → Harvey's seniority vocabulary (what Hunter and the analysis use).
EXECUTIVE_LEVELS = {"cxo", "owner", "partner", "vp", "director"}
SENIOR_LEVELS = {"senior", "manager"}

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pdl_records (
    kind TEXT NOT NULL,              -- 'company' or 'person'
    ref_id TEXT NOT NULL,            -- companies.id or prospects.id
    matched INTEGER DEFAULT 0,
    likelihood INTEGER DEFAULT 0,
    data_json TEXT DEFAULT '',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, ref_id)
);
"""


class PDLError(Exception):
    pass


def api_key() -> str:
    return (os.getenv("PEOPLEDATALABS_API_KEY") or os.getenv("PDL_API_KEY") or "").strip()


def _monthly_cap() -> int:
    try:
        return max(int(os.getenv("PDL_MONTHLY_CREDITS", "").strip() or DEFAULT_MONTHLY_CREDITS), 0)
    except ValueError:
        return DEFAULT_MONTHLY_CREDITS


def _usage_key() -> str:
    return f"pdl_credit_count:{datetime.now(timezone.utc):%Y-%m}"


async def credits_remaining(state) -> int:
    try:
        used = int(await state.get_setting(_usage_key(), "0") or "0")
    except (ValueError, TypeError):
        used = 0
    return max(_monthly_cap() - used, 0)


async def ensure_tables(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(TABLE_SQL)
        await db.commit()


def _compact(data: dict, fields) -> dict:
    """Only real values: free plans answer some fields with true/false."""
    out = {}
    for k in fields:
        v = data.get(k)
        if v is None or isinstance(v, bool) or v == "" or v == []:
            continue
        if k == "tags" or k == "skills":
            v = [x for x in v if isinstance(x, str)][:15]
        if k == "summary" and isinstance(v, str):
            v = v[:600]
        out[k] = v
    if isinstance(data.get("location"), dict):
        loc = data["location"]
        out["location"] = ", ".join(x for x in (loc.get("locality"), loc.get("region"), loc.get("country"))
                                    if isinstance(x, str) and x)
    return out


async def lookup(client: httpx.AsyncClient, kind: str, params: dict, key: str) -> tuple[dict | None, int, str]:
    """(record or None for no match, likelihood, error)."""
    try:
        resp = await client.get(f"{PDL_URL}/{kind}/enrich",
                                params={**params, "min_likelihood": MIN_LIKELIHOOD},
                                headers={"X-Api-Key": key})
    except httpx.HTTPError as e:
        return None, 0, f"PDL unreachable: {e}"
    if resp.status_code == 404:
        return None, 0, ""
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if resp.status_code != 200:
        err = body.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return None, 0, f"PDL {resp.status_code}: {msg or resp.text[:150]}"
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return data, int(body.get("likelihood") or 0), ""


async def _save(db_path: str, kind: str, ref_id: str, record: dict | None, likelihood: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO pdl_records (kind, ref_id, matched, likelihood, data_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(kind, ref_id) DO UPDATE SET matched = excluded.matched,
                 likelihood = excluded.likelihood, data_json = excluded.data_json,
                 fetched_at = excluded.fetched_at""",
            (kind, ref_id, 1 if record else 0, likelihood, json.dumps(record) if record else ""))
        await db.commit()


async def _fill_company(db_path: str, company_id: str, c: dict):
    """Fill empty company fields only; what Harvey already knows wins."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """UPDATE companies SET
                 industry = CASE WHEN COALESCE(industry, '') = '' THEN ? ELSE industry END,
                 company_size = CASE WHEN COALESCE(company_size, '') = '' THEN ? ELSE company_size END,
                 location = CASE WHEN COALESCE(location, '') = '' THEN ? ELSE location END,
                 description = CASE WHEN COALESCE(description, '') = '' THEN ? ELSE description END,
                 updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (c.get("industry") or "", c.get("size") or "", c.get("location") or "",
             (c.get("headline") or c.get("summary") or "")[:500], company_id))
        await db.commit()


def seniority(levels) -> str:
    levels = {x for x in (levels or []) if isinstance(x, str)}
    if levels & EXECUTIVE_LEVELS:
        return "executive"
    if levels & SENIOR_LEVELS:
        return "senior"
    return ""


async def _fill_person(db_path: str, prospect_id: str, p: dict):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """UPDATE prospects SET
                 linkedin_url = CASE WHEN COALESCE(linkedin_url, '') = '' THEN ? ELSE linkedin_url END,
                 seniority = CASE WHEN COALESCE(seniority, '') = '' THEN ? ELSE seniority END,
                 department = CASE WHEN COALESCE(department, '') = '' THEN ? ELSE department END,
                 title = CASE WHEN COALESCE(title, '') IN ('', 'Employee') THEN ? ELSE title END,
                 first_name = CASE WHEN COALESCE(first_name, '') = '' AND COALESCE(last_name, '') = ''
                                   THEN ? ELSE first_name END,
                 last_name = CASE WHEN COALESCE(first_name, '') = '' AND COALESCE(last_name, '') = ''
                                  THEN ? ELSE last_name END,
                 updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (_linkedin(p.get("linkedin_url")), seniority(p.get("job_title_levels")),
             p.get("job_title_role") or "", p.get("job_title") or "",
             _cap(p.get("first_name")), _cap(p.get("last_name")), prospect_id))
        await db.commit()


def _cap(name) -> str:
    """PDL names are lowercase: 'pat' → 'Pat'."""
    return name.strip().title() if isinstance(name, str) and name.strip() else ""


def _linkedin(url) -> str:
    if not isinstance(url, str) or not url:
        return ""
    return url if url.startswith("http") else f"https://{url}"


@dataclass
class PDLReport:
    companies: int = 0
    people: int = 0
    no_match: int = 0
    credits: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def _todo(db_path: str, sql: str, params) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return [dict(r) for r in await db.execute_fetchall(sql, params)]


async def enrich(state, key: str, *, companies: bool = True, people: bool = True,
                 source: str = "", limit: int = 25, progress=lambda stage: None,
                 client: httpx.AsyncClient | None = None) -> PDLReport:
    """Look up companies, then contacts with a usable email, not looked up before.

    ``limit`` caps each kind per run. Stops at the monthly cap, or at the
    first error PDL returns (bad key, out of credits, rate limit).
    """
    report = PDLReport()
    if not key:
        report.skipped.append("People Data Labs: no PEOPLEDATALABS_API_KEY in .env")
        return report
    await ensure_tables(state.db_path)
    scope, args = (" AND c.source = ?", [source]) if source else ("", [])
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)

    async def budget_left() -> bool:
        if await credits_remaining(state) > 0:
            return True
        report.errors.append("monthly People Data Labs budget used up (PDL_MONTHLY_CREDITS)")
        return False

    try:
        if companies:
            rows = await _todo(state.db_path, f"""
                SELECT c.id, c.name, c.domain FROM companies c
                 WHERE c.domain != '' AND NOT EXISTS (SELECT 1 FROM pdl_records r
                       WHERE r.kind = 'company' AND r.ref_id = c.id){scope}
                 ORDER BY c.created_at LIMIT ?""", (*args, limit))
            for i, c in enumerate(rows, start=1):
                if not await budget_left():
                    return report
                progress(f"People Data Labs: {c['name']} ({i}/{len(rows)})")
                data, likelihood, error = await lookup(client, "company", {"website": c["domain"]}, key)
                if error:
                    report.errors.append(error)
                    return report
                record = _compact(data, COMPANY_FIELDS) if data else None
                await _save(state.db_path, "company", c["id"], record, likelihood)
                if record:
                    await state.increment_setting(_usage_key())
                    report.credits += 1
                    report.companies += 1
                    await _fill_company(state.db_path, c["id"], record)
                else:
                    report.no_match += 1

        if people:
            rows = await _todo(state.db_path, f"""
                SELECT p.id, p.first_name, p.last_name, p.email, p.linkedin_url, c.domain
                  FROM prospects p JOIN companies c ON c.id = p.company_id
                 WHERE p.email_status IN ('verified', 'risky') AND NOT EXISTS (
                       SELECT 1 FROM pdl_records r WHERE r.kind = 'person' AND r.ref_id = p.id){scope}
                 ORDER BY p.created_at LIMIT ?""", (*args, limit))
            for i, p in enumerate(rows, start=1):
                if not await budget_left():
                    return report
                progress(f"People Data Labs: {p['first_name']} {p['last_name']} ({i}/{len(rows)})")
                if p["linkedin_url"]:
                    params = {"profile": p["linkedin_url"]}
                elif p["email"]:
                    params = {"email": p["email"]}
                else:
                    params = {"first_name": p["first_name"], "last_name": p["last_name"],
                              "company": p["domain"]}
                data, likelihood, error = await lookup(client, "person", params, key)
                if error:
                    report.errors.append(error)
                    return report
                record = _compact(data, PERSON_FIELDS) if data else None
                await _save(state.db_path, "person", p["id"], record, likelihood)
                if record:
                    await state.increment_setting(_usage_key())
                    report.credits += 1
                    report.people += 1
                    await _fill_person(state.db_path, p["id"], record)
                else:
                    report.no_match += 1
    finally:
        if own:
            await client.aclose()
        if report.companies or report.people or report.no_match:
            await state.log_action(action_type="pdl_enrich", agent="people", details={
                "companies": report.companies, "people": report.people,
                "no_match": report.no_match, "credits": report.credits})
    return report


async def records(db_path: str, kind: str, ref_ids: list[str]) -> dict[str, dict]:
    """Matched records by ref id, or {} when the table doesn't exist yet."""
    if not ref_ids:
        return {}
    async with aiosqlite.connect(db_path) as db:
        exists = await db.execute_fetchall(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pdl_records'")
        if not exists:
            return {}
        rows = await db.execute_fetchall(
            f"SELECT ref_id, data_json FROM pdl_records WHERE kind = ? AND matched = 1 "
            f"AND ref_id IN ({','.join('?' * len(ref_ids))})", (kind, *ref_ids))
    out = {}
    for ref_id, data_json in rows:
        try:
            out[ref_id] = json.loads(data_json)
        except (TypeError, ValueError):
            pass
    return out
