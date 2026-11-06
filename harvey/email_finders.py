"""More ways to find a named contact's email: Findymail, Snov.io, Lusha.

Used by ``harvey check-emails`` (and Lead flow's "Run all steps") after
Hunter's Email Finder comes up empty, cheapest-risk first:

- **Findymail** — ``POST /api/search/name`` (full name + domain). 1 credit
  only when an email is found.
- **Snov.io** — OAuth token from client id + secret, then the async
  ``emails-by-domain-by-name`` start/result pair. 1 credit per email
  returned. ``SNOV_API_KEY`` is ``client_id:client_secret`` (or set
  SNOV_CLIENT_ID and SNOV_CLIENT_SECRET).
- **Lusha** — v3 Search Contacts (name + domain; charged per result), then
  Enrich Contacts revealing emails only. Last, because the search itself costs.

Each has its own monthly cap (``<NAME>_MONTHLY_CREDITS``, default 50),
counted before every attempt. Only Snov reports an SMTP check (``valid``
becomes verified, a catch-all becomes risky); every other email comes back
as ``guess`` and goes through Harvey's verifiers before anything can use it.
Nothing here sends email.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("harvey.email_finders")

DEFAULT_MONTHLY_CREDITS = 50
FINDYMAIL_URL = "https://app.findymail.com/api"
SNOV_URL = "https://api.snov.io"
LUSHA_URL = "https://api.lusha.com/v3"


@dataclass
class Finder:
    name: str          # findymail / snov / lusha
    label: str
    key: str


def configured() -> list[Finder]:
    """The finders with a key in the environment, in the order they're tried."""
    snov = (os.getenv("SNOV_API_KEY") or "").strip()
    if not snov and os.getenv("SNOV_CLIENT_ID") and os.getenv("SNOV_CLIENT_SECRET"):
        snov = f"{os.getenv('SNOV_CLIENT_ID').strip()}:{os.getenv('SNOV_CLIENT_SECRET').strip()}"
    keys = [("findymail", "Findymail", os.getenv("FINDYMAIL_API_KEY")),
            ("snov", "Snov.io", snov),
            ("lusha", "Lusha", os.getenv("LUSHA_API_KEY"))]
    return [Finder(n, label, (k or "").strip()) for n, label, k in keys if (k or "").strip()]


def _cap(name: str) -> int:
    try:
        return max(int(os.getenv(f"{name.upper()}_MONTHLY_CREDITS", "").strip()
                       or DEFAULT_MONTHLY_CREDITS), 0)
    except ValueError:
        return DEFAULT_MONTHLY_CREDITS


def _usage_key(name: str) -> str:
    return f"{name}_credit_count:{datetime.now(timezone.utc):%Y-%m}"


async def credits_remaining(state, name: str) -> int:
    try:
        used = int(await state.get_setting(_usage_key(name), "0") or "0")
    except (ValueError, TypeError):
        used = 0
    return max(_cap(name) - used, 0)


async def record_credit(state, name: str, n: int = 1) -> None:
    await state.increment_setting(_usage_key(name), n)


def _err(service: str, resp: httpx.Response) -> str:
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    msg = body.get("error") or body.get("message") or body.get("errors") or resp.text[:150]
    if isinstance(msg, dict):
        msg = msg.get("message") or str(msg)
    return f"{service} {resp.status_code}: {msg}"


# ── Findymail ──

async def findymail(client: httpx.AsyncClient, first: str, last: str, domain: str,
                    key: str) -> tuple[str, str, str]:
    try:
        resp = await client.post(f"{FINDYMAIL_URL}/search/name",
                                 headers={"Authorization": f"Bearer {key}"},
                                 json={"name": f"{first} {last}".strip(), "domain": domain})
    except httpx.HTTPError as e:
        return "", "", f"Findymail unreachable: {e}"
    if resp.status_code == 404:
        return "", "", ""
    if resp.status_code != 200:
        return "", "", _err("Findymail", resp)
    try:
        contact = (resp.json() or {}).get("contact") or {}
    except ValueError:
        return "", "", ""
    return (contact.get("email") or "").strip().lower(), "guess", ""


# ── Snov.io ──

_snov_tokens: dict[str, tuple[str, float]] = {}


