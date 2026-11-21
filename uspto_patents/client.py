"""USPTO Patent Search client.

Data source: USPTO Patent Public Search (ppubs.uspto.gov) — the
USPTO's own public search over US patent grants and published US
patent applications. The Actor uses the same JSON API the public
search page itself uses, through the anonymous session the site
issues to any visitor:

    GET  /pubwebapp/                          (the search page)
    POST /api/users/me/session                (anonymous session;
          response header x-access-token authenticates later calls)
    POST /api/searches/counts                 (validates + enriches
          the query; search rejects the raw query with HTTP 400)
    POST /api/searches/searchWithBeFamily     (one page of results)

No key, no account, no login, no proxy, no CAPTCHA bypass.

Payload facts (verified live 2026-11-21 from the workspace AND
from inside Apify; fixtures in tests/fixtures, ground truth in
BUILD_NOTES):
- The publication window is enforced SERVER-side with a Solr
  filter query on the date_publ field:
  fq = "date_publ:[2026-11-01T00:00:00Z TO 2026-11-30T23:59:59Z]"
  (counts for "machine learning" in Nov 2026: 25,734 vs 5,912,946
  unfiltered; search results verified in-window only). The window
  is ALSO enforced client-side on each record's publication date,
  so the split is exact by construction.
- Sort "date_publ desc" works (the server appends ",id asc");
  "datePublished desc" and friends return HTTP 500 — date_publ is
  the Solr field name (from the web app's own column config).
- Keyword/field fragments in the BRS query string MUST be
  parenthesised: an unparenthesised multi-term fragment breaks
  AND precedence (("machine learning" AND x.as.) matched ~5.7M;
  ("(machine learning)" AND x.as.) matched the true 63,593).
- Field codes (verified alone via counts): assignee ".as."
  matches the recorded assignee name ONLY (an applicant-only
  application is not matched); inventor ".in."; CPC ".cpc."
  matches a full group symbol exactly, and a trailing wildcard
  covers its subgroups ("G06N20/*.cpc." = 928 in Nov 2026 =
  main group 837 + subgroups 91).
- CPC via the fq flattened fields does NOT work (quoted-exact fq
  on cpc_inventive_flattened matched 71 vs the BRS route's 837 —
  those fields are sort artefacts), so CPC lives in the BRS query.
- A filterless search unit uses the query "(a)": the token was
  verified to match the entire Nov 2026 census (USPAT 28,754 +
  US-PGPUB 27,389 = 56,143, identical to the token "us"); an
  empty q is rejected (HTTP 400) and "*" matches nothing.
- Result records carry ~89 fields; the ones this Actor ships are
  whitelisted in PATENT_FIELDS. Notably: inventorsShort is the
  FIRST inventor only, formatted "Last; First" (with " et al."
  appended when there are more inventors — 83 of a 100-record
  sample); assigneeName is null on most fresh applications
  (72 of 100) while applicantName is always present, so the
  shipped assignee falls back to the applicant; cpcInventive-
  Flattened is ";" separated; priorityClaimsDate is sparse on
  fresh applications (2 of 100); there is NO abstract in the
  search payload, and no record field claims one.
- sourceUrl is the official USPTO PDF of the publication
  (image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/
  US<number>) — verified HTTP 200 / application/pdf for Nov 2026
  application and grant publications (Google Patents document
  pages are not used: that index no longer serves new documents).
- Pagination: start offsets page the full filtered set (verified
  to start=25,730 of 25,734); pageCount up to 1,000 is accepted —
  this Actor pages at 100.

Delivered records are built from a field WHITELIST only
(PATENT_FIELDS). Personal-data discipline: inventor and assignee
names are official-register data and ship (the same class as the
trademark-owner field the USPTO Trademark actor ships); the
source carries no contact data, and any email that ever appeared
in a shipped text value is stripped at parse time (TED lesson).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

PPUBS_BASE = 'https://ppubs.uspto.gov'
SHELL_URL = f'{PPUBS_BASE}/pubwebapp/'
SESSION_URL = f'{PPUBS_BASE}/api/users/me/session'
COUNTS_URL = f'{PPUBS_BASE}/api/searches/counts'
SEARCH_URL = f'{PPUBS_BASE}/api/searches/searchWithBeFamily'
PDF_URL = 'https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/US{}'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
)

STATE_KEY = 'uspto-patent-search-state'
STATE_IDS_CAP = 20000

PAGE_SIZE = 100
PAGE_PACE_S = 0.6  # keep the request rate gentle (playbook rule)
MAX_BACKFILL_DAYS = 92
MAX_RESULTS_CAP = 5000
UNIT_PRICE_USD = 0.004
DATABASES = ('USPAT', 'US-PGPUB')

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_CPC_RE = re.compile(r'^[A-HY]\d{2}([A-Z](\d{1,4}(/\d{1,6})?)?)?$')
_GUID_RE = re.compile(r'^US-([A-Z0-9]+)-([A-Z]\d?)$')
_ET_AL_RE = re.compile(r'\s+et al\.?$', re.IGNORECASE)

# The only fields a delivered PATENT record may ever carry (plus
# recordType). Tests assert parsed records never exceed this set.
PATENT_FIELDS = {
    'publicationNumber', 'kindCode', 'patentTitle', 'type',
    'assignee', 'inventors', 'filingDate', 'publicationDate',
    'priorityDate', 'applicationNumber', 'cpcCodes', 'familyId',
    'sourceUrl', 'matchedQueries',
}


def clean_text(value: Any) -> str | None:
    """One shipped text value: markup stripped, entities decoded,
    whitespace collapsed. Returns None for empty/non-text."""
    if not isinstance(value, str):
        return None
    text = _TAG_RE.sub('', value)
    text = html.unescape(text)
    text = _WS_RE.sub(' ', text).strip()
    return text or None


def scrub_emails(text):
    """Remove email addresses from a shipped text value."""
    if not isinstance(text, str) or '@' not in text:
        return text
    cleaned = _EMAIL_RE.sub('', text)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' ,;')
    return cleaned or None


class PatentsError(Exception):
    """A user-facing error (invalid input). Ends the run with a
    SUMMARY + an ERROR record."""


class ApiError(Exception):
    """USPTO Patent Public Search could not be read: an ERROR row,
    and the monitor state is NOT advanced, so the next run
    catches up."""


# --------------------------------------------------------------------------
# Input validation / config resolution
# --------------------------------------------------------------------------

def _extract_store_id(value: Any) -> str | None:
    """resourcePicker input arrives as an id string or an {id/name} object."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ('id', 'name'):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def _extract_string_list(value: Any, label: str) -> list[str]:
    """Normalise a string-list input (also accepting {"value"/"name"}
    objects and a bare string) to clean strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise PatentsError(f'"{label}" must be a list of text values.')
    out: list[str] = []
    for item in value:
        candidate = item
        if isinstance(candidate, dict):
            for key in ('value', 'name', 'code', 'text'):
                inner = candidate.get(key)
                if isinstance(inner, str):
                    candidate = inner
                    break
            else:
                raise PatentsError(
                    f'"{label}" entries must be plain text values.'
                )
        if not isinstance(candidate, str):
            raise PatentsError(f'"{label}" entries must be plain text values.')
        cleaned = candidate.strip()
        if cleaned:
            out.append(cleaned)
    return out


def _parse_iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise PatentsError(f'"{label}" must be an ISO date, e.g. "2026-11-01".')
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise PatentsError(
            f'"{label}" value "{value}" is not a valid ISO date '
            '(expected e.g. "2026-11-01").'
        ) from None


def parse_since_cursor(value: Any) -> tuple[str | None, date | None]:
    """Parse the optional ``publishedSince`` stateless cursor.

    Accepts an ISO date ("2026-11-01") or datetime
    ("2026-11-01T12:00:00Z"). Publication dates are day-granular,
    so the cursor compares at DAY granularity: patents published
    on the cursor's date or later pass (boundary-inclusive).
    Returns (normalized string, date) or (None, None) when absent.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise PatentsError(
            '"Published on/after (publishedSince)" must be an ISO '
            'date or datetime string, e.g. "2026-11-01".'
        )
    text = value.strip()
    if not text:
        return None, None
    try:
        return text, date.fromisoformat(text)
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise PatentsError(
            f'"Published on/after (publishedSince)" value "{text}" '
            'is not a valid ISO date or datetime (expected e.g. '
            '"2026-11-01" or "2026-11-01T12:00:00Z").'
        ) from None
    return text, moment.date()


