"""Find the people at a company through Apollo's API.

Two calls, because Apollo's search doesn't return emails:

1. People API Search (``mixed_people/api_search``) — free, no credits. Lists
   the people Apollo has at the domain, with a title and whether it holds an
   email for them.
2. Bulk People Enrichment (``people/bulk_match``) — 1 credit per person
   returned with an email. Only people who aren't already contacts are sent,
   at most 10 a request.

Credits are capped per calendar month (APOLLO_MONTHLY_CREDITS, default 50)
the same way Hunter searches are, and counted before each request. Apollo's
own ``verified`` becomes Harvey's verified; anything else stays ``guess``,
which Harvey never sends to. Nothing here sends email.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

from harvey.models.prospect import Prospect

logger = logging.getLogger("harvey.apollo")

APOLLO_URL = "https://api.apollo.io/api/v1"
SOURCE = "apollo_people_search"
DEFAULT_MONTHLY_CREDITS = 50
BULK_SIZE = 10            # Apollo's bulk_match limit
MAX_SEARCH_PAGES = 3
# Hunter's "decision makers" (senior + executive) in Apollo's vocabulary.
DECISION_MAKER_SENIORITIES = ("owner", "founder", "c_suite", "partner", "vp", "head", "director")
STATUS_MAP = {"verified": "verified"}


def api_key() -> str:
    return (os.getenv("APOLLO_API_KEY") or "").strip()


def _monthly_cap() -> int:
    try:
        return max(int(os.getenv("APOLLO_MONTHLY_CREDITS", "").strip() or DEFAULT_MONTHLY_CREDITS), 0)
    except ValueError:
        return DEFAULT_MONTHLY_CREDITS


def _usage_key() -> str:
    return f"apollo_credit_count:{datetime.now(timezone.utc):%Y-%m}"


async def credits_remaining(state) -> int:
    """Apollo credits Harvey may still spend this calendar month (UTC)."""
    try:
        used = int(await state.get_setting(_usage_key(), "0") or "0")
    except (ValueError, TypeError):
        used = 0
    return max(_monthly_cap() - used, 0)


def search_params(domain: str, people_filter, page: int, per_page: int) -> list[tuple[str, str]]:
    """Apollo takes its filters in the query string, arrays as ``name[]``."""
    params = [("q_organization_domains_list[]", domain),
              ("page", str(page)), ("per_page", str(per_page))]
    titles = list(people_filter.title_keywords)
    seniorities = list(DECISION_MAKER_SENIORITIES) if people_filter.decision_makers else []
    # Apollo's search has no department filter: departments become title
    # keywords (sales → "sales" titles), and executive becomes c-suite.
    if not titles:
        titles = [d for d in people_filter.departments if d != "executive"]
        if "executive" in people_filter.departments and "c_suite" not in seniorities:
            seniorities.append("c_suite")
    params += [("person_seniorities[]", s) for s in seniorities]
    params += [("person_titles[]", t) for t in titles]
    if titles:
        params.append(("include_similar_titles", "true"))
    return params


def _error(resp: httpx.Response) -> str:
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    detail = body.get("error") or body.get("message") or resp.text[:150]
    return f"Apollo {resp.status_code}: {detail}"


async def search_page(client: httpx.AsyncClient, domain: str, key: str, people_filter,
                      page: int, per_page: int) -> tuple[list[dict], int | None, str]:
    """(people, total Apollo has, error). Spends no credits."""
    try:
        resp = await client.post(f"{APOLLO_URL}/mixed_people/api_search",
                                 params=search_params(domain, people_filter, page, per_page),
                                 headers={"x-api-key": key, "Content-Type": "application/json"},
                                 json={})
    except httpx.HTTPError as e:
        return [], None, f"Apollo unreachable: {e}"
    if resp.status_code != 200:
        return [], None, _error(resp)
    try:
        body = resp.json() or {}
    except ValueError:
        return [], None, "Apollo returned something that isn't JSON"
    people = body.get("people")
    total = body.get("total_entries")
    return (people if isinstance(people, list) else []), (total if isinstance(total, int) else None), ""


async def enrich_people(client: httpx.AsyncClient, ids: list[str], key: str) -> tuple[list[dict], str]:
    """Emails for up to 10 Apollo person ids. 1 credit per person with an email."""
    try:
        resp = await client.post(f"{APOLLO_URL}/people/bulk_match",
                                 params={"reveal_personal_emails": "false"},
                                 headers={"x-api-key": key, "Content-Type": "application/json"},
                                 json={"details": [{"id": i} for i in ids]})
    except httpx.HTTPError as e:
        return [], f"Apollo unreachable: {e}"
    if resp.status_code != 200:
        return [], _error(resp)
    try:
        matches = (resp.json() or {}).get("matches")
    except ValueError:
        return [], "Apollo returned something that isn't JSON"
    return [m for m in (matches or []) if isinstance(m, dict)], ""


def usable_email(person: dict) -> str:
    email = (person.get("email") or "").strip().lower()
    # Apollo answers with a placeholder when the email isn't unlocked.
    if "@" not in email or "not_unlocked" in email or email.startswith("email_"):
        return ""
    return email


def to_prospect(person: dict, company: dict) -> Prospect:
    departments = person.get("departments") or []
    department = (departments[0] if departments else "").removeprefix("master_").replace("_", " ")
    status = STATUS_MAP.get((person.get("email_status") or "").strip().lower(), "guess")
    domain = company.get("domain") or ""
    return Prospect(
        first_name=person.get("first_name") or "",
        last_name=person.get("last_name") or "",
        email=usable_email(person),
        email_status=status,
        email_verified=status == "verified",
        linkedin_url=person.get("linkedin_url") or "",
        title=(person.get("title") or "").strip() or "Employee",
        seniority=person.get("seniority") or "",
        department=department,
        company=company.get("name") or domain,
        company_id=company["id"],
        industry=company.get("industry") or "",
        source=SOURCE,
        source_url=f"https://app.apollo.io/#/people/{person.get('id')}" if person.get("id") else f"https://{domain}",
    )


def _known(contacts, person: dict) -> bool:
    """Already a contact: same Apollo id, or same first name and title (the
    search hides last names, and this saves a credit on people we have)."""
    pid = person.get("id") or ""
    first = (person.get("first_name") or "").strip().lower()
    title = (person.get("title") or "").strip().lower()
    for c in contacts:
        if pid and pid in (getattr(c, "source_url", "") or ""):
            return True
        if first and first == (c.first_name or "").strip().lower() and title == (c.title or "").strip().lower():
            return True
    return False


async def find_people(state, company: dict, key: str, people_filter, max_people: int = 10,
                      client: httpx.AsyncClient | None = None):
    """Search Apollo for one company, enrich up to ``max_people`` new people,
    save each one with an email as a contact."""
    from harvey.people import PeopleResult

    domain = (company.get("domain") or "").strip()
    result = PeopleResult(domain=domain)
    if not domain:
        result.note = "no website"
        return result
    if not key:
        result.note = "APOLLO_API_KEY is not set"
        return result
    if people_filter.email_type == "generic":
        result.note = "Apollo lists people, not shared inboxes"
        return result

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        contacts = await state.get_contacts_for_company(company["id"])
        candidates: list[str] = []
        per_page = min(100, max(max_people * 3, 10))
        for page in range(1, MAX_SEARCH_PAGES + 1):
            people, total, error = await search_page(client, domain, key, people_filter, page, per_page)
            if total is not None:
                result.available = total
            if error:
                result.note = error
                return result
            for p in people:
                if len(candidates) >= max_people:
                    break
                if not isinstance(p, dict) or not p.get("id") or p.get("has_email") is False:
                    continue
                if _known(contacts, p):
                    result.already_known += 1
                    continue
                candidates.append(p["id"])
            if len(candidates) >= max_people or len(people) < per_page:
                break

        for i in range(0, len(candidates), BULK_SIZE):
            chunk = candidates[i:i + BULK_SIZE]
            left = await credits_remaining(state)
            if left <= 0:
                result.note = "monthly Apollo budget used up (APOLLO_MONTHLY_CREDITS)"
                break
            chunk = chunk[:left]
            # Counted before the request, like Hunter: a call that fails
            # mid-flight may still have been charged.
            await state.increment_setting(_usage_key(), len(chunk))
            result.credits_used += len(chunk)
            matches, error = await enrich_people(client, chunk, key)
            if error:
                result.note = error
                break
            for person in matches:
                if not usable_email(person):
                    continue
                result.found += 1
                prospect = to_prospect(person, company)
                if await state.get_prospect_by_email(prospect.email):
                    result.already_known += 1
                    continue
                await state.add_prospect(prospect)
                result.added += 1
    finally:
        if own_client:
            await client.aclose()

    if result.credits_used or result.found:
        await state.log_action(action_type="find_people", agent="people", details={
            "domain": domain, "source": "apollo", "found": result.found, "added": result.added,
            "available": result.available, "apollo_credits": result.credits_used,
        })
    return result
