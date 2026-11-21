#!/usr/bin/env python3
"""Stage / test / publish the USPTO Patent Search actor (Magnova).

Uses the stored custom.apify credential via surrogate (never touches raw keys).
Adapted from the Product Hunt Launches Monitor sibling deploy script.

Usage:
  deploy.py stage                 create actor (private), upload source, build
  deploy.py build                 re-upload source + rebuild
  deploy.py run '<json-input>' [--max-charge-usd 0.02] [--run-timeout 3600] [--memory 256]
  deploy.py start '<json-input>' [--max-charge-usd X] [--run-timeout S] [--memory MB]
  deploy.py status <runId>
  deploy.py dataset <datasetId> [limit]   -> counts by recordType, saves items
  deploy.py log <runId>           -> print run log (tail)
  deploy.py actor                 -> GET actor summary (public, pricing, readme?)
  deploy.py set-pricing           -> metadata + pricing while staying private
  deploy.py publish               -> full-body PUT: metadata + pricing + isPublic=true
  deploy.py set-env [NAME=VALUE ...] [--no-build] -> set version env vars (alerts token by path + extras)
  deploy.py kvcreate <name>       -> create throwaway key-value store, print id
  deploy.py standby               -> enable standby SINGLE_TENANT + read back
  deploy.py kvget <storeId> <key> [outfile] -> download a KV record to /tmp
  deploy.py kvput <storeId> <key> <jsonfile> -> upload a JSON KV record
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response

BASE = "https://api.apify.com"
REPO = Path("/home/hatch/workspace/uspto-patent-search")
ACTOR_NAME = "uspto-patent-search"
ACTOR_TITLE = "USPTO Patent Search \u2014 New Patent Alerts"
VERSION = "0.0"
STATE_FILE = REPO / ".deploy_state.json"

SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache", "apify_storage",
             "tests", "icons"}
SKIP_FILES = {".gitignore", "deploy.py", ".deploy_state.json", "tasks.py"}

PREFILL_INPUT = {
    "queries": ["machine learning"],
    "backfillDays": 7,
    "maxResults": 250,
}

# The actor-level example input equals the schema prefill (fleet
# rule): unlike the Google-sourced actor, USPTO's index is current,
# so the relative last-7-days window returns live data and no
# pinned window is needed.
EXAMPLE_INPUT = PREFILL_INPUT

SEO_TITLE = "USPTO Patent Search \u2014 New Patent Alerts API"
SEO_DESCRIPTION = (
    "Search USPTO's official US patent index by keyword, assignee, inventor or CPC class \u2014 structured records with the official USPTO PDF, plus only-new patent alerts. $0.004/patent, no start fee."
)
CATEGORIES_PRIMARY = ["BUSINESS", "AUTOMATION", "LEAD_GENERATION"]
CATEGORIES_FALLBACK = ["BUSINESS", "AUTOMATION", "MARKETING"]


def api(method: str, path: str, body=None, raw_body: bytes | None = None,
        content_type: str = "application/json"):
    url = BASE + path
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, method=method, data=data)
    if data:
        req.add_header("Content-Type", content_type)
    add_surrogate_to_request(req, "custom.apify", allowed_hosts=["api.apify.com"])
    try:
        resp = urllib.request.urlopen(req, data=data, timeout=180)
        return read_json_response(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:1200]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e


def api_raw(method: str, path: str) -> bytes:
    url = BASE + path
    req = urllib.request.Request(url, method=method)
    add_surrogate_to_request(req, "custom.apify", allowed_hosts=["api.apify.com"])
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e


def api_text(method: str, path: str) -> str:
    return api_raw(method, path).decode("utf-8", "replace")


def api_put_raw(path: str, payload: bytes, content_type: str):
    url = BASE + path
    req = urllib.request.Request(url, method="PUT", data=payload)
    req.add_header("Content-Type", content_type)
    add_surrogate_to_request(req, "custom.apify", allowed_hosts=["api.apify.com"])
    try:
        resp = urllib.request.urlopen(req, data=payload, timeout=180)
        return read_json_response(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:1200]
        raise RuntimeError(f"PUT {path} -> HTTP {e.code}: {detail}") from e


def save_state(**kw):
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    state.update(kw)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def find_actor():
    res = api("GET", "/v2/actors?limit=100")
    for item in res.get("data", {}).get("items", []):
        if item.get("name") == ACTOR_NAME:
            return item
    return None


def collect_source_files():
    files = []
    for path in sorted(REPO.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.name.startswith("media-generation-"):
            # Icon candidate images awaiting Hamzah's pick — not actor
            # source; their base64 size breaks the version-upload limit.
            continue
        rel = str(path.relative_to(REPO))
        try:
            content = path.read_text(encoding="utf-8")
            fmt = "TEXT"
        except UnicodeDecodeError:
            import base64
            content = base64.b64encode(path.read_bytes()).decode()
            fmt = "BASE64"
        files.append({"name": rel, "format": fmt, "content": content})
    return files


def do_build(actor_id: str):
    print("Triggering build...")
    build = api("POST", f"/v2/actors/{actor_id}/builds?version={VERSION}&useCache=0", {})["data"]
    build_id = build["id"]
    print(f"  build {build_id} started")
    for _ in range(150):
        time.sleep(10)
        b = api("GET", f"/v2/actor-builds/{build_id}")["data"]
        status = b.get("status")
        print(f"  ... build {status}")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            if status != "SUCCEEDED":
                log = ""
                try:
                    log = api_text("GET", f"/v2/actor-builds/{build_id}/log")[-3000:]
                except Exception:
                    pass
                print("BUILD FAILED. Log tail:\n", log)
                sys.exit(1)
            save_state(actor_id=actor_id, build_id=build_id)
            print(f"BUILD SUCCEEDED: {build_id}")
            return build_id
    print("Build polling timed out")
    sys.exit(1)


def cmd_stage():
    actor = find_actor()
    if actor:
        print(f"Actor exists: {actor['id']} ({actor.get('username')}/{actor['name']}) public={actor.get('isPublic')}")
    else:
        print("Creating actor (private)...")
        actor = api("POST", "/v2/actors", {
            "name": ACTOR_NAME,
            "title": ACTOR_TITLE,
            "description": json.loads((REPO / ".actor" / "actor.json").read_text())["description"],
        })["data"]
        print(f"Created: {actor['id']} public={actor.get('isPublic')}")
    actor_id = actor["id"]
    save_state(actor_id=actor_id)
    print("Collecting source files...")
    source_files = collect_source_files()
    total = sum(len(f["content"]) for f in source_files)
    print(f"  {len(source_files)} files, {total} bytes")
    print(f"Upserting version {VERSION}...")
    api("PUT", f"/v2/actors/{actor_id}/versions/{VERSION}", {
        "versionNumber": VERSION,
        "buildTag": "latest",
        "sourceType": "SOURCE_FILES",
        "sourceFiles": source_files,
    })
    print("  version upserted")
    do_build(actor_id)


def cmd_build():
    actor = find_actor()
    if not actor:
        print("Actor not found; run stage first")
        sys.exit(1)
    actor_id = actor["id"]
    source_files = collect_source_files()
    print(f"  {len(source_files)} files")
    api("PUT", f"/v2/actors/{actor_id}/versions/{VERSION}", {
        "versionNumber": VERSION,
        "buildTag": "latest",
        "sourceType": "SOURCE_FILES",
        "sourceFiles": source_files,
    })
    print("  version upserted")
    do_build(actor_id)


def _parse_run_args(argv):
    inp = json.loads(argv[0])
    max_charge = None
    run_timeout = 3600
    memory = 256
    if "--max-charge-usd" in argv:
        max_charge = float(argv[argv.index("--max-charge-usd") + 1])
    if "--run-timeout" in argv:
        run_timeout = int(argv[argv.index("--run-timeout") + 1])
    if "--memory" in argv:
        memory = int(argv[argv.index("--memory") + 1])
    return inp, max_charge, run_timeout, memory


def start_run(actor_id: str, inp: dict, max_charge: float | None,
              run_timeout: int, memory: int = 256):
    path = f"/v2/acts/{actor_id}/runs?build=latest&memory={memory}&timeout={run_timeout}"
    if max_charge is not None:
        path += f"&maxTotalChargeUsd={max_charge}"
    run = api("POST", path, body=None, raw_body=json.dumps(inp).encode(),
              content_type="application/json")["data"]
    return run


def cmd_run(argv):
    inp, max_charge, run_timeout, memory = _parse_run_args(argv)
    actor = find_actor()
    actor_id = actor["id"]
    run = start_run(actor_id, inp, max_charge, run_timeout, memory)
    run_id = run["id"]
    print(f"RUN STARTED: {run_id} status={run.get('status')}")
    t0 = time.time()
    while True:
        time.sleep(10)
        r = api("GET", f"/v2/actor-runs/{run_id}")["data"]
        status = r.get("status")
        elapsed = int(time.time() - t0)
        print(f"  ... run {status} ({elapsed}s)")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        if elapsed > run_timeout + 300:
            print("Run polling timed out")
            break
    summary = {
        "runId": run_id,
        "status": r.get("status"),
        "startedAt": r.get("startedAt"),
        "finishedAt": r.get("finishedAt"),
        "defaultDatasetId": r.get("defaultDatasetId"),
        "defaultKeyValueStoreId": r.get("defaultKeyValueStoreId"),
        "usageTotalUsd": r.get("usageTotalUsd"),
        "chargedEventCounts": r.get("chargedEventCounts"),
        "statusMessage": r.get("statusMessage"),
        "stats": r.get("stats"),
        "options": r.get("options"),
    }
    print("RUN SUMMARY JSON:")
    print(json.dumps(summary, indent=1))
    out = Path(f"/tmp/run_{run_id}.json")
    out.write_text(json.dumps(summary, indent=1))
    print(f"saved: {out}")


def cmd_start(argv):
    inp, max_charge, run_timeout, memory = _parse_run_args(argv)
    actor = find_actor()
    run = start_run(actor["id"], inp, max_charge, run_timeout, memory)
    print(json.dumps({"runId": run["id"], "status": run.get("status")}))


def cmd_status(argv):
    r = api("GET", f"/v2/actor-runs/{argv[0]}")["data"]
    out = {
        "runId": r["id"], "status": r.get("status"),
        "statusMessage": r.get("statusMessage"),
        "startedAt": r.get("startedAt"), "finishedAt": r.get("finishedAt"),
        "defaultDatasetId": r.get("defaultDatasetId"),
        "defaultKeyValueStoreId": r.get("defaultKeyValueStoreId"),
        "usageTotalUsd": r.get("usageTotalUsd"),
        "chargedEventCounts": r.get("chargedEventCounts"),
        "stats": r.get("stats"),
    }
    print(json.dumps(out, indent=1))


def fetch_dataset_items(dataset_id: str, limit: int = 100000):
    items = []
    offset = 0
    page = 1000
    while len(items) < limit:
        res = api("GET", f"/v2/datasets/{dataset_id}/items?clean=true&limit={page}&offset={offset}")
        batch = res if isinstance(res, list) else res.get("data", {}).get("items", [])
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    return items


def cmd_dataset(argv):
    dataset_id = argv[0]
    limit = int(argv[1]) if len(argv) > 1 else 100000
    meta = api("GET", f"/v2/datasets/{dataset_id}")["data"]
    items = fetch_dataset_items(dataset_id, limit)
    from collections import Counter
    kinds = Counter((it.get("recordType") or "?") for it in items)
    print(f"dataset {dataset_id}: itemCount(meta)={meta.get('itemCount')} fetched={len(items)} byType={dict(kinds)}")
    summaries = [it for it in items if it.get("recordType") == "SUMMARY"]
    for s in summaries:
        print("SUMMARY ROW:", json.dumps(s, indent=1)[:3000])
    for kind in ("ERROR", "BLOCKED"):
        rows = [it for it in items if it.get("recordType") == kind]
        for e in rows[:8]:
            print(f"{kind} ROW:", json.dumps(e)[:600])
    out = Path(f"/tmp/dataset_{dataset_id}.json")
    out.write_text(json.dumps(items, indent=1))
    print(f"saved items: {out}")


def cmd_log(argv):
    text = api_text("GET", f"/v2/logs/{argv[0]}")
    print(text[-6000:])


def cmd_actor():
    actor = find_actor()
    if not actor:
        print("not found")
        return
    detail = api("GET", f"/v2/actors/{actor['id']}")["data"]
    keys = ["id", "name", "username", "title", "description", "seoTitle", "seoDescription",
            "categories", "isPublic", "exampleRunInput", "defaultRunOptions", "pricingInfos"]
    out = {k: detail.get(k) for k in keys}
    print(json.dumps(out, indent=1)[:6000])
    readme = detail.get("readme")
    print("readme field present:", bool(readme), "| len:", len(readme) if readme else 0)
    if readme:
        print("readme head:", readme[:300].replace("\n", " "))


def _actor_body(detail, is_public: bool, categories):
    actor_json = json.loads((REPO / ".actor" / "actor.json").read_text())
    ppe = json.loads((REPO / ".actor" / "pay_per_event.json").read_text())
    events = ppe[0]
    existing_pricing = detail.get("pricingInfos") or []
    ppe_records = [p for p in existing_pricing
                   if p.get("pricingModel") == "PAY_PER_EVENT"]
    latest = ppe_records[-1] if ppe_records else {}
    latest_events = ((latest.get("pricingPerEvent") or {})
                     .get("actorChargeEvents"))
    if latest and latest_events == events:
        # No pricing change: echo the live list verbatim (the API's
        # pricing-modifier rule requires an exact prefix match).
        pricing_infos = existing_pricing
    elif latest:
        # Pricing changed: the modifier must start with the existing
        # objects; append the new one with a fresh startedAt.
        from datetime import datetime, timezone
        new_record = {
            "pricingModel": "PAY_PER_EVENT",
            "pricingPerEvent": {"actorChargeEvents": events},
            "startedAt": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"),
        }
        pricing_infos = existing_pricing + [new_record]
    else:
        pricing_infos = [{
            "pricingModel": "PAY_PER_EVENT",
            "pricingPerEvent": {"actorChargeEvents": events},
        }]
    return {
        "title": actor_json["title"],
        "description": actor_json["description"],
        "seoTitle": SEO_TITLE,
        "seoDescription": SEO_DESCRIPTION,
        "categories": categories,
        "defaultRunOptions": {"build": "latest", "memoryMbytes": 256, "timeoutSecs": 3600},
        "isPublic": is_public,
        "exampleRunInput": {
            "body": json.dumps(EXAMPLE_INPUT),
            "contentType": "application/json",
        },
        "pricingInfos": pricing_infos,
    }


def _put_actor_with_categories(actor_id, detail, is_public: bool):
    """PUT the full actor body; if the primary category list is rejected,
    retry with the fallback list (never fail the publish over categories)."""
    try:
        res = api("PUT", f"/v2/actors/{actor_id}",
                  _actor_body(detail, is_public, CATEGORIES_PRIMARY))["data"]
        return res, CATEGORIES_PRIMARY
    except RuntimeError as exc:
        if "categor" not in str(exc).lower():
            raise
        print(f"Primary categories rejected ({str(exc)[:200]}); "
              "retrying with fallback categories.")
        res = api("PUT", f"/v2/actors/{actor_id}",
                  _actor_body(detail, is_public, CATEGORIES_FALLBACK))["data"]
        return res, CATEGORIES_FALLBACK


def cmd_set_pricing():
    """Set metadata + categories + pay-per-event pricing WITHOUT publishing.

    Pricing must be live for the charge-dependent staging gates
    (max-charge proof) while the actor is still private. Aborts if the
    actor is already public.
    """
    actor = find_actor()
    actor_id = actor["id"]
    detail = api("GET", f"/v2/actors/{actor_id}")["data"]
    if detail.get("isPublic"):
        print("ACTOR IS PUBLIC — refusing to touch it from set-pricing")
        sys.exit(1)
    print("Setting metadata + pricing (isPublic stays false)...")
    res, used = _put_actor_with_categories(actor_id, detail, False)
    print("isPublic:", res.get("isPublic"))
    print("categories:", res.get("categories"), "(requested:", used, ")")
    print("pricingInfos:", json.dumps(res.get("pricingInfos"))[:1200])
    save_state(pricing_set=True, categories_used=used)


def cmd_publish():
    """Full-body PUT: metadata + categories + pricing + isPublic=true.

    The README must already be part of the uploaded source (if the PUT
    answers readme-required, re-upload + rebuild + smoke-run first).
    """
    actor = find_actor()
    actor_id = actor["id"]
    detail = api("GET", f"/v2/actors/{actor_id}")["data"]
    if detail.get("isPublic"):
        print("ALREADY PUBLIC — aborting publish to avoid a pricing re-PUT")
        sys.exit(1)
    print("Publishing (single PUT with pricingInfos + isPublic)...")
    res, used = _put_actor_with_categories(actor_id, detail, True)
    print("isPublic:", res.get("isPublic"))
    print("categories:", res.get("categories"), "(requested:", used, ")")
    print("seoTitle:", res.get("seoTitle"))
    print("pricingInfos:", json.dumps(res.get("pricingInfos"))[:1500])
    save_state(published=True, categories_used=used)


def cmd_standby():
    """Enable standby mode (SINGLE_TENANT) + limited permissions via an
    echo full-body PUT: carry every writable field from the current
    record, change only actorStandby / actorPermissionLevel."""
    actor = find_actor()
    actor_id = actor["id"]
    detail = api("GET", f"/v2/actors/{actor_id}")["data"]
    body = {}
    for k in ("title", "description", "seoTitle", "seoDescription", "categories",
              "defaultRunOptions", "exampleRunInput", "pricingInfos", "isPublic"):
        if detail.get(k) is not None:
            body[k] = detail[k]
    body["actorPermissionLevel"] = "LIMITED_PERMISSIONS"
    body["actorStandby"] = {
        "isEnabled": True,
        "disableStandbyFieldsOverride": False,
        "tenancy": "SINGLE_TENANT",
        "maxRequestsPerActorRun": 4,
        "desiredRequestsPerActorRun": 3,
        "idleTimeoutSecs": 300,
        "build": "latest",
        "memoryMbytes": 256,
        "shouldPassActorInput": False,
    }
    res = api("PUT", f"/v2/actors/{actor_id}", body)["data"]
    print("actorPermissionLevel:", res.get("actorPermissionLevel"))
    print("actorStandby:", json.dumps(res.get("actorStandby")))
    print("notice:", res.get("notice"))
    print("isPublic:", res.get("isPublic"))
    save_state(standby_enabled=True)


TOKEN_FILE = Path("/home/hatch/.secrets/magnova_alerts_token")


def cmd_set_env(argv):
    """Set the version's env vars: MAGNOVA_ALERTS_TOKEN (read BY PATH
    from Hamzah's secured token file, in this process only — never
    printed) plus any extra NAME=VALUE pairs passed as arguments
    (used for the staging selftests). Re-run with no extra args to
    reset the version to the token only. A rebuild is required for
    new runs to see changed env vars, so this also triggers one
    unless --no-build is given."""
    actor = find_actor()
    actor_id = actor["id"]
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    env_vars = [{"name": "MAGNOVA_ALERTS_TOKEN", "value": token,
                 "isSecret": True}]
    for pair in argv:
        if pair == "--no-build":
            continue
        name, _, value = pair.partition("=")
        env_vars.append({"name": name, "value": value, "isSecret": False})
    version = api("GET", f"/v2/actors/{actor_id}/versions/{VERSION}")["data"]
    body = {
        "versionNumber": VERSION,
        "buildTag": version.get("buildTag") or "latest",
        "sourceType": version.get("sourceType") or "SOURCE_FILES",
        "sourceFiles": version.get("sourceFiles") or [],
        "envVars": env_vars,
    }
    api("PUT", f"/v2/actors/{actor_id}/versions/{VERSION}", body)
    check = api("GET", f"/v2/actors/{actor_id}/versions/{VERSION}")["data"]
    placed = [
        {"name": v.get("name"), "isSecret": v.get("isSecret"),
         "value": "(masked)" if v.get("isSecret") else v.get("value")}
        for v in (check.get("envVars") or [])
    ]
    print("env vars on version:", json.dumps(placed))
    save_state(env_vars_set=[v["name"] for v in placed])
    if "--no-build" not in argv:
        do_build(actor_id)


def cmd_kvcreate(argv):
    name = argv[0]
    res = api("POST", "/v2/key-value-stores", {"name": name})["data"]
    print(json.dumps({"id": res["id"], "name": res.get("name")}))


def cmd_kvget(argv):
    store_id, key = argv[0], argv[1]
    raw = api_raw("GET", f"/v2/key-value-stores/{store_id}/records/{key}")
    out = Path(argv[2]) if len(argv) > 2 else Path(f"/tmp/kv_{key}.txt")
    out.write_bytes(raw)
    print(f"saved {len(raw)} bytes -> {out}")


def cmd_kvput(argv):
    store_id, key, jsonfile = argv[0], argv[1], argv[2]
    payload = Path(jsonfile).read_bytes()
    res = api_put_raw(f"/v2/key-value-stores/{store_id}/records/{key}",
                      payload, "application/json")
    print(json.dumps(res)[:300])


if __name__ == "__main__":
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "start":
        cmd_start(args)
    elif cmd == "status":
        cmd_status(args)
    elif cmd == "stage":
        cmd_stage()
    elif cmd == "build":
        cmd_build()
    elif cmd == "run":
        cmd_run(args)
    elif cmd == "dataset":
        cmd_dataset(args)
    elif cmd == "log":
        cmd_log(args)
    elif cmd == "actor":
        cmd_actor()
    elif cmd == "publish":
        cmd_publish()
    elif cmd == "set-pricing":
        cmd_set_pricing()
    elif cmd == "standby":
        cmd_standby()
    elif cmd == "set-env":
        cmd_set_env(args)
    elif cmd == "kvcreate":
        cmd_kvcreate(args)
    elif cmd == "kvget":
        cmd_kvget(args)
    elif cmd == "kvput":
        cmd_kvput(args)
    else:
        print(__doc__)
        sys.exit(1)