def resolve_window(actor_input: dict[str, Any],
                   today: date | None = None) -> tuple[date, date]:
    """Resolve the publication-date window [start, end] (inclusive).

    Default: the ``backfillDays`` complete publication days ending
    YESTERDAY (UTC) — a closed window keeps repeat runs over it
    deterministic (the index for a finished day no longer moves);
    dateFrom/dateTo override the window directly. A
    publishedSince cursor raises the start to the cursor's date.
    """
    today = today or datetime.now(timezone.utc).date()
    date_from = actor_input.get('dateFrom')
    date_to = actor_input.get('dateTo')
    if date_from or date_to:
        if not (date_from and date_to):
            raise PatentsError(
                '"Date from" and "Date to" must be given together.'
            )
        start = _parse_iso_date(date_from, 'Date from')
        end = _parse_iso_date(date_to, 'Date to')
        if end < start:
            raise PatentsError('"Date to" is before "Date from".')
        if (end - start).days + 1 > 366:
            raise PatentsError(
                'The date window is too large (maximum 366 days). '
                'Narrow it, or raise the per-run cap and page '
                'through with several runs.'
            )
    else:
        backfill = actor_input.get('backfillDays')
        if backfill is None:
            backfill = 7
        if isinstance(backfill, bool) or not isinstance(backfill, int):
            raise PatentsError('"Publication days back" must be a whole number.')
        if backfill < 1 or backfill > MAX_BACKFILL_DAYS:
            raise PatentsError(
                f'"Publication days back" must be between 1 and '
                f'{MAX_BACKFILL_DAYS}.'
            )
        end = today - timedelta(days=1)
        start = end - timedelta(days=backfill - 1)

    _since_raw, since_date = parse_since_cursor(
        actor_input.get('publishedSince'))
    if since_date is not None and since_date > start:
        start = since_date
    return start, end


