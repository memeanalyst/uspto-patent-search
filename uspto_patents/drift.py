"""Data-drift telemetry — Magnova template core (this actor ships it from day
one, 2026-11-09; first carrier was the Product Hunt Launches
Monitor).

Implements the corrected design in
~/workspace/goals/apify-actor-revenue/research/drift-telemetry-design-20261108.md:
failure self-reporting (reporter.py) catches runs that FAIL; this
module catches SILENT DRIFT — a run that succeeds and charges while
its delivered records quietly change shape (a field the listing
promises stops being populated, or a fixed-value field's
distribution flips), which no run statistic can see.

How it works:
- At run end the Actor fingerprints the records it delivered:
  per-field population rates, plus the value distribution of fields
  marked fixed-value in the claims table. NEVER record values,
  customer inputs or keywords — rates and counts only.
- The fingerprint is compared against the EXPECTED SHAPE shipped in
  the build (expected_shape.json). That table is derived from the
  LISTING'S CLAIMS, with ranges confirmed by censuses across varied
  inputs — never from a single run's own census, so a launch defect
  cannot be baked in as "normal".
- A field breaches when its observed rate falls outside
  [min - margin, max + margin] (both directions). Comparisons need
  at least the table's minimum sample of delivered records
  (default 30) so binomial noise cannot trip the sensor.
- Each breached field reports through the SAME channel as failure
  reports (private GitHub repo memeanalyst/magnova-alerts, token
  from MAGNOVA_ALERTS_TOKEN). Dedupe key is actor + field + BUILD:
  an open issue whose title matches the field AND whose body names
  the running build gets a "+1 occurrence" comment; otherwise a new
  issue is opened (max 3 new issues per run — further breached
  fields are listed in a comment on the first touched issue).
- Fail-silent everywhere: dormant when the token env var is absent,
  and any transport/computation problem is swallowed. Drift
  telemetry must never change a run's outcome, output or charges.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .reporter import (
    ALERTS_REPO,
    API_BASE,
    ENV_VAR,
    TIMEOUT_S,
)

log = logging.getLogger(__name__)

MAX_NEW_ISSUES_PER_RUN = 3
_SHAPE_FILE = Path(__file__).with_name('expected_shape.json')


# ---------------------------------------------------------------------------
# Expected shape (the claims table shipped inside the build)
# ---------------------------------------------------------------------------

def load_expected_shape() -> dict[str, Any]:
    """Read the claims table (expected_shape.json next to this file)."""
    return json.loads(_SHAPE_FILE.read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# Fingerprint + breach detection (pure functions — unit-tested)
# ---------------------------------------------------------------------------

def _populated(value: Any) -> bool:
    """A field counts as populated when the record carries a real
    value for it: not None, not an empty string, and not an empty
    list/dict (this Actor ships list-valued fields — buyerCountry,
    cpvCodes — where an empty list is as absent as a null)."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    return True


def _value_key(value: Any) -> str:
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if value is None:
        return 'null'
    return str(value)


def compute_fingerprint(records: list[dict[str, Any]],
                        shape: dict[str, Any]) -> dict[str, Any]:
    """Population rates (+ fixed-value distributions) for ``records``.

    Only rates and counts leave this function — never values."""
    n = len(records)
    fields: dict[str, Any] = {}
    for field, spec in (shape.get('fields') or {}).items():
        populated = sum(1 for r in records if _populated(r.get(field)))
        entry: dict[str, Any] = {
            'rate': (populated / n) if n else 0.0,
            'populated': populated,
        }
        if spec.get('fixedValue'):
            rates: dict[str, float] = {}
            for tracked in (spec.get('valueRates') or {}):
                count = sum(
                    1 for r in records
                    if _value_key(r.get(field)) == tracked
                )
                rates[tracked] = (count / n) if n else 0.0
            entry['valueRates'] = rates
        fields[field] = entry
    return {'n': n, 'fields': fields}


def _outside(rate: float, low: float, high: float, margin: float) -> bool:
    return rate < (low - margin) or rate > (high + margin)


