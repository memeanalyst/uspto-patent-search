# USPTO Patent Search — New Patent Alerts

Search the USPTO's own patent index — **USPTO Patent Public Search**, the official register of US patent grants and published US patent applications — by keyword, assignee, inventor or CPC class, and get clean structured records: publication number, title, assignee, inventor, filing / publication / priority dates, application number, CPC classifications and a link to the official USPTO PDF.

**US coverage only.** This Actor reads the two US databases (grants and published applications). It does not cover EP, WO, CN or other offices — if you need worldwide patent data, this is not the tool.

Run it once for a corpus pull, or schedule it with a monitor store and every later run delivers **only the patents published since the last run** — new grants and new applications for exactly the searches you care about.

## What you get

One `PATENT` record per delivered publication, one `SUMMARY` record at the end of every run, and an `ERROR` record if the source cannot be read (never silently empty).

| Field | What it is | Present on |
|---|---|---|
| `publicationNumber` | The publication's stable number as published by the USPTO, e.g. `US12802069B1`, `US20260370217A1` — the key only-new detection diffs against | Always |
| `kindCode` | The WIPO kind-code suffix (`B1`, `B2`, `A1`, …), exactly as published | ≥ 95% of records |
| `patentTitle` | The patent title as indexed by the USPTO | ≥ 95% of records |
| `type` | `grant` for a granted US patent, `application` for a published US patent application | ≥ 95% of records |
| `assignee` | The recorded assignee exactly as published in the official register (usually an organisation; occasionally a person for independent filings). Where a publication records no assignee — common for the newest applications — this is the recorded **applicant** instead | ≥ 90% of records |
| `inventors` | A one-entry list: the **first-listed** inventor, exactly as published, in "First Last" order. A publication can name more inventors; the USPTO search payload returns the first | ≥ 90% of records |
| `filingDate` | Application filing date (ISO date) | ≥ 95% of records |
| `publicationDate` | Publication date (ISO date) — the date the run window compares against | Always |
| `priorityDate` | Earliest priority date, where the publication claims priority. Absent on first filings that claim none — common for the newest applications | Where claimed |
| `applicationNumber` | The USPTO application (serial) number, e.g. `18/598057` | ≥ 95% of records |
| `cpcCodes` | The publication's inventive CPC classification symbols, e.g. `["G06N20/00"]` | ≥ 85% of records |
| `familyId` | The USPTO's current family identifier for the publication (a stable numeric ID, shipped as a string) | ≥ 95% of records |
| `sourceUrl` | The publication's official PDF on the USPTO's image server (`image-ppubs.uspto.gov`) | Always |
| `matchedQueries` | Keyword runs only: the keyword query that surfaced this patent | Keyword runs |

**No abstract.** The USPTO search payload carries bibliographic data only, so records ship without an abstract — what you see in the table above is the complete record.

## Three ways to run it

**Corpus (default).** A run delivers the current matching set for its publication window — the backfill is the first-run value. Set keywords and/or filters, pick a window (last N days, or an explicit date range up to 366 days), and cap the run with *Maximum patents per run*.

**Monitoring (only-new).** Pick one of your own key-value stores in the *Monitor state store* input and schedule the run (e.g. daily, with 2–3 days of lookback). The first run delivers the window's current patents as a baseline; every later run delivers only patents the store has not seen. Patents a capped run could not deliver stay unseen and are delivered by the next run. Turn on *Start monitoring silently* to skip the baseline: the first run just remembers the current set, and only later publications are ever delivered.

**Stateless cursor.** Workflow tools (Make, Zapier, your own scheduler) that hold their own state can pass `publishedSince` (an ISO date or datetime) instead of a store: only patents published on/after that day are delivered, boundary-inclusive.

## Inputs