def resolve_config(actor_input: dict[str, Any],
                   today: date | None = None) -> dict[str, Any]:
    """Validate raw Actor input and resolve the run configuration."""
    queries: list[str] = []
    for raw in _extract_string_list(actor_input.get('queries'), 'Keyword queries'):
        # Quotes and parentheses would break the BRS query syntax;
        # the search treats multi-word values as keywords either way.
        cleaned = raw.replace('"', ' ').replace('(', ' ').replace(')', ' ')
        cleaned = _WS_RE.sub(' ', cleaned).strip()
        if cleaned:
            if len(cleaned) > 200:
                raise PatentsError('A keyword query is too long (max 200 characters).')
            if cleaned not in queries:
                queries.append(cleaned)
    if len(queries) > 10:
        raise PatentsError('Too many keyword queries: the maximum is 10 per run.')

    assignees = _extract_string_list(actor_input.get('assignees'), 'Assignees')
    if len(assignees) > 10:
        raise PatentsError('Too many assignees: the maximum is 10 per run.')
    inventors = _extract_string_list(actor_input.get('inventors'), 'Inventors')
    if len(inventors) > 10:
        raise PatentsError('Too many inventors: the maximum is 10 per run.')

    cpc_codes: list[str] = []
    for raw in _extract_string_list(actor_input.get('cpcCodes'), 'CPC codes'):
        code = raw.upper().replace(' ', '')
        if not _CPC_RE.match(code):
            raise PatentsError(
                f'CPC code "{raw}" is not a valid CPC symbol '
                '(e.g. "G06N", "G06N20/00", "H04L9/00").'
            )
        if code not in cpc_codes:
            cpc_codes.append(code)

    monitor_only_new = actor_input.get('monitorOnlyNew')
    if monitor_only_new is None:
        monitor_only_new = False
    if not isinstance(monitor_only_new, bool):
        raise PatentsError('"Monitor only new" must be true or false.')

    max_results = actor_input.get('maxResults')
    if max_results is None:
        max_results = 1000
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise PatentsError('"Maximum patents per run" must be a whole number.')
    if max_results < 1 or max_results > MAX_RESULTS_CAP:
        raise PatentsError(
            f'"Maximum patents per run" must be between 1 and '
            f'{MAX_RESULTS_CAP}.'
        )

    max_charge = actor_input.get('maxTotalChargeUsd')
    if max_charge is not None:
        if isinstance(max_charge, bool) or not isinstance(max_charge, (int, float)):
            raise PatentsError('"Maximum total charge" must be a number.')
        max_charge = float(max_charge)
        if max_charge <= 0:
            raise PatentsError('"Maximum total charge" must be above zero.')

    start, end = resolve_window(actor_input, today=today)
    since_raw, _since = parse_since_cursor(actor_input.get('publishedSince'))

    # Search units: one per keyword query; a single keyword-less
    # unit when only filters (or nothing) were given.
    units = queries if queries else [None]

    return {
        'queries': queries,
        'units': units,
        'assignees': assignees,
        'inventors': inventors,
        'cpcCodes': cpc_codes,
        'monitorOnlyNew': monitor_only_new,
        'stateStoreId': _extract_store_id(actor_input.get('monitorStore')),
        'maxResults': max_results,
        'maxTotalChargeUsd': max_charge,
        'windowStart': start,
        'windowEnd': end,
        'publishedSince': since_raw,
    }


