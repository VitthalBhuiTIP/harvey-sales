"""Import a list of people (emails) from CSV or Excel.

Each row needs an email address; everything else is optional and matched by
column name, case-insensitively (Email / Work Email, First Name, Last Name or
Full Name, Title / Job Title, Company, Website, Phone, LinkedIn, Industry,
Location, Status). A file that is just a column of addresses with no header
works too.

For every usable row:

- the **company** comes from the Website column, else the email's domain
  (``pat@acme.example`` → acme.example), and is created if Harvey doesn't
  have it yet (source ``csv_import``, so "only my imported list" covers it);
- the **contact** is saved with ``email_status = 'guess'``, whatever the file
  says. Harvey never sends to a guess: the verify step grades it first.

Rows are skipped, each with a reason, for: no valid email, a personal mailbox
(gmail.com, outlook.com…: no company to research), a status that marks the
person unsubscribed / bounced / do-not-contact, a duplicate in the file, or
an address Harvey already has (so an earlier opt-out is never undone).
Nothing here sends email.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from harvey.company_import import INACTIVE, domain_from_url, plain_text
from harvey.models.company import Company
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from harvey.state import FREE_MAIL_DOMAINS

SOURCE = "contact_import"
COMPANY_SOURCE = "csv_import"
MAX_XLSX_BYTES = 10 * 1024 * 1024

COLUMNS = {
    "email": ("email", "emailaddress", "workemail", "businessemail", "mail", "emailid", "contactemail"),
    "first_name": ("firstname", "first", "givenname", "fname"),
    "last_name": ("lastname", "last", "surname", "familyname", "lname"),
    "name": ("name", "fullname", "contactname", "contact", "person"),
    "title": ("title", "jobtitle", "position", "role", "designation", "job"),
    "company": ("company", "companyname", "organization", "organisation", "account", "employer"),
    "website": ("website", "domain", "companywebsite", "companydomain", "url", "web"),
    "phone": ("phone", "phonenumber", "mobile", "telephone", "directphone"),
    "linkedin": ("linkedin", "linkedinurl", "linkedinprofile", "personlinkedinurl"),
    "industry": ("industry", "sector", "vertical"),
    "location": ("location", "country", "city", "region"),
    "status": ("status", "emailstatus", "contactstatus", "subscription"),
}
# A status that means "don't contact this person", on top of company INACTIVE.
DO_NOT_CONTACT = INACTIVE | {
    "unsubscribed", "optedout", "opted out", "opt out", "optout", "bounced", "bounce",
    "invalid", "donotcontact", "do not contact", "dnc", "do_not_contact", "spam",
    "complained", "suppressed",
}
# Shared inboxes: a row like info@ is the company's general address, not a person.
ROLE_LOCALS = {
    "info", "sales", "contact", "hello", "hi", "admin", "support", "office", "team",
    "enquiries", "enquiry", "inquiries", "marketing", "hr", "careers", "jobs", "billing",
    "accounts", "help", "service", "mail", "general", "reception", "press", "media",
}
EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass
class ContactRow:
    email: str
    domain: str
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    company: str = ""
    phone: str = ""
    linkedin: str = ""
    industry: str = ""
    location: str = ""
    role_inbox: bool = False


@dataclass
class ContactImportReport:
    rows: int = 0
    added: int = 0
    companies_added: int = 0
    domains: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def xlsx_to_csv(data: bytes) -> str:
    """The first worksheet of an .xlsx file as CSV text."""
    if len(data) > MAX_XLSX_BYTES:
        raise ValueError("The Excel file is larger than 10 MB; split it or save it as CSV.")
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ValueError("Reading Excel needs openpyxl: pip install openpyxl "
                         "(or save the sheet as CSV).") from None
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"Couldn't read that Excel file: {e}") from None
    out = io.StringIO()
    writer = csv.writer(out)
    for row in wb.worksheets[0].iter_rows(values_only=True):
        cells = ["" if v is None else (v.isoformat() if hasattr(v, "isoformat") else str(v)) for v in row]
        if any(c.strip() for c in cells):
            writer.writerow(cells)
    wb.close()
    return out.getvalue()


def _key(header: str) -> str:
    return re.sub(r"[^a-z]", "", (header or "").lower())


def _header_map(headers: list[str]) -> dict[str, str]:
    keys = {h: _key(h) for h in headers}
    mapping = {}
    for name, aliases in COLUMNS.items():
        for alias in aliases:
            match = next((h for h, k in keys.items() if k == alias and h not in mapping.values()), None)
            if match is not None:
                mapping[name] = match
                break
    return mapping


def names_from_email(local: str) -> tuple[str, str]:
    """'pat.example' / 'pat_example' / 'pat-example' → ('Pat', 'Example'). Else blanks."""
    parts = [p for p in re.split(r"[._-]", local) if p]
    if len(parts) == 2 and all(p.isalpha() and len(p) >= 2 for p in parts):
        return parts[0].capitalize(), parts[1].capitalize()
    return "", ""


def parse_contacts(text: str, default_industry: str = "") -> tuple[list[ContactRow], list[str]]:
    """Contacts from CSV text, and a note for every row that was skipped."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        raise ValueError("The file is empty.")
    headers = rows[0]
    cols = _header_map(headers)
    first_line = 2
    if "email" not in cols:
        # No header we recognise: a bare list of addresses, or a header row
        # with an unusual name. Use the first column holding an address.
        at = next((i for i, c in enumerate(headers) if "@" in (c or "")), None)
        if at is None:
            at = next((i for r in rows[1:6] for i, c in enumerate(r) if "@" in (c or "")), None)
            if at is None:
                raise ValueError("The file needs an email column. Found: "
                                 + ", ".join(h for h in headers if h))
        else:
            first_line = 1
        headers = [f"col{i}" for i in range(max(len(r) for r in rows))]
        headers[at] = "email"
        cols = {"email": "email"}
        if first_line == 1:
            rows = [headers] + rows
    index = {h: i for i, h in enumerate(headers)}

    contacts, skipped, seen = [], [], set()
    for line, row in enumerate(rows[1:], start=first_line):
        def get(f):
            i = index.get(cols.get(f, ""), -1)
            return (row[i] if 0 <= i < len(row) else "").strip()

        email = get("email").lower().strip("<>; ")
        if email.startswith("mailto:"):
            email = email[7:]
        label = email or get("name") or f"{get('first_name')} {get('last_name')}".strip() or "no email"
        if not EMAIL_RE.match(email):
            skipped.append(f"row {line} ({label}): no valid email")
            continue
        if get("status").lower() in DO_NOT_CONTACT:
            skipped.append(f"row {line} ({email}): marked {get('status')}")
            continue
        if email in seen:
            skipped.append(f"row {line} ({email}): duplicate of an earlier row")
            continue
        seen.add(email)
        local, _, email_domain = email.partition("@")
        if email_domain in FREE_MAIL_DOMAINS:
            skipped.append(f"row {line} ({email}): personal mailbox, no company to research")
            continue

        first, last = get("first_name"), get("last_name")
        if not (first or last) and get("name"):
            first, _, last = get("name").partition(" ")
        role = local in ROLE_LOCALS
        if not (first or last) and not role:
            first, last = names_from_email(local)
        contacts.append(ContactRow(
            email=email,
            domain=domain_from_url(get("website")) or email_domain,
            first_name=first.strip().title() if first.isupper() or first.islower() else first.strip(),
            last_name=last.strip().title() if last.isupper() or last.islower() else last.strip(),
            title=plain_text(get("title"))[:200],
            company=get("company"),
            phone=get("phone"),
            linkedin=get("linkedin"),
            industry=get("industry") or default_industry,
            location=get("location"),
            role_inbox=role,
        ))
    return contacts, skipped


