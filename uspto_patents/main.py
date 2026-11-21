"""USPTO Patent Search — New Patent Alerts — Apify Actor.

Searches USPTO Patent Public Search (the USPTO's own public
index of US patent grants and published US patent applications)
through the same JSON API the public search page uses, and
delivers structured records in three modes:

- Corpus (default): a run delivers the current matching set for
  its publication window — the backfill is the first-run sale.
- Monitoring mode: with a user-supplied key-value store
  selected, scheduled runs deliver only patents not seen before
  (new publications for the saved searches), deduped across runs.
- Cursor mode: an optional publishedSince input the caller holds
  in their own workflow narrows the window statelessly.

Reliability contract (Magnova playbook):
- If USPTO Patent Public Search cannot be read at all, the run
  writes one ERROR record plus the SUMMARY and ends normally —
  and the monitor state is NOT advanced, so the next run catches
  up. A broken source never looks like "no new patents". If a
  search breaks mid-stream, the patents already delivered stand,
  the rest stay unseen, and the SUMMARY + an ERROR row say so.
- Only delivered PATENT records are charged (event
  "patent-fetched"). ERROR rows, duplicates, out-of-window
  results, silently seeded patents and the SUMMARY are never
  charged.
- No named storages. Delta state lives in an optional
  user-supplied key-value store (resourcePicker input), opened
  by id; anything wrong with it disables delta state for the
  run instead of crashing it.
- Failure self-reporting (see reporter.py): a run that fails
  outright, whose source check broke, or where every delivery
  attempt errored sends ONE stripped report to the Magnova
  alerts channel. Fail-silent, dormant without its credential.
- Data-drift telemetry (see drift.py): at run end the delivered
  records are fingerprinted (field population rates only, never
  values) and compared against the claims table shipped in the
  build; a field far outside its claimed range reports through
  the same channel. Also fail-silent.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from apify import Actor

from .client import (
    PAGE_PACE_S,
    ApiError,
    PatentsError,
    PpubsClient,
    build_fq,
    build_unit_query,
    in_window,
    input_charge_cap_count,
    iter_result_items,
    load_state,
    make_http_client,
    merge_seen_ids,
    page_total,
    parse_result,
    resolve_config,
    save_state,
    seconds_remaining,
    timeout_deadline,
)
from .drift import evaluate_drift
from .reporter import actor_build_label, report_failure

ACTOR_NAME = 'uspto-patent-search'
CHARGE_EVENT = 'patent-fetched'
SOURCE_LABEL = 'uspto-ppubs'
TIMEOUT_RESERVE_S = 45.0  # stop early enough to write the SUMMARY

BASE_NOTE = (
    'Patent publications from USPTO Patent Public Search — the '
    'USPTO\'s official index of US patent grants and published '
    'US patent applications — read through the public search '
    'API, logged out, with no API key. US coverage only. Records '
    'carry register data only: publication number, title, the '
    'recorded assignee (or the applicant where no assignee is '
    'recorded), the first-listed inventor, filing / publication '
    '/ priority dates, application number, CPC classifications '
    'and USPTO\'s family ID, plus a link to the official USPTO '
    'PDF of the publication. The search payload carries no '
    'abstract, and none is shipped. A record\'s publication '
    'window is enforced on its publication date, '
    'boundary-inclusive.'
)


async def _set_status(message: str) -> None:
    try:
        await Actor.set_status_message(message)
    except Exception:  # noqa: BLE001 - status is cosmetic, never fail on it
        pass


class _Delivery:
    """Where emitted records go. Batch mode: the default dataset.
    Server mode: an in-memory list the HTTP handler returns in the
    response; PATENT records delivered there are counted here and
    charged once, after the response is built."""

    def __init__(self) -> None:
        self.collect: list[dict[str, Any]] | None = None
        self.charged = 0


_DELIVERY = _Delivery()


async def _emit(record: dict[str, Any]) -> None:
    """Emit an uncharged record (ERROR rows): dataset in batch
    mode, the response list in server mode."""
    if _DELIVERY.collect is not None:
        _DELIVERY.collect.append(record)
    else:
        await Actor.push_data(record)


async def _emit_summary(summary: dict[str, Any]) -> None:
    """Emit the SUMMARY record. In server mode the handler returns
    the summary itself, so nothing is emitted here."""
    if _DELIVERY.collect is not None:
        return
    await Actor.push_data(summary)


async def _push_patent(record: dict[str, Any]) -> None:
    """Push one PATENT record and charge the pay-per-event for it."""
    if _DELIVERY.collect is not None:
        _DELIVERY.collect.append(record)
        _DELIVERY.charged += 1
        return
    try:
        await Actor.push_data(record, charged_event_name=CHARGE_EVENT)
    except TypeError:
        # Older SDK without charged_event_name on push_data: push, then charge.
        await Actor.push_data(record)
        await Actor.charge(CHARGE_EVENT, count=1)


def _charge_limit_reached(delivered: int, input_cap_count: int | None) -> bool:
    """True when no further patent can be charged for in this run
    (input-level cap, or the run-level max charge the SDK enforces
    silently — the loop must ask the charging manager explicitly)."""
    if input_cap_count is not None and delivered >= input_cap_count:
        return True
    try:
        manager = Actor.get_charging_manager()
        remaining = manager.calculate_max_event_charge_count_within_limit(CHARGE_EVENT)
    except Exception:  # noqa: BLE001 - introspection must never break a run
        return False
    return remaining is not None and remaining <= 0


async def _open_state_store(store_id: str | None):
    """Open the user-supplied KV store by id. Fail-soft: None on any problem."""
    if not store_id:
        return None
    try:
        store = await Actor.open_key_value_store(id=store_id)
    except Exception as exc:  # noqa: BLE001
        Actor.log.warning(
            'Could not open the monitor store (%s); delta state is OFF '
            'for this run. All fetched patents will be delivered as a '
            'baseline.', exc,
        )
        return None
    if store is None:
        Actor.log.warning(
            'Monitor store %s could not be opened; delta state is OFF '
            'for this run.', store_id,
        )
    return store


async def _safe_report(*, run_status: str, error_class: str,
                       message: str, counts: dict[str, int]) -> None:
    """Send the run's one failure self-report. Never raises."""
    try:
        outcome = await report_failure(
            actor_name=ACTOR_NAME,
            build=actor_build_label(),
            run_status=run_status,
            error_class=error_class,
            message=message,
            counts=counts,
        )
        if outcome not in ('dormant',):
            Actor.log.info('Failure self-report: %s', outcome)
    except Exception:  # noqa: BLE001 - reporting must never break a run
        pass