def input_charge_cap_count(max_total_charge_usd: float | None,
                           unit_price_usd: float = UNIT_PRICE_USD) -> int | None:
    """How many charged patents an input-level charge cap pays for
    (at the highest, FREE/BRONZE, unit price — a conservative hard
    stop for every tier)."""
    if max_total_charge_usd is None or max_total_charge_usd <= 0:
        return None
    return int(max_total_charge_usd / unit_price_usd + 1e-9)


# --------------------------------------------------------------------------
# Timeout handling (ACTOR_TIMEOUT_AT) — same contract as the siblings
# --------------------------------------------------------------------------

def timeout_deadline() -> float | None:
    raw = (os.environ.get('ACTOR_TIMEOUT_AT') or '').strip()
    if not raw:
        return None
    try:
        value = float(raw)
        if value > 1e12:  # milliseconds
            return value / 1000.0
        if value > 1e9:  # seconds
            return value
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def seconds_remaining(deadline: float | None,
                      now: float | None = None) -> float | None:
    if deadline is None:
        return None
    import time as _time

    return deadline - (now if now is not None else _time.time())


# --------------------------------------------------------------------------
# Query construction (BRS fragments + Solr fq window)
# --------------------------------------------------------------------------

def _clean_fragment_text(value: str) -> str:
    """Text safe to embed in a BRS fragment: no quotes/parens."""
    cleaned = value.replace('"', ' ').replace('(', ' ').replace(')', ' ')
    return _WS_RE.sub(' ', cleaned).strip()


def assignee_fragment(name: str) -> str:
    """One assignee as a BRS phrase fragment: "Name".as. — the
    .as. field matches the recorded assignee name (verified)."""
    return f'"{_clean_fragment_text(name)}".as.'


def inventor_fragment(name: str) -> str:
    """One inventor as BRS .in. fragments. The index stores
    inventors surname-first ("Goodenough John"), so a multi-word
    name is tried as given AND surname-first; a single word is a
    bare surname term."""
    cleaned = _clean_fragment_text(name)
    words = cleaned.split(' ')
    if len(words) <= 1:
        return f'{cleaned}.in.'
    as_given = f'"{cleaned}".in.'
    reversed_name = ' '.join([words[-1]] + words[:-1])
    if reversed_name == cleaned:
        return as_given
    return f'({as_given} OR "{reversed_name}".in.)'


