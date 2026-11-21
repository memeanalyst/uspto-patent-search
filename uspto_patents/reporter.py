"""Failure self-reporting — Magnova template core (this actor ships it from day
one, 2026-11-09; first carrier was the Product Hunt Launches
Monitor, 2026-11-07).

Implements Hamzah's chosen design (see
~/workspace/goals/apify-actor-revenue/research/self-reporting-channel-design-20261107.md):
when a run fails outright, or finishes but its check broke silently
(the feed could not be fetched/parsed, or every item it tried to
deliver errored), the Actor reports itself to the private GitHub
repo memeanalyst/magnova-alerts by opening an issue — or, when an
open issue for the same actor + error class already exists, by
adding a "+1 occurrence" comment to it instead, so an outage
collapses into one thread per breakage instead of a storm.

Hard rules (from the design):
- Max ONE report per run (this module's caller invokes it at most
  once, on a terminal path).
- The report is STRIPPED: actor name, build, run status, error
  class + short sanitised message, counts, timestamp. NEVER
  customer inputs or keywords, dataset contents, env dumps, or the
  token itself. The token is never logged.
- Transport is one HTTPS call set to the GitHub API with the token
  from the MAGNOVA_ALERTS_TOKEN environment variable (a
  fine-grained token scoped to that one private repo, Issues
  read+write). When the variable is absent the module is DORMANT
  and returns immediately — no calls, no errors.
- Fail-silent: any failure inside reporting is swallowed and
  reported back as the string 'failed'; it must never change the
  run's outcome, output or charges.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

ALERTS_REPO = 'memeanalyst/magnova-alerts'
API_BASE = 'https://api.github.com'
ENV_VAR = 'MAGNOVA_ALERTS_TOKEN'
TIMEOUT_S = 5.0
MESSAGE_LIMIT = 300

_WS_RE = re.compile(r'\s+')
# Belt-and-braces: never let anything shaped like a credential reach
# the report body, whatever an exception message happened to carry.
_SECRET_SHAPE_RE = re.compile(
    r'(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{10,}'
)


def actor_build_label() -> str:
    """The running build's label from the platform environment."""
    return (
        os.environ.get('APIFY_ACTOR_BUILD_NUMBER')
        or os.environ.get('ACTOR_BUILD_NUMBER')
        or 'unknown'
    )


def sanitize_message(message: Any, limit: int = MESSAGE_LIMIT) -> str:
    """One line, length-capped, credential shapes redacted."""
    text = _WS_RE.sub(' ', str(message or '')).strip()
    text = _SECRET_SHAPE_RE.sub('[redacted]', text)
    if len(text) > limit:
        text = text[:limit - 1] + '…'
    return text


def build_report(*, actor_name: str, build: str, run_status: str,
                 error_class: str, message: str,
                 counts: dict[str, int],
                 now: datetime | None = None) -> tuple[str, str]:
    """The (title, body) of a failure report. The title is the
    dedupe key: identical actor + error class -> identical title."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    title = f'[{actor_name}] run failure — {error_class}'
    lines = [
        'Automated failure report from the Actor itself '
        '(Magnova self-reporting).',
        '',
        f'- Actor: {actor_name}',
        f'- Build: {build}',
        f'- Run status: {run_status}',
        f'- Error class: {error_class}',
        f'- Error: {sanitize_message(message)}',
        f'- Checks ok: {counts.get("checksOk", 0)}',
        f'- Checks errored: {counts.get("checksErrored", 0)}',
        f'- Items delivered: {counts.get("itemsDelivered", 0)}',
        f'- Items errored: {counts.get("itemsErrored", 0)}',
        f'- Reported at: {stamp}',
        '',
        'Stripped report by design: no customer inputs, keywords, '
        'dataset contents or credentials are ever included.',
    ]
    return title, '\n'.join(lines)


def _comment_body(*, run_status: str, message: str,
                  counts: dict[str, int],
                  now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return (
        f'+1 occurrence — {stamp} (run status {run_status}; '
        f'error: {sanitize_message(message, 200)}; '
        f'checks ok {counts.get("checksOk", 0)}, '
        f'checks errored {counts.get("checksErrored", 0)}, '
        f'items delivered {counts.get("itemsDelivered", 0)}, '
        f'items errored {counts.get("itemsErrored", 0)})'
    )


async def report_failure(*, actor_name: str, build: str,
                         run_status: str, error_class: str,
                         message: str, counts: dict[str, int],
                         token: str | None = None,
                         client: httpx.AsyncClient | None = None,
                         now: datetime | None = None) -> str:
    """Send one failure report. Returns 'dormant' (no token),
    'created', 'commented', or 'failed' (transport problem —
    swallowed). NEVER raises and NEVER logs the token."""
    if token is None:
        token = os.environ.get(ENV_VAR)
    if not token:
        return 'dormant'

    title, body = build_report(
        actor_name=actor_name, build=build, run_status=run_status,
        error_class=error_class, message=message, counts=counts,
        now=now,
    )
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'magnova-self-report',
    }
    issues_url = f'{API_BASE}/repos/{ALERTS_REPO}/issues'
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S))
    try:
        existing_number: int | None = None
        try:
            resp = await client.get(
                issues_url, params={'state': 'open', 'per_page': '100'},
                headers=headers, timeout=TIMEOUT_S,
            )
            if resp.status_code == 200:
                for issue in resp.json():
                    if not isinstance(issue, dict):
                        continue
                    if 'pull_request' in issue:
                        continue
                    if issue.get('title') == title:
                        number = issue.get('number')
                        if isinstance(number, int):
                            existing_number = number
                            break
        except Exception:  # noqa: BLE001 - dedupe is best-effort
            existing_number = None

        if existing_number is not None:
            resp = await client.post(
                f'{issues_url}/{existing_number}/comments',
                json={'body': _comment_body(
                    run_status=run_status, message=message,
                    counts=counts, now=now,
                )},
                headers=headers, timeout=TIMEOUT_S,
            )
            return 'commented' if resp.status_code < 300 else 'failed'

        resp = await client.post(
            issues_url, json={'title': title, 'body': body},
            headers=headers, timeout=TIMEOUT_S,
        )
        return 'created' if resp.status_code < 300 else 'failed'
    except Exception:  # noqa: BLE001 - reporting must never break a run
        return 'failed'
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
