"""Lead flow, all at once: read sites → find people (Hunter, then Apollo) → OpenAI analysis → MySQL.

One call runs Steps 2-5 of the dashboard's Lead flow so the only thing left
for a human is Step 6, reviewing the drafts. Each step is independent: one
failing (no Hunter key, OpenAI out of credits, MySQL down) is recorded and the
rest still run. Nothing here sends email: drafts land in ``ai_drafts``, which
Harvey's sender never reads.
"""

import logging
from dataclasses import dataclass, field

import aiosqlite

logger = logging.getLogger("harvey.lead_flow")


@dataclass
class FlowReport:
    companies: int = 0
    profiled: int = 0
    facts: int = 0
    people_added: int = 0
    hunter_searches: int = 0
    apollo_credits: int = 0
    emails_found: int = 0
    emails_verified: int = 0
    emails_invalid: int = 0
    pdl_companies: int = 0
    pdl_people: int = 0
    pdl_credits: int = 0
    analysed: int = 0
    drafts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    mysql: dict[str, int] = field(default_factory=dict)
    sales_db: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def _unprofiled(db_path: str, domains: list[str]) -> list[str]:
    if not domains:
        return []
    async with aiosqlite.connect(db_path) as db:
        rows = await db.execute_fetchall(
            f"SELECT domain FROM companies WHERE profile_checked_at IS NULL "
            f"AND domain IN ({','.join('?' * len(domains))})", domains)
    return [r[0] for r in rows]


async def run_all(state, config, *, source: str = "", read_sites: bool = True,
                  hunter_key: str = "", people_filter=None, max_people: int = 10,
                  analyse_limit: int = 10, max_drafts: int = 3, redo: bool = False,
                  sync_mysql: bool = True, progress=lambda stage: None,
                  apollo_key: str = "", verify_emails: bool = True, env=None,
                  pdl_key: str = "", pdl_limit: int = 25, find_people: bool = True,
                  analyse_leads: bool = True, verify_limit: int = 50) -> FlowReport:
    """Run Steps 2-5 for every company (or one ``source``, e.g. csv_import).

    Websites already read are skipped, companies that already have
    ``max_people`` contacts cost no Hunter search, and companies already
    analysed are skipped unless ``redo``. ``analyse_limit`` caps the OpenAI
    calls in one run.
    """
    from harvey.analysis import (AnalysisError, analyse, companies_to_analyse, ensure_tables,
                                 openai_settings)
    from harvey.company_import import enrich

    report = FlowReport()
    companies = await state.companies_for_people(source=source)
    domains = [c["domain"] for c in companies]
    report.companies = len(domains)

    # Step 3 first: the site text sharpens both the people search and the analysis.
    new_sites = await _unprofiled(state.db_path, domains) if read_sites else []
    if read_sites and not new_sites:
        report.skipped.append("websites: all already read")
    # Step 2: people. enrich() skips companies that already have enough contacts.
    if not find_people:
        report.skipped.append("finding more people: turned off")
    elif not hunter_key and not apollo_key:
        report.skipped.append("people: no HUNTER_API_KEY or APOLLO_API_KEY in .env")
    if new_sites:
        r = await enrich(state, config, new_sites, read_sites=True, sync_mysql=False,
                         progress=progress)
        report.profiled, report.facts = r.profiled, r.facts
        report.errors += r.errors
    if find_people and (hunter_key or apollo_key) and domains:
        r = await enrich(state, config, domains, read_sites=False, hunter_key=hunter_key,
                         apollo_key=apollo_key,
                         people_filter=people_filter, max_people=max_people,
                         sync_mysql=False, progress=progress)
        report.people_added, report.hunter_searches = r.people_added, r.hunter_searches
        report.apollo_credits = r.apollo_credits
        report.errors += r.errors

    # Step 2b: Hunter finds missing emails, the verifiers grade guesses.
    if verify_emails:
        from harvey.email_check import check_emails
        try:
            c = await check_emails(state, env, hunter_key=hunter_key, source=source,
                                   limit=max(1, verify_limit), progress=progress)
            report.emails_found = c.found
            report.emails_verified = c.verified + c.risky
            report.emails_invalid = c.invalid
            report.hunter_searches += c.hunter_searches
            report.skipped += c.skipped
            report.errors += c.errors
        except Exception as e:
            logger.exception("lead flow: email check failed")
            report.errors.append(f"email check: {e}")

    # Step 3b: People Data Labs fills in company and contact details.
    if pdl_key:
        from harvey import pdl
        try:
            d = await pdl.enrich(state, pdl_key, source=source, limit=max(1, pdl_limit),
                                 progress=progress)
            report.pdl_companies, report.pdl_people, report.pdl_credits = d.companies, d.people, d.credits
            report.errors += d.errors
        except Exception as e:
            logger.exception("lead flow: PDL failed")
            report.errors.append(f"People Data Labs: {e}")
    else:
        report.skipped.append("People Data Labs: no PEOPLEDATALABS_API_KEY in .env")

    # Step 4: OpenAI.
    try:
        if not analyse_leads:
            raise AnalysisError("turned off")
        openai_settings()
        await ensure_tables(state.db_path)
        todo = await companies_to_analyse(state.db_path, source=source, redo=redo,
                                          limit=max(1, analyse_limit))
        if not todo:
            report.skipped.append("analysis: every company already analysed")
        else:
            a = await analyse(state, config, todo, max_drafts=max_drafts, progress=progress)
            report.analysed, report.drafts = a.analysed, a.drafts
            report.tokens_in, report.tokens_out = a.tokens_in, a.tokens_out
            report.errors += a.errors
    except AnalysisError as e:
        report.skipped.append(f"analysis: {e}")
    except Exception as e:
        logger.exception("lead flow: analysis failed")
        report.errors.append(f"analysis: {e}")

    # Step 5: MySQL, last, so it carries everything above.
    if sync_mysql:
        r = await enrich(state, config, [], read_sites=False, sync_mysql=True, progress=progress)
        report.mysql, report.sales_db = r.mysql, r.sales_db
        report.errors += r.errors
    progress("Done")
    return report