def cpc_fragment(code: str) -> str:
    """One CPC symbol as a BRS .cpc. fragment with subgroup
    coverage: a main group ("G06N20/00") becomes "G06N20/*.cpc."
    (main group + every subgroup), any other full symbol becomes
    "SYM*.cpc." (itself + deeper subgroups), and a group-less
    symbol ("G06N") becomes "G06N*.cpc." (the whole subclass).
    Verified: "G06N20/*.cpc." = 928 in Nov 2026 vs 837 for the
    exact main group alone."""
    if '/' in code:
        stem, group = code.split('/', 1)
        if group == '00':
            return f'{stem}/*.cpc.'
        return f'{code}*.cpc.'
    return f'{code}*.cpc.'


def build_unit_query(config: dict[str, Any], keyword: str | None) -> str:
    """The BRS query string for one search unit. Every fragment is
    parenthesised (an unparenthesised multi-term fragment breaks
    AND precedence — verified) and fragments combine with AND;
    values within one field combine with OR. A unit with no
    keyword and no filters uses "(a)" — a token verified to match
    the whole index census; the fq window does the real work."""
    fragments: list[str] = []
    if keyword:
        fragments.append(f'({_clean_fragment_text(keyword)})')
    if config['assignees']:
        inner = ' OR '.join(assignee_fragment(a) for a in config['assignees'])
        fragments.append(f'({inner})')
    if config['inventors']:
        inner = ' OR '.join(inventor_fragment(i) for i in config['inventors'])
        fragments.append(f'({inner})')
    if config['cpcCodes']:
        inner = ' OR '.join(cpc_fragment(c) for c in config['cpcCodes'])
        fragments.append(f'({inner})')
    if not fragments:
        return '(a)'
    return ' AND '.join(fragments)


def build_fq(config: dict[str, Any]) -> str:
    """The Solr filter query that enforces the publication window
    server-side on the date_publ field (verified)."""
    start: date = config['windowStart']
    end: date = config['windowEnd']
    return (
        f'date_publ:[{start.isoformat()}T00:00:00Z TO '
        f'{end.isoformat()}T23:59:59Z]'
    )


def build_query_object(case_id: int, q: str, fq: str) -> dict[str, Any]:
    """The query object POSTed to /api/searches/counts. The counts
    response (the server-enriched query) is what search accepts —
    the raw object is rejected with HTTP 400 (verified)."""
    return {
        'anchorDocIds': [], 'querySource': 'search', 'caseId': case_id,
        'facetFilters': [], 'hl_snippets': '3', 'op': 'OR', 'q': q,
        'excludeResultsAfter': None, 'queryName': q, 'highlights': '1',
        'qt': 'brs', 'spellCheck': False, 'viewName': 'grid',
        'plurals': True, 'britishEquivalents': True,
        'databaseFilters': [
            {'databaseName': name, 'countryCodes': []} for name in DATABASES
        ],
        'searchType': 1, 'cNumber': None, 'ignorePersist': True,
        'userEnteredQuery': q, 'extSearchQueryType': None, 'fq': fq,
    }


def build_search_body(enriched_query: dict[str, Any], start: int,
                      page_count: int = PAGE_SIZE) -> dict[str, Any]:
    """The searchWithBeFamily body for one page of an enriched query."""
    return {
        'start': start, 'pageCount': page_count, 'sort': 'date_publ desc',
        'docFamilyFiltering': 'noFiltering', 'searchType': 1,
        'familyIdEnglishOnly': None, 'familyIdFirstPreferred': None,
        'familyIdSecondPreferred': None, 'familyIdThirdPreferred': None,
        'showDocPerFamilyPref': None, 'queryId': 0, 'tagDocSearch': False,
        'query': enriched_query,
    }


