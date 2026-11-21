"""Unit tests for the USPTO ppubs client logic.

Network-free: fixtures are real ppubs searchWithBeFamily
payloads captured from the live API on 2026-11-21 (Nov 2026
window), trimmed to a few records per page. The fragment-builder
and body-builder expectations encode the Step-1 prototype
findings: BRS fragments must be parenthesised, the date window
is an fq Solr range on date_publ, and the search request only
accepts the counts-enriched query verbatim.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from uspto_patents import client as C

FIXTURES = Path(__file__).parent / 'fixtures'


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


MIXED = load('ppubs_mixed_page.json')
GRANTS = load('ppubs_grants_page.json')
APPS = load('ppubs_applications_page.json')


# ------------------------- config / window -----------------------

def test_prefill_input_resolves():
    cfg = C.resolve_config({
        'queries': ['machine learning'], 'backfillDays': 7,
        'maxResults': 250,
    }, today=date(2026, 11, 21))
    assert cfg['units'] == ['machine learning']
    assert cfg['windowStart'] == date(2026, 11, 14)
    assert cfg['windowEnd'] == date(2026, 11, 20)
    assert cfg['maxResults'] == 250


def test_explicit_window_overrides_backfill():
    cfg = C.resolve_config({
        'dateFrom': '2026-10-01', 'dateTo': '2026-10-31',
    }, today=date(2026, 11, 21))
    assert cfg['windowStart'] == date(2026, 10, 1)
    assert cfg['windowEnd'] == date(2026, 10, 31)
    # No filters at all -> one filterless unit.
    assert cfg['units'] == [None]


def test_half_window_is_an_error():
    with pytest.raises(C.PatentsError):
        C.resolve_config({'dateFrom': '2026-10-01'},
                         today=date(2026, 11, 21))


def test_published_since_raises_window_start_only():
    cfg = C.resolve_config({
        'dateFrom': '2026-10-01', 'dateTo': '2026-10-31',
        'publishedSince': '2026-10-15',
    }, today=date(2026, 11, 21))
    assert cfg['windowStart'] == date(2026, 10, 15)
    assert cfg['windowEnd'] == date(2026, 10, 31)


def test_bad_inputs_rejected():
    with pytest.raises(C.PatentsError):
        C.resolve_config({'backfillDays': 0}, today=date(2026, 11, 21))
    with pytest.raises(C.PatentsError):
        C.resolve_config({'maxResults': 99999}, today=date(2026, 11, 21))
    with pytest.raises(C.PatentsError):
        C.resolve_config({'queries': [f'q{i}' for i in range(11)]},
                         today=date(2026, 11, 21))
    with pytest.raises(C.PatentsError):
        C.resolve_config({'cpcCodes': ['not a code']},
                         today=date(2026, 11, 21))


# ------------------------- BRS fragments -------------------------

def _cfg(**kw):
    base = {
        'queries': ['machine learning'], 'backfillDays': 7,
        'maxResults': 250,
    }
    base.update(kw)
    return C.resolve_config(base, today=date(2026, 11, 21))


def test_keyword_fragment_is_parenthesised():
    q = C.build_unit_query(_cfg(), 'machine learning')
    assert q == '(machine learning)'


def test_phrase_keyword_quotes_stripped():
    # resolve_config strips quotes/parens from keyword queries
    # (they would break the BRS syntax); multi-word values are
    # keyword searches either way.
    cfg = _cfg(queries=['"solid state battery"'])
    assert cfg['queries'] == ['solid state battery']
    q = C.build_unit_query(cfg, 'solid state battery')
    assert q == '(solid state battery)'


def test_filterless_unit_is_universal_token():
    cfg = C.resolve_config({}, today=date(2026, 11, 21))
    assert C.build_unit_query(cfg, None) == '(a)'
    cfg = C.resolve_config({'assignees': ['Toyota']},
                           today=date(2026, 11, 21))
    assert C.build_unit_query(cfg, None) == '("Toyota".as.)'


def test_assignee_and_inventor_fragments():
    cfg = _cfg(assignees=['Toyota', 'Samsung Electronics'],
               inventors=['Goodenough'])
    q = C.build_unit_query(cfg, 'battery')
    assert '("Toyota".as. OR "Samsung Electronics".as.)' in q
    assert '(Goodenough.in.)' in q
    assert q.startswith('(battery)')


def test_inventor_first_last_also_tried_surname_first():
    assert C.inventor_fragment('Goodenough') == 'Goodenough.in.'
    assert C.inventor_fragment('John Goodenough') == (
        '("John Goodenough".in. OR "Goodenough John".in.)')


def test_cpc_fragments():
    assert C.cpc_fragment('G06N') == 'G06N*.cpc.'
    assert C.cpc_fragment('G06N20/00') == 'G06N20/*.cpc.'
    assert C.cpc_fragment('H04L9/32') == 'H04L9/32*.cpc.'
    assert C.cpc_fragment('G06N20') == 'G06N20*.cpc.'
    assert C.cpc_fragment('B60L') == 'B60L*.cpc.'


def test_window_fq_format():
    cfg = _cfg()
    fq = C.build_fq(cfg)
    assert fq == ('date_publ:[2026-11-14T00:00:00Z TO '
                  '2026-11-20T23:59:59Z]')


def test_query_object_shape():
    qo = C.build_query_object(4242, '(a)', 'date_publ:[x TO y]')
    assert qo['q'] == '(a)'
    assert qo['userEnteredQuery'] == '(a)'
    assert qo['fq'] == 'date_publ:[x TO y]'
    assert qo['databaseFilters'] == [
        {'databaseName': 'USPAT', 'countryCodes': []},
        {'databaseName': 'US-PGPUB', 'countryCodes': []}]
    assert qo['caseId'] == 4242
    assert qo['highlights'] == '1'
    assert qo['hl_snippets'] == '3'


def test_search_body_uses_counts_enrichment_shape():
    enriched = {'q': '(a)', 'anchorDocIds': [{'guid': 'US-1-A1'}],
                'caseId': 9, 'facetFilters': {}}
    body = C.build_search_body(enriched, 200)
    assert body['start'] == 200
    assert body['pageCount'] == 100
    assert body['sort'] == 'date_publ desc'
    assert body['query'] is enriched
    assert body['queryId'] == 0


# ------------------------- parsing -------------------------------

def test_mixed_page_parses():
    items = C.iter_result_items(MIXED)
    assert C.page_total(MIXED) == 25734
    recs = [C.parse_result(i) for i in items]
    assert all(r is not None for r in recs)
    first = recs[0]
    assert first['publicationNumber'] == 'US20260370217A1'
    assert first['kindCode'] == 'A1'
    assert first['publicationType'] == 'application'
    assert first['patentTitle']
    assert first['publicationDate'] == '2026-11-19'
    assert first['filingDate'] == '2024-03-07'
    assert first['applicationNumber'] == '18/598057'
    assert first['assignee'] == 'Quantum Machines'
    assert first['inventors'] == ['Yonatan COHEN']
    assert first['sourceUrl'].endswith('/US20260370217')
    assert first['familyId'] == '1000007776137'
    assert 'G06N10/00' in first['cpcCodes']


def test_grants_page_types():
    recs = [C.parse_result(i) for i in C.iter_result_items(GRANTS)]
    assert recs and all(r['publicationType'] == 'grant' for r in recs)
    first = recs[0]
    assert first['publicationNumber'] == 'US12802069B1'
    assert first['kindCode'] == 'B1'
    assert first['assignee'] == 'Auctane, Inc.'
    assert first['inventors'] == ['Akbar A. Thobhani']


def test_priority_date_is_earliest_claim():
    rec = C.parse_result({
        'guid': 'US-1234567-B2', 'type': 'USPAT',
        'priorityClaimsDate': ['2020-06-15T00:00:00.000Z',
                               '2019-01-01T00:00:00.000Z'],
    })
    assert rec['priorityDate'] == '2019-01-01'


def test_applications_page_types_and_assignee_fallback():
    recs = [C.parse_result(i) for i in C.iter_result_items(APPS)]
    assert recs and all(r['publicationType'] == 'application' for r in recs)
    # At least one fresh application has no recorded assignee and
    # falls back to the applicant name.
    fallback = [r for r in recs if r['assignee']]
    assert fallback


def test_inventors_first_only_et_al_stripped():
    assert C.parse_inventors('Harry; Paul et al.') == ['Paul Harry']
    assert C.parse_inventors('Harry; Paul') == ['Paul Harry']
    assert C.parse_inventors(None) is None
    assert C.parse_inventors('') is None
    assert C.parse_inventors('Solo') == ['Solo']


def test_parse_result_rejects_garbage():
    assert C.parse_result({'guid': ''}) is None
    assert C.parse_result({}) is None
    assert C.parse_result('not a dict') is None


def test_whitelist_keys_only():
    rec = C.parse_result(C.iter_result_items(MIXED)[0])
    assert set(rec) <= C.PATENT_FIELDS
    assert {'publicationNumber', 'sourceUrl', 'publicationDate'} <= set(rec)


def test_in_window_boundaries():
    cfg = _cfg()
    assert C.in_window({'publicationDate': '2026-11-14'}, cfg)
    assert C.in_window({'publicationDate': '2026-11-20'}, cfg)
    assert not C.in_window({'publicationDate': '2026-11-13'}, cfg)
    assert not C.in_window({'publicationDate': '2026-11-21'}, cfg)
    assert not C.in_window({'publicationDate': None}, cfg)


def test_all_fixture_records_in_november_window():
    cfg = C.resolve_config(
        {'dateFrom': '2026-11-01', 'dateTo': '2026-11-30'},
        today=date(2026, 11, 21))
    for payload in (MIXED, GRANTS, APPS):
        for item in C.iter_result_items(payload):
            rec = C.parse_result(item)
            assert rec is not None
            assert C.in_window(rec, cfg), rec['publicationNumber']


# ------------------------- state / misc --------------------------

def test_state_merge_caps_and_keeps_newest():
    merged = C.merge_seen_ids([f'US{i}' for i in range(10)],
                              ['US100', 'US101'], cap=5)
    assert merged == ['US6', 'US7', 'US8', 'US9', 'US100', 'US101'][-5:]


def test_scrub_emails():
    assert C.scrub_emails('mail a@b.co now') == 'mail now'
    assert C.scrub_emails('no mail') == 'no mail'


def test_input_charge_cap_count():
    assert C.input_charge_cap_count(1.0) == 250
    assert C.input_charge_cap_count(None) is None
    assert C.input_charge_cap_count(0) is None
