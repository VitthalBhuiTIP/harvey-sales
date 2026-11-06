"""Import a company list from CSV into the companies table.

For lists the user already has (a CRM export, a spreadsheet): each row
becomes a company, and Scout's known-companies backfill then looks for
contacts on its website, exactly as for companies `harvey discover` found.

Columns are matched by name, case-insensitively, so most exports work as
they are. Only a website (or domain) is required: without one Scout has
nothing to read. A status column (status / is_active) that marks a row
inactive skips it: a closed company is not a prospect.

`enrich` takes the companies just imported through the rest of the list
build: read their websites, find their people through Hunter, and refresh
the MySQL databases. It never writes or sends email.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from harvey.models.company import Company

logger = logging.getLogger(__name__)

# First matching header wins. Headers are compared lowercased and stripped
# of anything that isn't a letter, so "Company Name" matches "companyname".
COLUMNS = {
    "name": ("name", "companyname", "company", "organization", "organisation", "account"),
    "website": ("website", "domain", "url", "web", "homepage", "companywebsite"),
    "description": ("description", "about", "summary", "overview"),
    "location": ("headquarters", "location", "hq", "country", "city", "address"),
    "industry": ("industry", "sector", "vertical", "category"),
    "phone": ("phone", "phonenumber", "telephone"),
    "status": ("status", "isactive", "active", "companystatus"),
}
INACTIVE = {"0", "false", "no", "n", "inactive", "closed", "defunct", "dissolved", "acquired"}
MAX_DESCRIPTION = 2000


def read_csv_text(path: Path) -> str:
    """CSV text, whichever encoding a spreadsheet app saved it in."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def domain_from_url(url: str) -> str:
    """'https://www.Example.com/about' -> 'example.com'."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = urlparse(url).netloc.split("@")[-1].split(":")[0].lower()
    host = re.sub(r"^www\.", "", host)
    return host if "." in host else ""


def plain_text(value: str) -> str:
    """Strip HTML (exports often store rich-text descriptions) and squash whitespace."""
    value = value or ""
    if "<" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ")
    value = re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()
    return value[:MAX_DESCRIPTION]


def _header_map(headers: list[str]) -> dict[str, str]:
    keys = {h: re.sub(r"[^a-z]", "", (h or "").lower()) for h in headers}
    mapping = {}
    for field, aliases in COLUMNS.items():
        for alias in aliases:
            match = next((h for h, k in keys.items() if k == alias), None)
            if match is not None:
                mapping[field] = match
                break
    return mapping


def parse_companies(text: str, default_industry: str = "") -> tuple[list[Company], list[str]]:
    """Companies from CSV text, and a note for every row that was skipped."""
    reader = csv.DictReader(io.StringIO(text))
    cols = _header_map(reader.fieldnames or [])
    if "website" not in cols:
        raise ValueError(
            "The CSV needs a website or domain column. Found: "
            + ", ".join(h for h in (reader.fieldnames or []) if h)
        )

    companies, skipped, seen = [], [], set()
    for line, row in enumerate(reader, start=2):
        get = lambda f: (row.get(cols[f]) or "").strip() if f in cols else ""  # noqa: E731
        name = get("name")
        domain = domain_from_url(get("website"))
        if get("status").lower() in INACTIVE:
            skipped.append(f"row {line} ({name or domain or 'no name'}): marked {get('status')}")
            continue
        if not domain:
            skipped.append(f"row {line} ({name or 'no name'}): no usable website")
            continue
        if domain in seen:
            skipped.append(f"row {line} ({name or domain}): duplicate of an earlier row")
            continue
        seen.add(domain)
        companies.append(Company(
            name=name or domain,
            domain=domain,
            website=f"https://{domain}",
            description=plain_text(get("description")),
            location=get("location"),
            industry=get("industry") or default_industry,
            phone=get("phone"),
            source="csv_import",
        ))
    return companies, skipped


async def import_companies(state, companies: list[Company]) -> tuple[list[Company], list[str]]:
    """Save the companies not already in Harvey. Returns (added, notes on the rest)."""
    added, skipped = [], []
    for company in companies:
        if await state.company_exists(company.domain):
            skipped.append(f"{company.name}: already in Harvey")
            continue
        company.id = await state.add_company(company)
        added.append(company)
    return added, skipped


@dataclass
class EnrichReport:
    profiled: int = 0
    facts: int = 0
    people_added: int = 0
    hunter_searches: int = 0
    apollo_credits: int = 0
    people: list[dict] = field(default_factory=list)
    mysql: dict[str, int] = field(default_factory=dict)
    sales_db: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def enrich(state, config, domains: list[str], *, read_sites: bool = True,
                 hunter_key: str = "", people_filter=None, max_people: int = 10,
                 sync_mysql: bool = True, progress=lambda stage: None,
                 apollo_key: str = "") -> EnrichReport:
    """Company details, contacts and database insertion for ``domains``.

    Each step is independent: one failing (no Hunter key, MySQL down) is
    recorded in ``errors`` and the rest still run.
    """
    from harvey.state import DB_PATH

    report = EnrichReport()
    companies = await state.companies_for_people(domains=domains) if domains else []

    if read_sites and companies:
        progress(f"Reading {len(companies)} websites")
        try:
            from harvey.collectors.profile import profile_companies
            report.facts, _ = await profile_companies(
                state, [{"id": c["id"], "name": c["name"], "domain": c["domain"]} for c in companies],
                config=config)
            report.profiled = len(companies)
        except Exception as e:
            logger.exception("import: reading sites failed")
            report.errors.append(f"reading websites: {e}")

    if (hunter_key or apollo_key) and companies:
        from harvey.people import PeopleFilter, find_people_from
        for i, company in enumerate(companies, start=1):
            progress(f"Finding people at {company['name']} ({i}/{len(companies)})")
            # Hunter and Apollo return the same people first every time: a
            # company that already has max_people contacts would cost for nothing.
            known = len(await state.get_contacts_for_company(company["id"]))
            if known >= max_people:
                report.people.append({"company": company["name"], "added": 0, "available": 0,
                                      "note": f"already has {known} contacts; raise max people for more"})
                continue
            try:
                r = await find_people_from(state, company, people_filter or PeopleFilter(),
                                           max_people, hunter_key=hunter_key, apollo_key=apollo_key)
            except Exception as e:
                report.errors.append(f"people at {company['name']}: {e}")
                continue
            report.people_added += r.added
            report.hunter_searches += r.searches_used
            report.apollo_credits += r.credits_used
            report.people.append({"company": company["name"], "added": r.added,
                                  "available": r.available, "note": r.note})
            if r.stop:
                report.errors.append(r.note or "monthly people-finding budget used up")
                break

    if sync_mysql:
        progress("Saving to MySQL")
        try:
            from harvey.db_sync import sync_all
            from harvey.mysql_mirror import MySQLSettings
            MySQLSettings.from_env()   # a clear message when MYSQL_HOST / MYSQL_USER are missing
            result = await sync_all(state.db_path or DB_PATH, config, state)
            report.mysql, report.sales_db = result["mysql"], result["sales_db"]
        except Exception as e:
            logger.exception("import: MySQL sync failed")
            report.errors.append(f"MySQL: {e}")
    return report