def find_breaches(fingerprint: dict[str, Any], shape: dict[str, Any],
                  input_class: str) -> list[dict[str, Any]]:
    """Fields whose observed rates fall outside their claimed range
    (with the per-field absolute margin applied in both directions).
    Empty below the minimum sample — small runs stay silent."""
    n = fingerprint.get('n') or 0
    min_sample = int(shape.get('minSample') or 30)
    if n < min_sample:
        return []
    breaches: list[dict[str, Any]] = []
    for field, spec in (shape.get('fields') or {}).items():
        observed = (fingerprint.get('fields') or {}).get(field) or {}
        margin = float(spec.get('margin', 0.15))
        rate = float(observed.get('rate', 0.0))
        if _outside(rate, float(spec['min']), float(spec['max']), margin):
            breaches.append({
                'field': field, 'kind': 'population',
                'expectedMin': float(spec['min']),
                'expectedMax': float(spec['max']),
                'margin': margin, 'observed': rate,
                'n': n, 'inputClass': input_class,
            })
        if spec.get('fixedValue'):
            for tracked, tspec in (spec.get('valueRates') or {}).items():
                tmargin = float(tspec.get('margin', margin))
                trate = float(
                    (observed.get('valueRates') or {}).get(tracked, 0.0)
                )
                if _outside(trate, float(tspec['min']),
                            float(tspec['max']), tmargin):
                    breaches.append({
                        'field': field, 'kind': 'distribution',
                        'trackedValue': tracked,
                        'expectedMin': float(tspec['min']),
                        'expectedMax': float(tspec['max']),
                        'margin': tmargin, 'observed': trate,
                        'n': n, 'inputClass': input_class,
                    })
    return breaches


# ---------------------------------------------------------------------------
# Reporting (same channel + discipline as reporter.report_failure)
# ---------------------------------------------------------------------------

def _pct(rate: float) -> str:
    return f'{rate * 100:.1f}%'


def drift_title(actor_name: str, field: str) -> str:
    return f'[{actor_name}] data drift — {field}'