async def import_contacts(state, contacts: list[ContactRow]) -> ContactImportReport:
    """Save the contacts (and any new companies). Returns what happened."""
    report = ContactImportReport(rows=len(contacts))
    company_ids: dict[str, str] = {}
    for c in contacts:
        if await state.get_prospect_by_email(c.email):
            report.skipped.append(f"{c.email}: already a contact")
            continue
        company_id = company_ids.get(c.domain)
        if not company_id:
            existing = await state.get_company_by_domain(c.domain)
            if existing:
                company_id = existing.id
            else:
                company_id = await state.add_company(Company(
                    name=c.company or c.domain, domain=c.domain, website=f"https://{c.domain}",
                    industry=c.industry, location=c.location, source=COMPANY_SOURCE))
                report.companies_added += 1
            company_ids[c.domain] = company_id
            report.domains.append(c.domain)
        named = bool(c.first_name or c.last_name)
        await state.add_prospect(Prospect(
            company_id=company_id,
            first_name=c.first_name,
            last_name=c.last_name,
            email=c.email,
            email_status="guess",
            phone=c.phone,
            linkedin_url=c.linkedin,
            title=c.title or ("General inbox" if c.role_inbox and not named else "Employee"),
            company=c.company or c.domain,
            industry=c.industry,
            source=COMPANY_INBOX_SOURCE if c.role_inbox and not named else SOURCE,
            source_url="",
        ))
        report.added += 1
    if report.added:
        await state.log_action(action_type="import_contacts", agent="people", details={
            "added": report.added, "companies_added": report.companies_added,
            "skipped": len(report.skipped)})
    return report