| Input | What it does |
|---|---|
| Keyword queries | One search per entry (e.g. `machine learning`). Every other filter applies to each search. A patent found by several searches is delivered — and charged — once. Leave empty to search by filters alone |
| Assignees | Only patents whose **recorded assignee** matches one of these names (OR). One honest limit: a brand-new application that lists only an applicant is not matched by this filter until an assignee is recorded |
| Inventors | Only patents whose first-listed inventor matches. The index stores inventors surname-first, so `Goodenough` or `Goodenough John` work best; a `First Last` entry is also tried surname-first automatically |
| CPC codes | Only patents classified under one of these CPC symbols. A main group also returns its subgroups (`G06N20/00` returns `G06N20/10` too); a subclass (`G06N`) returns the whole subclass |
| Publication days back | Window size in days, counting back from yesterday (the last complete publication day). Ignored when explicit dates are set. Max 92 |
| Date from / Date to | Explicit window (both together), up to 366 days — for one-off corpus pulls |
| Maximum patents per run | Hard cap on patents delivered (and charged) per run, 1–5,000 |
| Maximum total charge per run (USD) | Optional hard stop: delivery stops when charged patents reach this amount; the SUMMARY reports `stoppedByMaxCharge` |
| Monitor state store | A key-value store you own (read/write) where the Actor keeps one small state record of seen publication numbers. Without it, every run is a baseline run |
| Start monitoring silently | With a store: the first run seeds silently (nothing delivered, nothing charged) |
| Published on/after | The stateless `publishedSince` cursor described above |

## Example

Input:

```json
{
  "queries": ["machine learning"],
  "backfillDays": 7,
  "maxResults": 250
}
```

Output (one real record, from a November 2026 run):

```json
{
  "recordType": "PATENT",
  "publicationNumber": "US20260370217A1",
  "kindCode": "A1",
  "patentTitle": "Software-Defined Pulse Orchestration Platform",
  "type": "application",
  "assignee": "Quantum Machines",
  "inventors": ["Yonatan COHEN"],
  "filingDate": "2024-03-07",
  "publicationDate": "2026-11-19",
  "applicationNumber": "18/598057",
  "cpcCodes": ["H03K3/38", "G06N3/04", "G06N10/00", "H03K19/195"],
  "familyId": "1000007776137",
  "sourceUrl": "https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/US20260370217",
  "matchedQueries": ["machine learning"]
}
```

Every run ends with a `SUMMARY` record reporting how many publications matched, how many were fetched, delivered, skipped as duplicates / out-of-window / dateless, whether the source errored, and whether delivery stopped at a cap, the max charge or the timeout.

## Pricing

Pay per event — you pay only for patents actually delivered:

| | Per patent | Per 1,000 patents |
|---|---|---|
| Free / Bronze | $0.004 | $4.00 |
| Silver | $0.0034 | $3.40 |
| Gold / Platinum / Diamond | $0.0028 | $2.80 |

No Actor start fee. ERROR records, the SUMMARY, duplicates, out-of-window results and silently seeded patents are **never charged**.

## Reliability contract

- If USPTO Patent Public Search cannot be read at all, the run writes one ERROR record plus the SUMMARY and ends normally — and the monitor state is **not** advanced, so the next run catches up. A broken source never looks like "no new patents".
- If a search breaks mid-stream, the patents already delivered stand, the rest stay unseen, and the SUMMARY plus an ERROR row say so.
- The window is enforced twice: server-side in the search itself, and again on every record's publication date before delivery. A run never silently delivers out-of-window publications.
- Register data only: names and organisations exactly as published in the official register. No contact details, ever.

## Server mode (API access)

This Actor also runs in standby server mode: POST a JSON body with the usual input fields to the Actor's standby URL and get `{"records": [...], "summary": {...}}` back in the response — no run to start, no dataset to poll. Server mode is a snapshot lookup (delta state and only-new monitoring stay batch-run features), returns at most 100 patents per request, and charges the same $0.004 per delivered patent.

## Notes and limits

- Data comes from the USPTO's public search, read logged-out with no API key; results are the USPTO's own index, including its indexing lag for the very newest publications (the index is typically current to the most recent publication days).
- Name matching follows the index: assignee and inventor filters match the names as recorded, so spelling variants in the register are separate names.
- The Actor remembers processed publication numbers in your store (capped, oldest dropped first); widening a monitoring window later does not re-deliver old patents.

⭐ Finding this useful? A rating on the Apify Store helps other users discover this Actor — thank you!
