"""Unit tests for drift telemetry against this Actor's claims
table (uspto_patents/expected_shape.json): fingerprinting,
breach detection with margins and minimum sample, dedupe by
actor+field+build, dormancy without the token, and the guarantee
that reports carry rates and counts only — never record values."""

from __future__ import annotations

import asyncio

import httpx

from uspto_patents.drift import (
    compute_fingerprint,
    drift_title,
    evaluate_drift,
    find_breaches,
    load_expected_shape,
    report_drift,
)
from uspto_patents.reporter import ALERTS_REPO


def make_records(n: int, **overrides) -> list[dict]:
    """n healthy PATENT-shaped records with every claimed field
    populated; ``overrides`` replaces a field's value on all of
    them (e.g. assignee=None)."""
    records = []
    for _ in range(n):
        record = {
            'publicationNumber': 'US12802069B1',
            'kindCode': 'B1',
            'patentTitle': 'Anomaly detection based on ensemble models',
            'publicationType': 'grant',
            'assignee': 'An Assignee Inc.',
            'inventors': ['An Inventor'],
            'filingDate': '2022-02-18',
            'publicationDate': '2026-11-17',
            'priorityDate': '2015-08-31',
            'applicationNumber': '17/123456',
            'cpcCodes': ['G06N20/00'],
            'familyId': '87654321',
            'sourceUrl': ('https://image-ppubs.uspto.gov/dirsearch-public/'
                          'print/downloadPdf/US12802069'),
        }
        record.update(overrides)
        records.append(record)
    return records


def breaches_for(records, shape=None):
    shape = shape or load_expected_shape()
    return find_breaches(compute_fingerprint(records, shape), shape,
                         'standard')


def test_shape_claims_match_listing_promises():
    shape = load_expected_shape()
    fields = shape['fields']
    # Structural fields are promised at 100%.
    for field in ('publicationNumber', 'sourceUrl', 'publicationDate'):
        assert fields[field]['min'] == 1.0
    # Content floors the listing states.
    assert fields['patentTitle']['min'] == 0.95
    assert fields['assignee']['min'] == 0.90
    assert fields['inventors']['min'] == 0.90
    assert fields['cpcCodes']['min'] == 0.85
    # Priority dates are sparse on the freshest applications (2/200
    # in the 2026-11-21 census); the floor guards against total
    # disappearance, not corpus mix.
    assert fields['priorityDate']['min'] == 0.02
    assert shape['minSample'] == 30


def test_healthy_records_no_breach():
    assert breaches_for(make_records(50)) == []


def test_blanked_claimed_field_breaches():
    breaches = breaches_for(make_records(50, assignee=None))
    assert [b['field'] for b in breaches] == ['assignee']
    assert breaches[0]['observed'] == 0.0
    assert breaches[0]['expectedMin'] == 0.90


def test_below_min_sample_never_breaches():
    assert breaches_for(make_records(29, assignee=None)) == []
    assert [b['field'] for b in
            breaches_for(make_records(30, assignee=None))] == ['assignee']


def test_margin_absorbs_small_dips():
    # 46/50 assignees = 92%: above the 90% floor -> no breach.
    records = make_records(50)
    for record in records[:4]:
        record['assignee'] = None
    assert breaches_for(records) == []
    # 38/50 = 76%: 90 - 15 = 75 boundary -> still no breach.
    records = make_records(50)
    for record in records[:12]:
        record['assignee'] = None
    assert breaches_for(records) == []
    # 37/50 = 74%: outside -> breach.
    records = make_records(50)
    for record in records[:13]:
        record['assignee'] = None
    assert [b['field'] for b in breaches_for(records)] == ['assignee']


def test_empty_list_field_counts_unpopulated():
    fingerprint = compute_fingerprint(
        [{'cpcCodes': []}], load_expected_shape())
    assert fingerprint['fields']['cpcCodes']['populated'] == 0
    # An empty string on a claimed scalar field also counts as
    # unpopulated.
    fingerprint = compute_fingerprint(
        [{'patentTitle': ''}], load_expected_shape())
    assert fingerprint['fields']['patentTitle']['populated'] == 0


class _FakeGitHub:
    """Minimal in-memory GitHub issues API for report_drift."""

    def __init__(self, issues=()):
        self.issues = [dict(i) for i in issues]
        self.posts = []

    async def get(self, url, params=None, headers=None, timeout=None):
        return httpx.Response(200, json=self.issues)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith('/issues'):
            number = 100 + len(self.posts)
            self.issues.append({
                'number': number, 'title': json['title'],
                'body': json['body'],
            })
            return httpx.Response(201, json={'number': number})
        return httpx.Response(201, json={})


def _breach(field='assignee', observed=0.0):
    return {
        'field': field, 'kind': 'population', 'expectedMin': 0.90,
        'expectedMax': 1.0, 'margin': 0.15, 'observed': observed,
        'n': 50, 'inputClass': 'standard',
    }


def test_report_drift_creates_issue_with_rates_only():
    fake = _FakeGitHub()
    outcome = asyncio.run(report_drift(
        actor_name='uspto-patent-search', build='0.0.1',
        breaches=[_breach()], token='tok-test', client=fake))
    assert outcome['status'] == 'reported'
    assert outcome['created'] == ['assignee']
    title, body = fake.posts[0][1]['title'], fake.posts[0][1]['body']
    assert title == drift_title('uspto-patent-search', 'assignee')
    assert '0.0%' in body and '90.0%' in body
    assert 'tok-test' not in body
    assert ALERTS_REPO  # channel constant exists


def test_report_drift_dedupes_same_build_by_comment():
    body = (
        'Automated data-drift report\n\n'
        '- Actor: uspto-patent-search\n- Build: 0.0.1\n'
    )
    fake = _FakeGitHub(issues=[{
        'number': 7,
        'title': drift_title('uspto-patent-search', 'assignee'),
        'body': body,
    }])
    outcome = asyncio.run(report_drift(
        actor_name='uspto-patent-search', build='0.0.1',
        breaches=[_breach()], token='tok-test', client=fake))
    assert outcome['commented'] == ['assignee']
    assert outcome['created'] == []
    # A different build gets its own issue (dedupe includes build).
    outcome = asyncio.run(report_drift(
        actor_name='uspto-patent-search', build='0.0.2',
        breaches=[_breach()], token='tok-test', client=fake))
    assert outcome['created'] == ['assignee']


def test_report_drift_dormant_without_token(monkeypatch):
    monkeypatch.delenv('MAGNOVA_ALERTS_TOKEN', raising=False)
    outcome = asyncio.run(report_drift(
        actor_name='uspto-patent-search', build='0.0.1',
        breaches=[_breach()], client=_FakeGitHub()))
    assert outcome['status'] == 'dormant'


def test_evaluate_drift_end_to_end(monkeypatch):
    monkeypatch.delenv('MAGNOVA_ALERTS_TOKEN', raising=False)
    status = asyncio.run(evaluate_drift(
        records=make_records(50, assignee=None), input_class='standard',
        actor_name='uspto-patent-search', build='0.0.1'))
    assert status == 'dormant'  # breach found, no token -> dormant
    status = asyncio.run(evaluate_drift(
        records=make_records(50), input_class='standard',
        actor_name='uspto-patent-search', build='0.0.1'))
    assert status == 'ok'
    status = asyncio.run(evaluate_drift(
        records=make_records(10), input_class='standard',
        actor_name='uspto-patent-search', build='0.0.1'))
    assert status == 'below-min-sample'
