"""Find and verify contacts' emails: Hunter Email Finder, then the verifiers.

Two passes over contacts Harvey already has:

1. **Find** — a named contact with no email gets Hunter's Email Finder
   (first name + last name + company domain). One Hunter search each,
   counted against HUNTER_MONTHLY_SEARCHES like every other Hunter search.
   If Hunter has nothing, Findymail, Snov.io and Lusha are tried in turn,
   whichever have keys (see ``harvey.email_finders``).
2. **Verify** — an email still marked ``guess`` goes through the same
   verifier chain Scout uses (ZeroBounce / Reoon / Hunter's Email Verifier,
   whichever keys are set). A clear verdict replaces the guess; an
   inconclusive one leaves it a guess, which Harvey never sends to.

Nothing here sends email.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiosqlite
import httpx

from harvey import email_finders
from harvey.integrations.email_finder import (
    hunter_searches_remaining,
    record_hunter_search,
    verify_published_address,
)

logger = logging.getLogger("harvey.email_check")

HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"
STATUS_MAP = {"valid": "verified", "accept_all": "risky", "invalid": "invalid"}


@dataclass
class CheckReport:
    found: int = 0
    checked: int = 0
    verified: int = 0
    risky: int = 0
    invalid: int = 0
    still_guess: int = 0
    hunter_searches: int = 0
    found_by: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def has_verifier(env, hunter_key: str) -> bool:
    return bool(hunter_key or getattr(env, "reoon_api_key", "") or getattr(env, "zerobounce_api_key", ""))


async def hunter_find_email(client: httpx.AsyncClient, domain: str, first: str, last: str,
                            key: str) -> tuple[str, str, str]:
    """(email, Harvey status, error) from Hunter's Email Finder."""
    try:
        resp = await client.get(HUNTER_FINDER_URL, params={
            "domain": domain, "first_name": first, "last_name": last, "api_key": key})
    except httpx.HTTPError as e:
        return "", "", f"Hunter unreachable: {e}"
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if resp.status_code != 200:
        errors = body.get("errors") or [{}]
        return "", "", f"Hunter {resp.status_code}: {errors[0].get('details') or resp.text[:150]}"
    data = body.get("data") or {}
    email = (data.get("email") or "").strip().lower()
    status = STATUS_MAP.get(((data.get("verification") or {}).get("status") or "").lower(), "guess")
    return email, status, ""


async def _rows(db_path: str, sql: str, params=()) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return [dict(r) for r in await db.execute_fetchall(sql, params)]


def _scope(source: str) -> tuple[str, list]:
    return (" AND c.source = ?", [source]) if source else ("", [])


async def check_emails(state, env, *, hunter_key: str = "", source: str = "", limit: int = 50,
                       find_missing: bool = True, progress=lambda stage: None,
                       client: httpx.AsyncClient | None = None) -> CheckReport:
    """Find missing emails, then verify guesses, for at most ``limit`` contacts each."""
    report = CheckReport()
    scope, args = _scope(source)
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        others = email_finders.configured() if find_missing else []
        finder_key = hunter_key
        if find_missing and (hunter_key or others):
            missing = await _rows(state.db_path, f"""
                SELECT p.id, p.first_name, p.last_name, c.domain FROM prospects p
                  JOIN companies c ON c.id = p.company_id
                 WHERE p.email = '' AND p.first_name != '' AND p.last_name != ''
                   AND c.domain != ''{scope} LIMIT ?""", (*args, limit))
            for i, p in enumerate(missing, start=1):
                if not finder_key and not others:
                    break   # every finder is out of budget or failed
                progress(f"Finding email for {p['first_name']} {p['last_name']} ({i}/{len(missing)})")
                email, status, by = "", "", ""
                # Hunter first: it's already paid for in the monthly searches.
                if finder_key:
                    if await hunter_searches_remaining(state) > 0:
                        await record_hunter_search(state)
                        report.hunter_searches += 1
                        email, status, error = await hunter_find_email(
                            client, p["domain"], p["first_name"], p["last_name"], finder_key)
                        by = "hunter" if email else ""
                        if error:
                            report.errors.append(error)
                            finder_key = ""   # bad key or out of searches: stop asking
                    else:
                        report.errors.append("monthly Hunter budget used up (HUNTER_MONTHLY_SEARCHES)")
                        finder_key = ""
                if not email and others:
                    email, status, by, errors = await email_finders.find(
                        state, client, others, p["first_name"], p["last_name"], p["domain"])
                    report.errors += errors
                if not email or await state.get_prospect_by_email(email):
                    continue
                await state.update_prospect_email(p["id"], email, status)
                report.found += 1
                report.found_by[by] = report.found_by.get(by, 0) + 1
        elif find_missing:
            report.skipped.append("finding missing emails: add HUNTER_API_KEY, FINDYMAIL_API_KEY, "
                                  "SNOV_API_KEY or LUSHA_API_KEY")

        if not has_verifier(env, hunter_key):
            report.skipped.append("verifying: add REOON_API_KEY, ZEROBOUNCE_API_KEY or HUNTER_API_KEY")
            return report
        guesses = await _rows(state.db_path, f"""
            SELECT p.id, p.email FROM prospects p JOIN companies c ON c.id = p.company_id
             WHERE p.email != '' AND COALESCE(p.email_status, '') IN ('', 'guess'){scope}
             ORDER BY p.created_at LIMIT ?""", (*args, limit))
        for i, p in enumerate(guesses, start=1):
            progress(f"Verifying {p['email']} ({i}/{len(guesses)})")
            try:
                status = await verify_published_address(p["email"], env=env)
            except Exception as e:
                report.errors.append(f"verifying {p['email']}: {e}")
                continue
            report.checked += 1
            if status == "guess":
                report.still_guess += 1
                continue
            await state.update_prospect_email(p["id"], p["email"], status)
            setattr(report, status, getattr(report, status) + 1)
    finally:
        if own:
            await client.aclose()
    if report.found or report.checked:
        await state.log_action(action_type="check_emails", agent="people", details={
            "found": report.found, "verified": report.verified, "risky": report.risky,
            "invalid": report.invalid, "still_guess": report.still_guess,
            "hunter_searches": report.hunter_searches})
    return report
