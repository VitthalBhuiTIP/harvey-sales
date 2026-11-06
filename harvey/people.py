"""Find the people at a company: Hunter's domain search, every result kept.

Scout's own Hunter strategy keeps only people whose title matches the ICP,
and only for companies with no contacts yet. This is the on-demand version
(`harvey people`, and "Find people" on a company in the dashboard): every
person Hunter has for the domain, filtered the way Hunter's own People view
filters (job title, decision makers, department, personal vs generic), and
saved as contacts.

Every request spends one Hunter search, counted against the same monthly
budget as the rest of Harvey (HUNTER_MONTHLY_SEARCHES), and stops before it.
Nothing here sends email.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx

from harvey.integrations.email_finder import (
    hunter_searches_remaining,
    record_hunter_search,
)
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect

logger = logging.getLogger("harvey.people")

HUNTER_URL = "https://api.hunter.io/v2"
# Hunter's free plan returns at most 10 people a request; paid plans allow 100.
DEFAULT_PAGE_SIZE = 10
STATUS_MAP = {"valid": "verified", "accept_all": "risky",
              "invalid": "invalid", "disposable": "invalid"}
DEPARTMENTS = ("executive", "it", "finance", "management", "sales", "legal", "support",
               "hr", "marketing", "communication", "education", "design", "health",
               "operations")
# What Hunter's People view calls decision makers.
DECISION_MAKER_SENIORITY = ("senior", "executive")


@dataclass
class PeopleFilter:
    email_type: str = ""            # "", "personal" or "generic"
    decision_makers: bool = False
    departments: list[str] = field(default_factory=list)
    title_keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_args(cls, email_type="", decision_makers=False, departments="", titles=""):
        split = lambda s: [x.strip().lower() for x in (s or "").split(",") if x.strip()]  # noqa: E731
        f = cls(
            email_type=(email_type or "").strip().lower(),
            decision_makers=bool(decision_makers),
            departments=split(departments) if isinstance(departments, str) else list(departments or []),
            title_keywords=split(titles) if isinstance(titles, str) else list(titles or []),
        )
        if f.email_type not in ("", "personal", "generic"):
            raise ValueError("email type must be personal or generic")
        bad = [d for d in f.departments if d not in DEPARTMENTS]
        if bad:
            raise ValueError(f"unknown department {', '.join(bad)}; use {', '.join(DEPARTMENTS)}")
        return f

    def params(self) -> dict:
        p = {}
        if self.email_type:
            p["type"] = self.email_type
        if self.decision_makers:
            p["seniority"] = ",".join(DECISION_MAKER_SENIORITY)
        if self.departments:
            p["department"] = ",".join(self.departments)
        return p

    def keeps(self, person: dict) -> bool:
        """Title keywords aren't a Hunter filter, so they're applied here."""
        if not self.title_keywords:
            return True
        title = (person.get("position") or "").lower()
        return any(k in title for k in self.title_keywords)


@dataclass
class PeopleResult:
    domain: str
    found: int = 0
    added: int = 0
    already_known: int = 0
    available: int | None = None
    searches_used: int = 0       # Hunter searches
    credits_used: int = 0        # Apollo credits
    note: str = ""
    stop: bool = False           # every source's monthly budget is used up


def page_size() -> int:
    try:
        return max(1, min(int(os.getenv("HUNTER_PAGE_SIZE") or DEFAULT_PAGE_SIZE), 100))
    except ValueError:
        return DEFAULT_PAGE_SIZE


async def hunter_page(domain: str, api_key: str, offset: int, limit: int,
                      people_filter: PeopleFilter) -> tuple[list[dict], int | None, str]:
    """One domain-search page: (people, total Hunter has for the filter, error)."""
    params = {"domain": domain, "api_key": api_key, "limit": limit, "offset": offset,
              **people_filter.params()}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{HUNTER_URL}/domain-search", params=params)
    except httpx.HTTPError as e:
        return [], None, f"Hunter unreachable: {e}"
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if resp.status_code != 200:
        errors = body.get("errors") or [{}]
        return [], None, f"Hunter {resp.status_code}: {errors[0].get('details') or resp.text[:150]}"
    data = body.get("data") or {}
    meta = body.get("meta") or {}
    total = (meta.get("results") if isinstance(meta.get("results"), int) else None)
    emails = data.get("emails")
    return (emails if isinstance(emails, list) else []), total, ""


async def hunter_account(api_key: str) -> dict:
    """Plan and search credits straight from Hunter (free: doesn't spend a search)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{HUNTER_URL}/account", params={"api_key": api_key})
        data = (resp.json() or {}).get("data") or {}
    except (httpx.HTTPError, ValueError) as e:
        return {"error": str(e)}
    searches = ((data.get("requests") or {}).get("searches") or {})
    return {"plan": data.get("plan_name") or "", "used": searches.get("used"),
            "available": searches.get("available")}


def to_prospect(person: dict, company: dict) -> Prospect:
    generic = (person.get("type") or "") == "generic" or not (person.get("first_name") and person.get("last_name"))
    title = (person.get("position") or "").strip()
    department = (person.get("department") or "").strip()
    status = STATUS_MAP.get(((person.get("verification") or {}).get("status") or "").lower(), "guess")
    domain = company.get("domain") or ""
    return Prospect(
        first_name="" if generic else person.get("first_name") or "",
        last_name="" if generic else person.get("last_name") or "",
        email=(person.get("value") or "").strip().lower(),
        email_status=status,
        email_verified=status == "verified",
        phone=person.get("phone_number") or "",
        linkedin_url=person.get("linkedin") or "",
        title=title or (f"{department.title()} (general inbox)" if generic else department.title() or "Employee"),
        seniority=person.get("seniority") or "",
        department=department,
        company=company.get("name") or domain,
        company_id=company["id"],
        industry=company.get("industry") or "",
        source=COMPANY_INBOX_SOURCE if generic else "hunter_domain_search",
        source_url=((person.get("sources") or [{}])[0] or {}).get("uri") or f"https://{domain}",
    )


async def find_people(state, company: dict, api_key: str, people_filter: PeopleFilter,
                      max_people: int = 10) -> PeopleResult:
    """Page through Hunter for one company, saving each person as a contact,
    until max_people are found, Hunter runs out, or the monthly budget does."""
    domain = (company.get("domain") or "").strip()
    result = PeopleResult(domain=domain)
    if not domain:
        result.note = "no website"
        return result
    if not api_key:
        result.note = "HUNTER_API_KEY is not set"
        return result

    limit, offset = page_size(), 0
    while result.found < max_people:
        if await hunter_searches_remaining(state) <= 0:
            result.note = "monthly Hunter budget used up (HUNTER_MONTHLY_SEARCHES)"
            break
        await record_hunter_search(state)
        result.searches_used += 1
        page, total, error = await hunter_page(domain, api_key, offset, limit, people_filter)
        if total is not None:
            result.available = total
        if error:
            result.note = error
            break
        for person in page:
            if result.found >= max_people:
                break
            if not isinstance(person, dict) or not person.get("value") or not people_filter.keeps(person):
                continue
            result.found += 1
            prospect = to_prospect(person, company)
            if await state.get_prospect_by_email(prospect.email):
                result.already_known += 1
                continue
            await state.add_prospect(prospect)
            result.added += 1
        offset += len(page)
        if len(page) < limit or (total is not None and offset >= total):
            break
    if result.searches_used:
        await state.log_action(action_type="find_people", agent="people", details={
            "domain": domain, "found": result.found, "added": result.added,
            "available": result.available, "hunter_searches": result.searches_used,
        })
    return result


async def find_people_from(state, company: dict, people_filter: PeopleFilter,
                           max_people: int = 10, hunter_key: str = "",
                           apollo_key: str = "") -> PeopleResult:
    """Hunter first, then Apollo for whatever is still missing.

    Apollo only runs when the company still has fewer than ``max_people``
    contacts after Hunter, so its credits go where Hunter came up short (big
    companies, mostly). ``stop`` is set once neither source has budget left.
    """
    from harvey import apollo

    total = PeopleResult(domain=(company.get("domain") or "").strip())
    notes = []
    if hunter_key:
        r = await find_people(state, company, hunter_key, people_filter, max_people)
        total.found, total.added, total.already_known = r.found, r.added, r.already_known
        total.searches_used, total.available = r.searches_used, r.available
        if r.note:
            notes.append(f"Hunter: {r.note}")
    if apollo_key:
        have = len(await state.get_contacts_for_company(company["id"]))
        if have < max_people:
            r = await apollo.find_people(state, company, apollo_key, people_filter, max_people - have)
            total.found += r.found
            total.added += r.added
            total.already_known += r.already_known
            total.credits_used = r.credits_used
            if r.available is not None:
                total.available = max(total.available or 0, r.available)
            if r.note:
                notes.append(f"Apollo: {r.note}")
    total.note = "; ".join(notes)
    hunter_out = not hunter_key or await hunter_searches_remaining(state) <= 0
    apollo_out = not apollo_key or await apollo.credits_remaining(state) <= 0
    total.stop = hunter_out and apollo_out
    return total