# --------------------------------------------------------------------------
# Search transport (anonymous session, gentle pace, bounded retries)
# --------------------------------------------------------------------------

def make_http_client() -> httpx.AsyncClient:
    """Shared HTTP client: a declared browser identity, plain HTTP,
    no proxy configuration, no cookie persistence."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': SHELL_URL,
        },
        follow_redirects=True,
    )


class PpubsClient:
    """Session-holding ppubs client. One anonymous session per
    run; on an HTTP 401 (expired token) the session is re-opened
    once and the current unit's counts are re-run, because the
    enriched query embeds the session's caseId."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.token: str | None = None
        self.case_id: int | None = None
        self._last_q: str | None = None
        self._last_fq: str | None = None
        self._last_enriched: dict[str, Any] | None = None

    async def _post(self, url: str, *, json_body: Any = None,
                    content: str | None = None,
                    content_type: str | None = None) -> httpx.Response:
        headers: dict[str, str] = {}
        if self.token:
            headers['x-access-token'] = self.token
        if content_type:
            headers['Content-Type'] = content_type
        backoffs = (2.0, 6.0, 15.0)
        last_error: str | None = None
        for attempt in range(4):
            try:
                resp = await self.http.post(
                    url, json=json_body, content=content, headers=headers,
                )
            except httpx.RequestError as exc:
                last_error = f'USPTO Patent Public Search could not be reached ({exc})'
                resp = None
            if resp is not None:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = (
                        f'USPTO Patent Public Search answered '
                        f'HTTP {resp.status_code}.'
                    )
                    if attempt < 3:
                        await asyncio.sleep(
                            backoffs[attempt] + random.uniform(0, 0.75))
                    continue
                return resp
            if attempt < 3:
                await asyncio.sleep(backoffs[attempt] + random.uniform(0, 0.75))
        raise ApiError(last_error or 'USPTO Patent Public Search could not be read.')

    async def open_session(self) -> None:
        """Open the anonymous session the search page issues to any
        visitor (no account, no key). Raises ApiError on failure."""
        try:
            await self.http.get(SHELL_URL)
        except httpx.RequestError:
            pass  # the shell GET is a warm-up; the session POST decides
        resp = await self._post(
            SESSION_URL, content='-1', content_type='text/plain')
        if resp.status_code != 200:
            raise ApiError(
                f'USPTO Patent Public Search session failed '
                f'(HTTP {resp.status_code}).'
            )
        token = resp.headers.get('x-access-token')
        try:
            case_id = resp.json()['userCase']['caseId']
        except Exception:  # noqa: BLE001
            case_id = None
        if not token or not isinstance(case_id, int):
            raise ApiError(
                'USPTO Patent Public Search session returned no '
                'usable token/case.'
            )
        self.token = token
        self.case_id = case_id

    async def counts(self, q: str, fq: str) -> tuple[dict[str, Any], int]:
        """Run counts for one unit: returns (enriched query,
        numResults). The enriched query is cached for page fetches
        and for the one re-auth recovery."""
        if self.token is None:
            await self.open_session()
        query = build_query_object(self.case_id, q, fq)
        resp = await self._post(COUNTS_URL, json_body=query)
        if resp.status_code == 401:
            # Expired token: re-open the session and retry once.
            self.token = None
            await self.open_session()
            query = build_query_object(self.case_id, q, fq)
            resp = await self._post(COUNTS_URL, json_body=query)
        if resp.status_code != 200:
            raise ApiError(
                f'USPTO Patent Public Search counts failed '
                f'(HTTP {resp.status_code}).'
            )
        try:
            enriched = resp.json()
        except Exception:  # noqa: BLE001
            enriched = None
        if not isinstance(enriched, dict) or not isinstance(
            enriched.get('numResults'), int
        ):
            raise ApiError(
                'USPTO Patent Public Search returned unreadable counts data.'
            )
        self._last_q, self._last_fq, self._last_enriched = q, fq, enriched
        return enriched, enriched['numResults']

    async def search_page(self, start: int) -> dict[str, Any]:
        """One page of results for the unit primed by the last
        counts() call. Raises ApiError on any transport or service
        failure."""
        if self._last_enriched is None:
            raise ApiError('USPTO Patent Public Search: no active search unit.')
        body = build_search_body(self._last_enriched, start)
        resp = await self._post(SEARCH_URL, json_body=body)
        if resp.status_code == 401 and self._last_q is not None:
            # Token expired mid-unit: re-auth, re-counts (the
            # enriched query embeds the old caseId), retry once.
            self.token = None
            await self.open_session()
            await self.counts(self._last_q, self._last_fq)
            body = build_search_body(self._last_enriched, start)
            resp = await self._post(SEARCH_URL, json_body=body)
        if resp.status_code != 200:
            raise ApiError(
                f'USPTO Patent Public Search search failed '
                f'(HTTP {resp.status_code}).'
            )
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = None
        if not isinstance(payload, dict) or not isinstance(
            payload.get('patents'), list
        ):
            raise ApiError(
                'USPTO Patent Public Search returned unreadable search data.'
            )
        return payload