async def _snov_token(client: httpx.AsyncClient, key: str) -> tuple[str, str]:
    cached = _snov_tokens.get(key)
    now = datetime.now(timezone.utc).timestamp()
    if cached and cached[1] > now + 60:
        return cached[0], ""
    client_id, _, secret = key.partition(":")
    if not secret:
        return "", "Snov.io: set SNOV_API_KEY as client_id:client_secret"
    try:
        resp = await client.post(f"{SNOV_URL}/v1/oauth/access_token", data={
            "grant_type": "client_credentials", "client_id": client_id, "client_secret": secret})
    except httpx.HTTPError as e:
        return "", f"Snov.io unreachable: {e}"
    if resp.status_code != 200:
        return "", _err("Snov.io", resp)
    body = resp.json() or {}
    token = body.get("access_token") or ""
    _snov_tokens[key] = (token, now + int(body.get("expires_in") or 3600))
    return token, "" if token else "Snov.io returned no access token"


async def snov(client: httpx.AsyncClient, first: str, last: str, domain: str, key: str,
               poll_delay: float = 2.0, polls: int = 10) -> tuple[str, str, str]:
    token, error = await _snov_token(client, key)
    if error:
        return "", "", error
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = await client.post(f"{SNOV_URL}/v2/emails-by-domain-by-name/start", headers=headers,
                                 json={"rows": [{"first_name": first, "last_name": last, "domain": domain}]})
        if resp.status_code not in (200, 202):
            return "", "", _err("Snov.io", resp)
        task = ((resp.json() or {}).get("data") or {}).get("task_hash")
        if not task:
            return "", "", "Snov.io returned no task"
        for _ in range(polls):
            await asyncio.sleep(poll_delay)
            resp = await client.get(f"{SNOV_URL}/v2/emails-by-domain-by-name/result",
                                    headers=headers, params={"task_hash": task})
            if resp.status_code != 200:
                return "", "", _err("Snov.io", resp)
            body = resp.json() or {}
            if body.get("status") == "not_enough_credits":
                return "", "", "Snov.io: not enough credits"
            if body.get("status") == "completed":
                break
        else:
            return "", "", ""
    except httpx.HTTPError as e:
        return "", "", f"Snov.io unreachable: {e}"
    for person in body.get("data") or []:
        for match in person.get("result") or []:
            email = (match.get("email") or "").strip().lower()
            if not email or match.get("is_disposable") or match.get("is_webmail"):
                continue
            if match.get("smtp_status") == "valid":
                return email, "verified", ""
            reason = match.get("unknown_status_reason") or ""
            return email, "risky" if reason == "catchall" else "guess", ""
    return "", "", ""


# ── Lusha ──

async def lusha(client: httpx.AsyncClient, first: str, last: str, domain: str,
                key: str) -> tuple[str, str, str]:
    headers = {"api_key": key}
    try:
        resp = await client.post(f"{LUSHA_URL}/contacts/search", headers=headers, json={
            "contacts": [{"clientReferenceId": "1", "firstName": first, "lastName": last,
                          "companyDomain": domain}]})
        if resp.status_code != 200:
            return "", "", _err("Lusha", resp)
        found = [r for r in (resp.json() or {}).get("results") or []
                 if isinstance(r, dict) and r.get("id") and not r.get("error")]
        if not found:
            return "", "", ""
        resp = await client.post(f"{LUSHA_URL}/contacts/enrich", headers=headers,
                                 json={"ids": [found[0]["id"]], "reveal": ["emails"]})
        if resp.status_code != 200:
            return "", "", _err("Lusha", resp)
        results = (resp.json() or {}).get("results") or []
    except httpx.HTTPError as e:
        return "", "", f"Lusha unreachable: {e}"
    emails = [e for r in results if isinstance(r, dict) for e in (r.get("emails") or [])]
    # A work address first: a private one isn't who the email is meant for.
    emails.sort(key=lambda e: 0 if e.get("type") == "work" else 1)
    for e in emails:
        email = (e.get("email") or "").strip().lower()
        if email and e.get("type") != "private":
            return email, "guess", ""
    return "", "", ""


CALLS = {"findymail": findymail, "snov": snov, "lusha": lusha}


async def find(state, client: httpx.AsyncClient, finders: list[Finder], first: str, last: str,
               domain: str) -> tuple[str, str, str, list[str]]:
    """Try each finder in turn: (email, status, which finder, errors).

    A finder that errors (bad key, out of credits, rate limit) is dropped
    from ``finders`` so the rest of the run doesn't repeat the failure.
    """
    errors = []
    for f in list(finders):
        if await credits_remaining(state, f.name) <= 0:
            continue
        await record_credit(state, f.name)
        email, status, error = await CALLS[f.name](client, first, last, domain, f.key)
        if error:
            errors.append(error)
            finders.remove(f)
            continue
        if email:
            return email, status, f.name, errors
    return "", "", "", errors
