"""Server-mode (Apify Standby) behaviour tests.

These pin the two halves of the server-mode design without
touching the network:

- resolve_config validation is the input-validation boundary
  for server requests too (same input shape as batch runs);
- the module-level delivery sink routes PATENT records to the
  HTTP response list instead of the dataset, and the module
  constants pin the per-request bound.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from uspto_patents import client as C
from uspto_patents import main as M


def test_server_constants():
    assert M.SERVER_MAX_RESULTS == 100
    assert 0 < M.SERVER_REQUEST_TIMEOUT_S <= 300
    assert M.CHARGE_EVENT == 'patent-fetched'


def test_server_input_uses_same_validation():
    cfg = C.resolve_config(
        {'queries': ['battery'], 'backfillDays': 3, 'maxResults': 5},
        today=date(2026, 11, 21),
    )
    assert cfg['units'] == ['battery']
    assert cfg['maxResults'] == 5
    with pytest.raises(C.PatentsError):
        C.resolve_config({'backfillDays': 0}, today=date(2026, 11, 21))


def test_delivery_sink_collects_instead_of_pushing():
    collected: list[dict] = []
    M._DELIVERY.collect = collected
    M._DELIVERY.charged = 0
    try:
        asyncio.run(M._push_patent(
            {'recordType': 'PATENT', 'publicationNumber': 'US1B1'}))
        asyncio.run(M._emit(
            {'recordType': 'ERROR', 'message': 'x'}))
    finally:
        M._DELIVERY.collect = None
    assert collected[0]['publicationNumber'] == 'US1B1'
    assert collected[1]['recordType'] == 'ERROR'
    # Only the PATENT record counted toward the charge.
    assert M._DELIVERY.charged == 1
    M._DELIVERY.charged = 0


def test_summary_not_emitted_in_server_mode():
    collected: list[dict] = []
    M._DELIVERY.collect = collected
    try:
        asyncio.run(M._emit_summary({'recordType': 'SUMMARY'}))
    finally:
        M._DELIVERY.collect = None
    assert collected == []