def iter_result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """All raw result records of a search page payload."""
    items = payload.get('patents') or []
    return [item for item in items if isinstance(item, dict)]


def page_total(payload: dict[str, Any]) -> int:
    """numFound of a search page payload (the filtered total)."""
    total = payload.get('numFound')
    return int(total) if isinstance(total, (int, float)) else 0


# --------------------------------------------------------------------------
# Result parsing (raw result record -> whitelisted record)
# --------------------------------------------------------------------------

def _clean_date(value: Any) -> str | None:
    """An ISO date string from an ISO datetime, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()[:10]
    if not _DATE_RE.match(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _first_text(value: Any) -> str | None:
    """First usable entry of a source list field (or the value
    itself when it is a plain string)."""
    if isinstance(value, list):
        for item in value:
            text = clean_text(item)
            if text:
                return text
        return None
    return clean_text(value)


def parse_inventors(value: Any) -> list[str] | None:
    """inventorsShort -> the inventors list. The search payload
    names the FIRST inventor only, formatted "Last; First" (with
    " et al." appended when the patent has more inventors —
    verified on a 100-record census: exactly one ";" in 100/100).
    The shipped entry is the name in natural "First Last" order."""
    text = clean_text(value)
    if not text:
        return None
    text = _ET_AL_RE.sub('', text).strip()
    parts = [part.strip() for part in text.split(';') if part.strip()]
    if len(parts) >= 2:
        name = f'{parts[1]} {parts[0]}'
    elif parts:
        name = parts[0]
    else:
        return None
    name = scrub_emails(name)
    return [name] if name else None


def parse_result(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build one whitelisted PATENT record (minus recordType) from
    a raw ppubs result record. Returns None without a usable
    publication guid (such results cannot be deduped or
    monitored)."""
    if not isinstance(item, dict):
        return None
    guid = clean_text(item.get('guid'))
    if not guid:
        return None
    match = _GUID_RE.match(guid)
    if not match:
        return None
    doc_number, guid_kind = match.group(1), match.group(2)
    pubnum = f'US{doc_number}{guid_kind}'

    kind_raw = item.get('kindCode')
    if isinstance(kind_raw, list):
        kind_raw = kind_raw[0] if kind_raw else None
    kind_code = clean_text(kind_raw) or guid_kind

    raw_type = clean_text(item.get('type'))
    if raw_type == 'USPAT':
        record_type = 'grant'
    elif raw_type == 'US-PGPUB':
        record_type = 'application'
    elif kind_code.startswith('A'):
        record_type = 'application'
    else:
        record_type = 'grant'

    # Assignee: the recorded assignee; for the many fresh
    # applications that carry none, the applicant (documented in
    # the listing copy — the field is "assignee or applicant as
    # recorded").
    assignee = _first_text(item.get('assigneeName'))
    if assignee is None:
        assignee = _first_text(item.get('applicantName'))
    assignee = scrub_emails(assignee)

    title = scrub_emails(clean_text(item.get('inventionTitle')))

    filing_dates = item.get('applicationFilingDate')
    filing_date = _clean_date(
        filing_dates[0] if isinstance(filing_dates, list) and filing_dates
        else filing_dates
    )
    priority_dates = [
        d for d in (
            _clean_date(v) for v in (item.get('priorityClaimsDate') or [])
            if isinstance(v, str)
        ) if d
    ] if isinstance(item.get('priorityClaimsDate'), list) else []

    cpc_raw = clean_text(item.get('cpcInventiveFlattened'))
    cpc_codes = (
        [part.strip() for part in cpc_raw.split(';') if part.strip()]
        if cpc_raw else []
    )

    family_raw = item.get('familyIdentifierCur')
    family_id: str | None = None
    if isinstance(family_raw, (int, float)) and family_raw:
        family_id = str(int(family_raw))
    else:
        family_id = clean_text(item.get('familyIdentifierCurStr'))

    record = {
        'publicationNumber': pubnum,
        'kindCode': kind_code,
        'patentTitle': title,
        'type': record_type,
        'assignee': assignee,
        'inventors': parse_inventors(item.get('inventorsShort')),
        'filingDate': filing_date,
        'publicationDate': _clean_date(item.get('datePublished')),
        'priorityDate': min(priority_dates) if priority_dates else None,
        'applicationNumber': clean_text(item.get('applicationNumber')),
        'cpcCodes': cpc_codes or None,
        'familyId': family_id,
        'sourceUrl': PDF_URL.format(doc_number),
    }
    return {k: v for k, v in record.items() if v is not None}