def build_drift_body(*, actor_name: str, build: str,
                     breach: dict[str, Any],
                     now: datetime | None = None) -> str:
    """The stripped body of a drift issue. The '- Build:' line is
    part of the dedupe key (actor + field + build), so it must keep
    exactly this shape."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    if breach['kind'] == 'distribution':
        check = (
            f"fixed-value distribution (rate of "
            f"'{breach['trackedValue']}')"
        )
    else:
        check = 'population rate'
    lines = [
        'Automated data-drift report from the Actor itself '
        '(Magnova drift telemetry).',
        '',
        f'- Actor: {actor_name}',
        f'- Build: {build}',
        f'- Field: {breach["field"]}',
        f'- Check: {check}',
        f'- Input class: {breach["inputClass"]}',
        f'- Expected: {_pct(breach["expectedMin"])}–'
        f'{_pct(breach["expectedMax"])} (absolute margin '
        f'{breach["margin"] * 100:.0f} points)',
        f'- Observed: {_pct(breach["observed"])} across '
        f'{breach["n"]} delivered records',
        f'- Reported at: {stamp}',
        '',
        'Stripped report by design: no record values, customer '
        'inputs, keywords, dataset contents or credentials are '
        'ever included.',
    ]
    return '\n'.join(lines)


def _drift_comment_body(*, build: str, breach: dict[str, Any],
                        now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return (
        f'+1 occurrence — {stamp} (build {build}; '
        f'observed {_pct(breach["observed"])} across {breach["n"]} '
        f'delivered records; input class {breach["inputClass"]})'
    )


async def report_drift(*, actor_name: str, build: str,
                       breaches: list[dict[str, Any]],
                       token: str | None = None,
                       client: httpx.AsyncClient | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
    """Report breached fields: one issue per actor+field+build
    (dedupe by comment), max MAX_NEW_ISSUES_PER_RUN new issues;
    additional breached fields are noted in a comment on the first
    touched issue. Returns a small outcome dict. NEVER raises and
    NEVER logs the token."""
    outcome: dict[str, Any] = {
        'status': 'ok', 'created': [], 'commented': [], 'overflow': [],
    }
    if not breaches:
        return outcome
    if token is None:
        token = os.environ.get(ENV_VAR)
    if not token:
        outcome['status'] = 'dormant'
        return outcome

    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'magnova-drift-telemetry',
    }
    issues_url = f'{API_BASE}/repos/{ALERTS_REPO}/issues'
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S))
    try:
        open_issues: list[dict[str, Any]] = []
        try:
            resp = await client.get(
                issues_url, params={'state': 'open', 'per_page': '100'},
                headers=headers, timeout=TIMEOUT_S,
            )
            if resp.status_code == 200:
                open_issues = [
                    i for i in resp.json()
                    if isinstance(i, dict) and 'pull_request' not in i
                ]
        except Exception:  # noqa: BLE001 - dedupe is best-effort
            open_issues = []

        first_touched: int | None = None
        created_count = 0
        any_failed = False
        overflow: list[dict[str, Any]] = []
        for breach in breaches:
            title = drift_title(actor_name, breach['field'])
            match: int | None = None
            for issue in open_issues:
                if issue.get('title') != title:
                    continue
                body = issue.get('body') or ''
                if f'- Build: {build}' in body:
                    number = issue.get('number')
                    if isinstance(number, int):
                        match = number
                        break
            if match is not None:
                resp = await client.post(
                    f'{issues_url}/{match}/comments',
                    json={'body': _drift_comment_body(
                        build=build, breach=breach, now=now)},
                    headers=headers, timeout=TIMEOUT_S,
                )
                if resp.status_code < 300:
                    outcome['commented'].append(breach['field'])
                    if first_touched is None:
                        first_touched = match
                else:
                    any_failed = True
                continue
            if created_count >= MAX_NEW_ISSUES_PER_RUN:
                overflow.append(breach)
                continue
            resp = await client.post(
                issues_url,
                json={'title': title, 'body': build_drift_body(
                    actor_name=actor_name, build=build,
                    breach=breach, now=now)},
                headers=headers, timeout=TIMEOUT_S,
            )
            if resp.status_code < 300:
                created_count += 1
                outcome['created'].append(breach['field'])
                try:
                    number = resp.json().get('number')
                except Exception:  # noqa: BLE001
                    number = None
                if first_touched is None and isinstance(number, int):
                    first_touched = number
            else:
                any_failed = True
        outcome['overflow'] = [b['field'] for b in overflow]
        if overflow and first_touched is not None:
            note_lines = [
                'Additional breached fields in the same run (issue '
                'cap reached; reported here instead of new issues):',
            ]
            for breach in overflow:
                note_lines.append(
                    f'- {breach["field"]}: observed '
                    f'{_pct(breach["observed"])} across {breach["n"]} '
                    f'delivered records (expected '
                    f'{_pct(breach["expectedMin"])}–'
                    f'{_pct(breach["expectedMax"])}).'
                )
            try:
                await client.post(
                    f'{issues_url}/{first_touched}/comments',
                    json={'body': '\n'.join(note_lines)},
                    headers=headers, timeout=TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 - best-effort note
                pass
        if outcome['created'] or outcome['commented'] or overflow:
            outcome['status'] = 'reported'
        elif any_failed:
            outcome['status'] = 'failed'
        return outcome
    except Exception:  # noqa: BLE001 - telemetry must never break a run
        outcome['status'] = 'failed'
        return outcome
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


async def evaluate_drift(*, records: list[dict[str, Any]],
                         input_class: str, actor_name: str,
                         build: str) -> str:
    """Run-end drift evaluation: fingerprint -> breaches -> report.
    Returns a short status string for the run log: 'ok',
    'below-min-sample', 'reported', 'dormant' or 'failed'.
    NEVER raises."""
    try:
        shape = load_expected_shape()
        fingerprint = compute_fingerprint(records, shape)
        if fingerprint['n'] < int(shape.get('minSample') or 30):
            return 'below-min-sample'
        breaches = find_breaches(fingerprint, shape, input_class)
        if not breaches:
            return 'ok'
        outcome = await report_drift(
            actor_name=actor_name, build=build, breaches=breaches,
        )
        return str(outcome.get('status') or 'failed')
    except Exception:  # noqa: BLE001 - telemetry must never break a run
        return 'failed'