# ------------------------------------------------------------------
# Server mode (Apify Standby / "Server Actors").
#
# When the platform starts the actor as a server, the same search
# core answers single HTTP requests: POST a JSON body with the
# usual input fields (queries, assignees, inventors, CPC codes,
# window, maxResults, maxTotalChargeUsd) and get
# {"records": [...], "summary": {...}} back in the response — no
# run to start, no dataset to poll. Server mode is a snapshot
# lookup: the monitor store / only-new state stays a batch-run
# feature, and one request returns at most SERVER_MAX_RESULTS
# patents. Delivered patents are charged at the same
# 'patent-fetched' price as batch runs, once per request, after
# the response is built. ERROR rows are never charged. Requests
# are serialized per container (the delivery sink is module-level
# state).
# ------------------------------------------------------------------

SERVER_MAX_RESULTS = 100
SERVER_REQUEST_TIMEOUT_S = 240.0
_SERVER_LOCK = asyncio.Lock()


def _is_standby() -> bool:
    meta = str(getattr(Actor.configuration, "meta_origin", "") or "")
    return "STANDBY" in meta.upper()


async def _server_handle(request):  # noqa: ANN001 - aiohttp request
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any bad body is a 400
        return web.json_response(
            {"error": "request body must be a JSON object"}, status=400
        )
    if not isinstance(payload, dict):
        return web.json_response(
            {"error": "request body must be a JSON object"}, status=400
        )

    collected: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    failure: Exception | None = None
    timed_out = False
    async with _SERVER_LOCK:
        _DELIVERY.collect = collected
        _DELIVERY.charged = 0
        try:
            summary = await asyncio.wait_for(
                _run(payload, server_mode=True),
                timeout=SERVER_REQUEST_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as exc:  # noqa: BLE001 - reported in the body
            failure = exc
        finally:
            charged = _DELIVERY.charged
            _DELIVERY.collect = None

    if charged:
        # Charge once, after the work, for exactly the patents
        # being returned. Multi-tenant attribution needs the
        # request's X-Actor-Request-ID header passed through.
        charge_kwargs: dict[str, Any] = {}
        request_id = request.headers.get("x-actor-request-id")
        if request_id:
            charge_kwargs["request_id"] = request_id
        await Actor.charge(
            event_name=CHARGE_EVENT, count=charged, **charge_kwargs
        )

    if failure is not None:
        return web.json_response(
            {
                "error": f"{type(failure).__name__}: {failure}",
                "records": collected,
                "chargedRecords": charged,
            },
            status=500,
        )
    if summary is None:  # timed out mid-search: partial answer
        summary = {
            "recordType": "SUMMARY",
            "mode": "server",
            "timedOut": True,
            "deliveredNew": charged,
            "note": BASE_NOTE,
        }
    else:
        summary = {**summary, "mode": "server", "timedOut": timed_out}
    return web.json_response({"records": collected, "summary": summary})


async def _server_root(request):  # noqa: ANN001 - aiohttp request
    return web.json_response({
        "service": "magnova/uspto-patent-search",
        "mode": "server",
        "usage": (
            "POST a JSON body with the usual input fields (same as "
            "the actor input) and get patent records back in the "
            "response. $0.004 per delivered patent."
        ),
    })


@web.middleware
async def _readiness_middleware(request, handler):  # noqa: ANN001, ANN202
    if "x-apify-container-server-readiness-probe" in request.headers:
        return web.Response(text="ok")
    return await handler(request)


async def _serve() -> None:
    app = web.Application(middlewares=[_readiness_middleware])
    app.router.add_post("/", _server_handle)
    app.router.add_get("/", _server_root)
    runner = web.AppRunner(app)
    await runner.setup()
    port = Actor.configuration.web_server_port
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    Actor.log.info("Server mode: listening on port %s", port)
    await asyncio.Event().wait()


async def main() -> None:
    async with Actor:
        if _is_standby():
            await _serve()
            return
        try:
            await _run()
        except Exception as exc:  # noqa: BLE001 - report, then stay honest
            await _safe_report(
                run_status='FAILED',
                error_class=type(exc).__name__,
                message=str(exc),
                counts={'checksOk': 0, 'checksErrored': 1,
                        'itemsDelivered': 0, 'itemsErrored': 0},
            )
            raise


async def _run(actor_input_override: dict[str, Any] | None = None,
         server_mode: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    actor_input = actor_input_override if actor_input_override is not None else (
        await Actor.get_input() or {})
    now = datetime.now(timezone.utc)

    summary: dict[str, Any] = {
        'recordType': 'SUMMARY',
        'generatedAt': now.isoformat(),
        'matchedTotal': 0,
        'resultsFetched': 0,
        'deliveredNew': 0,
        'duplicatesSkipped': 0,
        'filteredOutDate': 0,
        'datelessSkipped': 0,
        'seededSilently': 0,
        'unparsedSkipped': 0,
        'itemsErrored': 0,
        'errorCount': 0,
        'errors': [],
        'warnings': [],
        'apiError': False,
        'streamComplete': False,
        'truncatedByTimeout': False,
        'stoppedByMaxCharge': False,
        'stoppedByResultCap': False,
        'stateStoreUsed': False,
        'stateExisted': False,
        'monitorOnlyNew': False,
        'searchUnits': 0,
        'windowStart': None,
        'windowEnd': None,
        'durationSec': 0.0,
        'note': BASE_NOTE,
    }

    try:
        config = resolve_config(actor_input)
    except PatentsError as exc:
        Actor.log.error('Invalid input: %s', exc)
        summary['errors'] = [{'stage': 'input', 'error': str(exc)}]
        summary['errorCount'] = 1
        summary['durationSec'] = round(time.monotonic() - started, 2)
        await _emit({
            'recordType': 'ERROR',
            'source': SOURCE_LABEL,
            'message': str(exc),
        })
        await _emit_summary(summary)
        await _set_status(f'Invalid input: {exc}')
        return summary

    if server_mode:
        # Server mode is a snapshot lookup: delta state and
        # only-new monitoring stay batch/scheduled-run features
        # (the caller's store cannot be opened from a shared
        # server container), and one response stays bounded.
        config['stateStoreId'] = None
        config['monitorOnlyNew'] = False
        if config['maxResults'] > SERVER_MAX_RESULTS:
            config['maxResults'] = SERVER_MAX_RESULTS
    summary['monitorOnlyNew'] = config['monitorOnlyNew']
    summary['windowStart'] = config['windowStart'].isoformat()
    summary['windowEnd'] = config['windowEnd'].isoformat()
    summary['searchUnits'] = len(config['units'])
    if config['publishedSince'] is not None:
        summary['publishedSince'] = config['publishedSince']
    input_cap_count = input_charge_cap_count(config['maxTotalChargeUsd'])
    Actor.log.info(
        'USPTO Patent Search: window %s..%s, units=%d, assignees=%s, '
        'inventors=%s, cpc=%s',
        summary['windowStart'], summary['windowEnd'],
        len(config['units']),
        config['assignees'] or '(any)', config['inventors'] or '(any)',
        config['cpcCodes'] or '(any)',
    )

    deadline = timeout_deadline()

    def out_of_time() -> bool:
        remaining = seconds_remaining(deadline)
        return remaining is not None and remaining < TIMEOUT_RESERVE_S

    # Delta state: optional, user-supplied store, fail-soft.
    store = await _open_state_store(config['stateStoreId'])
    state: dict[str, Any] = {'patentIds': [], 'existed': False, 'runs': 0}
    if store is not None:
        try:
            state = await load_state(store)
            summary['stateStoreUsed'] = True
            summary['stateExisted'] = bool(state['existed'])
            Actor.log.info(
                'Delta state loaded: %d patent id(s) remembered '
                '(state record existed: %s).',
                len(state['patentIds']), state['existed'],
            )
        except Exception as exc:  # noqa: BLE001
            Actor.log.warning(
                'Could not read delta state (%s); state is OFF for '
                'this run.', exc,
            )
            store = None
    if not summary['stateStoreUsed']:
        summary['note'] = BASE_NOTE + (
            ' No monitor store was used, so every fetched patent '
            'was delivered as a baseline record and nothing was '
            'remembered. Pick one of your key-value stores in the '
            '"Monitor state store" input and schedule the run to '
            'get only-new patents on later runs.'
        )

    seen_ids = set(state['patentIds']) if summary['stateStoreUsed'] else set()
    seeding = (
        summary['stateStoreUsed'] and not state['existed']
        and config['monitorOnlyNew']
    )
    if seeding:
        Actor.log.info(
            'monitorOnlyNew with a fresh store: this run seeds the '
            'current matches silently; only later patents deliver.',
        )

    api_error_message: str | None = None
    delivered_records: list[dict[str, Any]] = []
    delivered_ids: list[str] = []
    seeded_ids: list[str] = []
    processed_ids: list[str] = []
    fetched_in_run: set[str] = set()
    matched_new_ids: set[str] = set()
    consecutive_push_failures = 0
    stop_all = False
    units_completed = 0

    fq = build_fq(config)
    async with make_http_client() as http_client:
        ppubs = PpubsClient(http_client)
        for keyword in config['units']:
            if stop_all:
                break
            unit_q = build_unit_query(config, keyword)
            try:
                _enriched, unit_total = await ppubs.counts(unit_q, fq)
            except ApiError as exc:
                api_error_message = str(exc)
                summary['apiError'] = True
                summary['errorCount'] += 1
                summary['errors'].append({
                    'source': SOURCE_LABEL, 'stage': 'fetch',
                    'error': api_error_message,
                })
                Actor.log.warning(
                    'USPTO Patent Public Search check failed: %s',
                    api_error_message)
                await _emit({
                    'recordType': 'ERROR',
                    'source': SOURCE_LABEL,
                    'message': api_error_message,
                })
                stop_all = True
                break
            summary['matchedTotal'] += unit_total

            start_offset = 0
            unit_exhausted = False
            while True:
                if out_of_time():
                    summary['truncatedByTimeout'] = True
                    Actor.log.warning(
                        'Approaching the run timeout — stopping the '
                        'fetch early with partial results.'
                    )
                    stop_all = True
                    break
                try:
                    payload = await ppubs.search_page(start_offset)
                except ApiError as exc:
                    api_error_message = str(exc)
                    summary['apiError'] = True
                    summary['errorCount'] += 1
                    summary['errors'].append({
                        'source': SOURCE_LABEL, 'stage': 'fetch',
                        'error': api_error_message,
                    })
                    Actor.log.warning(
                        'USPTO Patent Public Search check failed: %s',
                        api_error_message)
                    await _emit({
                        'recordType': 'ERROR',
                        'source': SOURCE_LABEL,
                        'message': api_error_message,
                    })
                    stop_all = True
                    break

                items = iter_result_items(payload)
                total_here = page_total(payload) or unit_total

                page_records: list[dict[str, Any]] = []
                for item in items:
                    record = parse_result(item)
                    if record is None:
                        summary['unparsedSkipped'] += 1
                        continue
                    page_records.append(record)
                summary['resultsFetched'] += len(page_records)

                for record in page_records:
                    if stop_all:
                        break
                    pubnum = record['publicationNumber']
                    if pubnum in fetched_in_run:
                        # Repeated across pages or units: process once.
                        summary['duplicatesSkipped'] += 1
                        continue
                    fetched_in_run.add(pubnum)
                    processed_ids.append(pubnum)
                    if record.get('publicationDate') is None:
                        summary['datelessSkipped'] += 1
                        continue
                    if not in_window(record, config):
                        summary['filteredOutDate'] += 1
                        continue
                    if summary['stateStoreUsed'] and pubnum in seen_ids:
                        summary['duplicatesSkipped'] += 1
                        continue
                    matched_new_ids.add(pubnum)
                    if seeding:
                        summary['seededSilently'] += 1
                        seeded_ids.append(pubnum)
                        continue
                    if summary['deliveredNew'] >= config['maxResults']:
                        summary['stoppedByResultCap'] = True
                        stop_all = True
                        break
                    if out_of_time():
                        summary['truncatedByTimeout'] = True
                        Actor.log.warning(
                            'Approaching the run timeout — stopping '
                            'delivery early with partial results.'
                        )
                        stop_all = True
                        break
                    if _charge_limit_reached(
                        summary['deliveredNew'], input_cap_count,
                    ):
                        summary['stoppedByMaxCharge'] = True
                        Actor.log.warning(
                            'The run\'s maximum charge is reached — '
                            'stopping early with %d delivered '
                            'patent(s). Raise the maximum charge or '
                            'the per-run cap to receive more.',
                            summary['deliveredNew'],
                        )
                        stop_all = True
                        break

                    delivered = {'recordType': 'PATENT', **record}
                    if keyword:
                        delivered['matchedQueries'] = [keyword]
                    try:
                        await _push_patent(delivered)
                    except Exception as exc:  # noqa: BLE001
                        consecutive_push_failures += 1
                        summary['itemsErrored'] += 1
                        summary['errorCount'] += 1
                        summary['errors'].append({
                            'source': SOURCE_LABEL,
                            'stage': 'push/charge', 'error': str(exc),
                        })
                        Actor.log.warning(
                            'Could not deliver/charge patent: %s', exc,
                        )
                        if consecutive_push_failures >= 3:
                            Actor.log.error(
                                'Three consecutive delivery failures — '
                                'stopping early.',
                            )
                            stop_all = True
                        continue
                    consecutive_push_failures = 0
                    summary['deliveredNew'] += 1
                    delivered_ids.append(pubnum)
                    delivered_records.append(delivered)

                if stop_all:
                    break
                start_offset += len(items)
                if not items or start_offset >= total_here:
                    unit_exhausted = True
                    break
                await asyncio.sleep(PAGE_PACE_S)
            if unit_exhausted:
                units_completed += 1

    summary['streamComplete'] = (
        units_completed == len(config['units']) and not summary['apiError']
    )

    # --- persist delta state -------------------------------------
    # Remember every patent the run PROCESSED — delivered,
    # duplicate, filtered out by the window, or silently seeded —
    # EXCEPT matched-new patents that were not delivered (result
    # cap / max charge / timeout / push failure): those stay
    # unseen so the next run delivers them. Patents on pages never
    # reached are not remembered either. When the source broke
    # before anything was fetched, nothing is remembered (state
    # not advanced).
    if store is not None and summary['stateStoreUsed'] and not (
        summary['apiError'] and summary['resultsFetched'] == 0
    ):
        undelivered = (
            matched_new_ids - set(delivered_ids) - set(seeded_ids)
        )
        remember_ids = [
            pid for pid in processed_ids if pid not in undelivered
        ]
        try:
            merged = merge_seen_ids(state['patentIds'], remember_ids)
            await save_state(
                store, {'patentIds': merged},
                int(state.get('runs') or 0) + 1,
            )
            Actor.log.info(
                'Delta state updated: %d patent id(s) remembered.',
                len(merged),
            )
        except Exception as exc:  # noqa: BLE001
            Actor.log.warning(
                'Could not save delta state (%s); next run may '
                're-deliver these patents.', exc,
            )

    summary['durationSec'] = round(time.monotonic() - started, 2)
    summary['errors'] = summary['errors'][:50]
    summary['warnings'] = summary['warnings'][:20]
    await _emit_summary(summary)
    Actor.log.info(
        'Done: %d matched, %d fetched, %d delivered, %d duplicates, '
        '%d out-of-window, %d dateless, %d seeded, apiError=%s, '
        'streamComplete=%s, %.1fs.',
        summary['matchedTotal'], summary['resultsFetched'],
        summary['deliveredNew'], summary['duplicatesSkipped'],
        summary['filteredOutDate'], summary['datelessSkipped'],
        summary['seededSilently'], summary['apiError'],
        summary['streamComplete'], summary['durationSec'],
    )

    # --- failure self-report (max one per run, fail-silent) --------
    counts = {
        'checksOk': 0 if summary['apiError'] else 1,
        'checksErrored': 1 if summary['apiError'] else 0,
        'itemsDelivered': summary['deliveredNew'],
        'itemsErrored': summary['itemsErrored'],
    }
    if summary['apiError']:
        await _safe_report(
            run_status='SUCCEEDED',
            error_class='ApiCheckError',
            message=api_error_message or 'USPTO Patent Public Search check failed.',
            counts=counts,
        )
    elif (
        summary['deliveredNew'] == 0
        and summary['itemsErrored'] > 0
        and len(matched_new_ids) > 0
        and summary['itemsErrored'] >= len(matched_new_ids)
        and not summary['stoppedByMaxCharge']
        and not summary['truncatedByTimeout']
    ):
        await _safe_report(
            run_status='SUCCEEDED',
            error_class='AllItemsErrored',
            message='Every matched patent failed at delivery/charge time.',
            counts=counts,
        )

    # --- data-drift telemetry (fail-silent, dormant without token) --
    try:
        drift_outcome = await evaluate_drift(
            records=delivered_records,
            input_class='standard',
            actor_name=ACTOR_NAME,
            build=actor_build_label(),
        )
        if drift_outcome not in ('ok', 'below-min-sample', 'dormant'):
            Actor.log.info('Drift telemetry: %s', drift_outcome)
    except Exception:  # noqa: BLE001 - telemetry must never break a run
        pass

    if summary['apiError']:
        await _set_status(
            'USPTO Patent Public Search could not be checked — see '
            'the ERROR and SUMMARY records. Undelivered patents '
            'stay unseen; the next run catches up.'
        )
    elif summary['deliveredNew'] == 0 and summary['errorCount'] > 0:
        await _set_status(
            'Run finished with no new patents delivered — see the '
            'SUMMARY record in the dataset for details.'
        )
    else:
        message = (
            f"USPTO Patent Search: {summary['deliveredNew']} patent(s) "
            'delivered.'
        )
        if summary['stoppedByMaxCharge']:
            message += " Stopped at the run's maximum charge."
        if summary['stoppedByResultCap']:
            message += ' Stopped at the per-run result cap.'
        if not summary['stateStoreUsed']:
            message += ' Baseline run (no monitor store selected).'
        await _set_status(message)
    return summary