def in_window(record: dict[str, Any], config: dict[str, Any]) -> bool:
    """Client-side window check on publicationDate (inclusive).
    The fq window is server-side exact too (verified), so this
    only ever trims anomalies — and it is what makes the
    publishedSince cursor split exact by construction."""
    pub = record.get('publicationDate')
    if pub is None:
        return False
    try:
        day = date.fromisoformat(pub)
    except ValueError:
        return False
    return config['windowStart'] <= day <= config['windowEnd']


def dedupe_within_run(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First occurrence per publicationNumber, order preserved."""
    seen: set[str] = set()
    unique = []
    for record in records:
        if record['publicationNumber'] in seen:
            continue
        seen.add(record['publicationNumber'])
        unique.append(record)
    return unique


# --------------------------------------------------------------------------
# Delta state (seen publication numbers), user-supplied KV store
# --------------------------------------------------------------------------

def merge_seen_ids(previous_ids: list[str], remember_ids: list[str],
                   cap: int = STATE_IDS_CAP) -> list[str]:
    """Fold newly remembered ids into the seen list; the oldest
    fall off the front when the cap is exceeded."""
    merged: list[str] = []
    seen: set[str] = set()
    for pid in list(previous_ids) + list(remember_ids):
        if pid not in seen:
            seen.add(pid)
            merged.append(pid)
    if len(merged) > cap:
        merged = merged[-cap:]
    return merged


async def load_state(store: Any) -> dict[str, Any]:
    """Read the monitor state. ``existed`` distinguishes "no state
    record yet" (first sight — where monitorOnlyNew seeding
    applies) from an existing record."""
    if store is None:
        return {'patentIds': [], 'existed': False, 'runs': 0}
    raw = await store.get_value(STATE_KEY)
    if isinstance(raw, dict):
        ids = raw.get('patentIds')
        if isinstance(ids, list):
            try:
                runs = int(raw.get('runs') or 0)
            except (TypeError, ValueError):
                runs = 0
            return {
                'patentIds': [str(i) for i in ids],
                'existed': True,
                'runs': runs,
            }
    return {'patentIds': [], 'existed': False, 'runs': 0}


async def save_state(store: Any, state: dict[str, Any], run_count: int) -> None:
    await store.set_value(STATE_KEY, {
        'patentIds': state.get('patentIds') or [],
        'updatedAt': datetime.now(timezone.utc).isoformat(),
        'runs': run_count,
    })
