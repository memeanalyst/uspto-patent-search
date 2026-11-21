"""Unit tests for the failure self-report module (reporter.py).

All HTTP is mocked with httpx.MockTransport — no network, no real
token. The GitHub alerts repo is memeanalyst/magnova-alerts.
"""
import asyncio
import json
import os

import httpx
import pytest

from uspto_patents.reporter import (
    build_report,
    report_failure,
    sanitize_message,
)

COUNTS = {'checksOk': 0, 'checksErrored': 1, 'itemsDelivered': 0,
          'itemsErrored': 0}
KW = dict(
    actor_name='uspto-patent-search', build='0.0.1',
    run_status='SUCCEEDED', error_class='ApiCheckError',
    message='Google Patents answered HTTP 503.',
    counts=COUNTS,
)


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url='https://api.github.com',
    )


def test_absent_token_is_dormant_and_silent(monkeypatch):
    monkeypatch.delenv('MAGNOVA_ALERTS_TOKEN', raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={})

    async def go():
        async with make_client(handler) as client:
            return await report_failure(client=client, **KW)

    assert asyncio.run(go()) == 'dormant'
    assert calls == []


def test_creates_issue_when_no_open_match():
    seen = {}

    def handler(request):
        seen[request.method] = request
        if request.method == 'GET':
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        seen['post_body'] = body
        return httpx.Response(201, json={'number': 7})

    async def go():
        async with make_client(handler) as client:
            return await report_failure(token='tok-test', client=client, **KW)

    assert asyncio.run(go()) == 'created'
    assert seen['post_body']['title'] == (
        '[uspto-patent-search] run failure — ApiCheckError'
    )
    assert 'Google Patents answered HTTP 503.' in seen['post_body']['body']
    # The Authorization header carries the token; it is never in the body.
    assert seen['POST'].headers['authorization'] == 'Bearer tok-test'
    assert 'tok-test' not in seen['post_body']['body']
    assert 'tok-test' not in seen['post_body']['title']


def test_dedupes_to_comment_on_open_issue():
    posted = {}

    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[{
                'number': 3,
                'title': '[uspto-patent-search] run failure '
                         '— ApiCheckError',
            }, {
                'number': 4,
                'title': '[other-actor] run failure — ApiCheckError',
            }])
        posted['url'] = str(request.url)
        posted['body'] = json.loads(request.content)
        return httpx.Response(201, json={'id': 99})

    async def go():
        async with make_client(handler) as client:
            return await report_failure(token='tok-test', client=client, **KW)

    assert asyncio.run(go()) == 'commented'
    assert posted['url'].endswith('/issues/3/comments')
    assert posted['body']['body'].startswith('+1 occurrence')


def test_dedupe_ignores_pull_requests():
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[{
                'number': 5,
                'title': '[uspto-patent-search] run failure '
                         '— ApiCheckError',
                'pull_request': {'url': 'https://api.github.com/x'},
            }])
        return httpx.Response(201, json={'number': 6})

    async def go():
        async with make_client(handler) as client:
            return await report_failure(token='tok-test', client=client, **KW)

    assert asyncio.run(go()) == 'created'


def test_post_failure_is_swallowed():
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[])
        return httpx.Response(500, text='boom')

    async def go():
        async with make_client(handler) as client:
            return await report_failure(token='tok-test', client=client, **KW)

    assert asyncio.run(go()) == 'failed'


def test_transport_error_is_swallowed():
    def handler(request):
        raise httpx.ConnectError('down', request=request)

    async def go():
        async with make_client(handler) as client:
            return await report_failure(token='tok-test', client=client, **KW)

    assert asyncio.run(go()) == 'failed'


def test_dedupe_get_failure_still_creates():
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(403, text='rate limited')
        return httpx.Response(201, json={'number': 8})

    async def go():
        async with make_client(handler) as client:
            return await report_failure(token='tok-test', client=client, **KW)

    assert asyncio.run(go()) == 'created'


def test_report_body_is_stripped():
    title, body = build_report(**KW)
    # Fixed schema only: no room for customer inputs, keywords or data.
    for expected in ('Actor: uspto-patent-search',
                     'Build: 0.0.1', 'Run status: SUCCEEDED',
                     'Error class: ApiCheckError',
                     'Checks errored: 1'):
        assert expected in body
    # No credential material and no customer-data values can appear:
    # the body is built only from the fixed schema above. (The word
    # "keywords" itself appears only inside the closing disclaimer
    # sentence that says they are never included.)
    for forbidden in ('Bearer', 'tok-', 'ghp_', 'github_pat_',
                      'MAGNOVA_ALERTS_TOKEN'):
        assert forbidden not in body
        assert forbidden not in title


def test_sanitize_message():
    assert sanitize_message('a\n\n  b\t c') == 'a b c'
    long = sanitize_message('x' * 1000)
    assert len(long) == 300
    leaked = sanitize_message('token ghp_abcdefghij1234567890 leaked')
    assert 'ghp_' not in leaked
    assert '[redacted]' in leaked


def test_env_token_used_when_present(monkeypatch):
    monkeypatch.setenv('MAGNOVA_ALERTS_TOKEN', 'tok-from-env')

    def handler(request):
        if request.method == 'GET':
            assert request.headers['authorization'] == 'Bearer tok-from-env'
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={'number': 1})

    async def go():
        async with make_client(handler) as client:
            return await report_failure(client=client, **KW)

    assert asyncio.run(go()) == 'created'
