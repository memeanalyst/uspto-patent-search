#!/usr/bin/env python3
"""Deploy helper for magnova/uspto-patent-search.

Subcommands:
  create     Create the actor record (name + title) if absent.
  stage      Create-or-find actor, push a build from this folder
             (private), set pricing + standby, apply default run
             options, attach the prefill as the example input.
  set-pricing  PUT fresh pricing infos on the actor (creates a
             new pricing-info entry; never edits in place).
  set-env    Set MAGNOVA_ALERTS_TOKEN from the stored fleet
             alerts credential (token value is never printed).
  runs       List recent runs (own account).
  run-status <runId>  Print one run's status incl. charge counts.
  run-input  <runId>  Print one run's stored input.
  dataset    <datasetId> [limit]  Print dataset items as JSON.
  publish    Publish the staged actor: PUT the public listing
             fields (title, description, SEO, categories, pricing)
             with isPublic=true, keeping the staged build/pricing.

Credentials: the fleet Magnova Apify token via the standard
dynamic_credentials loader (skill-creator bin on sys.path).
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import get_credential  # noqa: E402

REPO = Path("/home/hatch/workspace/uspto-patent-search")
API = "https://api.apify.com/v2"
ACTOR_NAME = "uspto-patent-search"
ACTOR_TITLE = "USPTO Patent Search — New Patent Alerts"
TOKEN_FILE = Path("/home/hatch/.secrets/magnova_alerts_token")

# Example input shown on the Store page. MUST match the input
# schema's prefill (fleet rule: example input = schema prefill,
# and it must be run in staging). No pinned window here: unlike
# the Google-sourced actor, USPTO's index is current, so the
# default last-7-days window returns live data.
PREFILL_INPUT = {
    "queries": ["machine learning"],
    "backfillDays": 7,
    "maxResults": 250,
}
EXAMPLE_INPUT = PREFILL_INPUT

SEO_TITLE = "USPTO Patent Search — New Patent Alerts API"
SEO_DESCRIPTION = (
    "Search USPTO's official US patent index by keyword, assignee, inventor "
    "or CPC class: publication number, title, assignee, first inventor, "
    "filing / publication / priority dates, application number, CPC codes "
    "and the official USPTO PDF — with monitoring mode that delivers "
    "only-new US patents on every scheduled run. $0.004 per patent, "
    "no start fee."
)
CATEGORIES_PRIMARY = ["BUSINESS", "AUTOMATION", "LEAD_GENERATION"]
CATEGORIES_FALLBACK = ["BUSINESS", "AUTOMATION", "MARKETING"]

TOKEN = get_credential("custom.legacy-apify", "api_token")["value"]


def api(method: str, path: str, body: dict | None = None,
        raw: bytes | None = None, ctype: str = "application/json") -> dict:
    url = API + path
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None
    )
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"{method} {path} -> {exc.code}: {detail[:800]}")


def get_actor() -> dict | None:
    try:
        return api("GET", f"/acts/{ACTOR_NAME}")["data"]
    except SystemExit:
        return None


def cmd_create() -> None:
    existing = get_actor()
    if existing:
        print(f"actor exists: {existing['id']} ({existing['name']})")
        return
    created = api("POST", "/acts", {
        "name": ACTOR_NAME,
        "title": ACTOR_TITLE,
    })["data"]
    print(f"created actor {created['id']} ({created['name']})")


def build_source_tarball() -> bytes:
    include_roots = {"uspto_patents"}
    include_files = {
        "Dockerfile", "requirements.txt", ".actor/actor.json",
        ".actor/input_schema.json", ".actor/dataset_schema.json",
        ".actor/output_schema.json", ".actor/pay_per_event.json",
        "README.md",
    }
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(REPO.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel in include_files or rel.split("/")[0] in include_roots:
                if "__pycache__" in rel or rel.endswith(".pyc"):
                    continue
                tar.add(path, arcname=rel)
    return buf.getvalue()


def wait_build(build_id: str) -> dict:
    for _ in range(120):
        build = api("GET", f"/actor-builds/{build_id}")["data"]
        if build["status"] in ("SUCCEEDED", "FAILED", "ABORTED",
                               "TIMED-OUT"):
            return build
        time.sleep(5)
    raise SystemExit(f"build {build_id} did not finish in time")


def pricing_infos() -> list[dict]:
    """Tiered pay-per-event pricing (100/85/70), no actor-start fee."""
    return [{
        "pricingModel": "PAY_PER_EVENT",
        "pricingPerEvent": {
            "actorChargeEvents": {
                "patent-fetched": {
                    "eventTitle": "Patent fetched",
                    "eventDescription": (
                        "One US patent publication delivered as a "
                        "structured record: publication number and "
                        "kind code, title, whether the publication "
                        "is a grant or an application publication, "
                        "the recorded assignee (or the applicant "
                        "where no assignee is recorded) and "
                        "first-listed inventor exactly as published "
                        "in the official register, filing / "
                        "publication / priority dates, application "
                        "number, CPC classifications, USPTO's "
                        "family ID and a link to the official USPTO "
                        "PDF of the publication. Already-seen "
                        "patents, out-of-window results, silently "
                        "seeded patents (monitorOnlyNew), ERROR "
                        "records and the run SUMMARY are never "
                        "charged."
                    ),
                    "eventTieredPricingUsd": {
                        "FREE": {"tieredEventPriceUsd": 0.004},
                        "BRONZE": {"tieredEventPriceUsd": 0.004},
                        "SILVER": {"tieredEventPriceUsd": 0.0034},
                        "GOLD": {"tieredEventPriceUsd": 0.0028},
                        "PLATINUM": {"tieredEventPriceUsd": 0.0028},
                        "DIAMOND": {"tieredEventPriceUsd": 0.0028},
                    },
                    "isPrimaryEvent": True,
                }
            }
        },
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }]


def cmd_stage() -> None:
    cmd_create()
    actor = get_actor()
    assert actor is not None
    actor_id = actor["id"]

    versions = api("GET", f"/acts/{actor_id}/versions")["data"]
    version_numbers = [v["versionNumber"] for v in versions]
    if "0.0" not in version_numbers:
        api("POST", f"/acts/{actor_id}/versions", {
            "versionNumber": "0.0",
            "sourceType": "SOURCE_FILES",
            "buildTag": "latest",
        })
        print("created version 0.0")

    tarball = build_source_tarball()
    uploaded = api(
        "PUT",
        f"/acts/{actor_id}/versions/0.0/sources",
        raw=tarball, ctype="application/x-tar",
    )
    print(f"uploaded sources ({len(tarball)} bytes): {uploaded.get('data')}")

    build = api("POST", f"/acts/{actor_id}/builds?version=0.0&useCache=false",
                {"buildNumber": ""})["data"]
    print(f"build started: {build['id']}")
    build = wait_build(build["id"])
    print(f"build {build['id']}: {build['status']}")
    if build["status"] != "SUCCEEDED":
        log_url = build.get("buildLogUrl") or ""
        raise SystemExit(f"build failed; log: {log_url}")

    api("PUT", f"/acts/{actor_id}", {
        "isPublic": False,
        "pricingInfos": pricing_infos(),
        "defaultRunOptions": {
            "build": "latest",
            "timeoutSecs": 3600,
            "memoryMbytes": 256,
        },
        "exampleRunInput": {
            "contentType": "application/json; charset=utf-8",
            "body": json.dumps(EXAMPLE_INPUT),
        },
    })
    print("pricing + default run options + example input applied")

    api("POST", f"/acts/{actor_id}/standby", {
        "isEnabled": True,
        "preferredMode": "SINGLE_TENANT",
    })
    print("standby enabled (SINGLE_TENANT)")


def cmd_set_pricing() -> None:
    actor = get_actor()
    assert actor is not None
    api("PUT", f"/acts/{actor['id']}", {
        "isPublic": bool(actor.get("isPublic")),
        "pricingInfos": pricing_infos(),
    })
    print("pricing updated")


def cmd_set_env() -> None:
    actor = get_actor()
    assert actor is not None
    token = TOKEN_FILE.read_text().strip()
    if not token:
        raise SystemExit("alerts token file is empty")
    env_vars = [{
        "name": "MAGNOVA_ALERTS_TOKEN",
        "value": token,
        "isSecret": True,
    }]
    api("PUT",
        f"/acts/{actor['id']}/versions/0.0/env-vars",
        {"envVars": env_vars})
    print("MAGNOVA_ALERTS_TOKEN set on version 0.0 (value not shown)")


def cmd_runs() -> None:
    actor = get_actor()
    assert actor is not None
    runs = api("GET", f"/acts/{actor['id']}/runs?limit=10&desc=1")["data"]
    for item in runs.get("items", []):
        print(item["id"], item["status"],
              item.get("startedAt"), item.get("finishedAt"),
              "usage$", round(item.get("usageTotalUsd", 0), 4))


def cmd_run_status(run_id: str) -> None:
    run = api("GET", f"/actor-runs/{run_id}")["data"]
    print(json.dumps({
        "id": run["id"],
        "status": run["status"],
        "startedAt": run.get("startedAt"),
        "finishedAt": run.get("finishedAt"),
        "usageTotalUsd": run.get("usageTotalUsd"),
        "chargedEventCounts": run.get("chargedEventCounts"),
        "defaultDatasetId": run.get("defaultDatasetId"),
        "defaultKeyValueStoreId": run.get("defaultKeyValueStoreId"),
        "exitCode": run.get("exitCode"),
    }, indent=1))


def cmd_run_input(run_id: str) -> None:
    run = api("GET", f"/actor-runs/{run_id}")["data"]
    store_id = run.get("defaultKeyValueStoreId")
    record = api("GET", f"/key-value-stores/{store_id}/records/INPUT")
    print(record if isinstance(record, str) else json.dumps(record, indent=1)[:2000])


def cmd_dataset(dataset_id: str, limit: int = 20) -> None:
    items = api("GET",
                f"/datasets/{dataset_id}/items?limit={limit}&clean=true")
    print(json.dumps(items, indent=1)[:12000])


def cmd_publish() -> None:
    actor = get_actor()
    assert actor is not None
    description = (REPO / ".actor" / "actor.json").read_text()
    actor_json = json.loads(description)
    short_desc = actor_json["description"]
    assert len(short_desc) <= 300, len(short_desc)
    body = {
        "isPublic": True,
        "title": ACTOR_TITLE,
        "name": ACTOR_NAME,
        "description": short_desc,
        "seoTitle": SEO_TITLE,
        "seoDescription": SEO_DESCRIPTION,
        "categories": CATEGORIES_PRIMARY,
        "defaultRunOptions": {
            "build": "latest",
            "timeoutSecs": 3600,
            "memoryMbytes": 256,
        },
        "exampleRunInput": {
            "contentType": "application/json; charset=utf-8",
            "body": json.dumps(EXAMPLE_INPUT),
        },
        "pricingInfos": pricing_infos(),
    }
    try:
        updated = api("PUT", f"/acts/{actor['id']}", body)["data"]
    except SystemExit:
        body["categories"] = CATEGORIES_FALLBACK
        updated = api("PUT", f"/acts/{actor['id']}", body)["data"]
    print("published:", updated.get("isPublic"), updated.get("name"),
          updated.get("title"))
    print("notice:", updated.get("notice"))
    check = api("GET", f"/acts/{actor['id']}")["data"]
    print("read-back: isPublic =", check.get("isPublic"),
          "| notice =", check.get("notice"),
          "| title =", check.get("title"))
    pricing = (check.get("pricingInfos") or [{}])[-1]
    events = ((pricing.get("pricingPerEvent") or {})
              .get("actorChargeEvents") or {})
    for name, ev in events.items():
        tiers = ev.get("eventTieredPricingUsd") or {}
        print("event", name,
              {k: v.get("tieredEventPriceUsd") for k, v in tiers.items()})
    standby = api("GET", f"/acts/{actor['id']}/standby")["data"]
    print("standby:", standby.get("isEnabled"), standby.get("preferredMode"))


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "create":
        cmd_create()
    elif cmd == "stage":
        cmd_stage()
    elif cmd == "set-pricing":
        cmd_set_pricing()
    elif cmd == "set-env":
        cmd_set_env()
    elif cmd == "runs":
        cmd_runs()
    elif cmd == "run-status" and len(sys.argv) > 2:
        cmd_run_status(sys.argv[2])
    elif cmd == "run-input" and len(sys.argv) > 2:
        cmd_run_input(sys.argv[2])
    elif cmd == "dataset" and len(sys.argv) > 2:
        cmd_dataset(sys.argv[2],
                    int(sys.argv[3]) if len(sys.argv) > 3 else 20)
    elif cmd == "publish":
        cmd_publish()
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
