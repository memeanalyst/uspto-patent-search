# USPTO Patent Search — build notes

Actor: magnova/uspto-patent-search ("USPTO Patent Search — New Patent Alerts")
Engine: USPTO Patent Public Search (ppubs.uspto.gov) public JSON API, keyless.
Replaces: magnova/google-patents-scraper (Google's reachable index frozen ~Aug 2026;
the Google actor is NOT touched by this build — deletion is a separate step).

## Step 1 — ppubs mechanics (settled 2026-11-21, workspace prototypes)

Full detail in PPUBS_API_NOTES.md. The settled design:

- **Flow**: GET /pubwebapp/ → POST /api/users/me/session (body `-1`,
  X-Requested-With) → `x-access-token` response header + JSON
  userCase.caseId → POST /api/searches/counts → POST
  /api/searches/searchWithBeFamily with the counts-ENRICHED query
  verbatim (the raw query object is rejected, HTTP 400).
- **Date window (server-side, exact)**: the query object's `fq`
  field carries a Solr range on `date_publ`:
  `date_publ:[YYYY-MM-DDT00:00:00Z TO YYYY-MM-DDT23:59:59Z]`.
  Evidence: "machine learning" Nov 2026 counts 25,734 with the fq
  vs 5,912,946 without; `fq document_type:USPAT` = 2,992,302,
  matching the facet count exactly; search results verified
  in-window only. Client-side in_window check kept as
  belt-and-braces (and it makes the publishedSince cursor exact).
- **Sort**: `date_publ desc` (Solr field mined from the app
  bundle's column config; "date desc"/"datePublished desc"/"pd
  desc" all HTTP 500). Newest data at test time: 2026-11-19
  publications — the index is current (the point of the switch).
- **BRS fragments must be parenthesised**: unparenthesised
  `machine learning AND "samsung electronics".as.` = 5,696,119
  (≈ the full set); parenthesised = 63,593 (the true
  intersection).
- **Assignee `.as.` matches assigneeName ONLY** (verified:
  "auctane".as. found grant US-12802069-B1; applicant-only names
  do not match). Documented in the input description + README.
- **Inventor `.in.`** works; index is surname-first ("goodenough
  john".in. = 79 vs goodenough.in. = 310 in the prototype
  census). Multi-word input names are tried as given AND
  surname-first.
- **CPC lives in BRS, never fq** (fq on cpc_inventive_flattened
  is broken: quoted exact = 71 vs BRS 837). A full group symbol
  is required: G06N20/00.cpc. Nov = 837; main-group wildcard
  G06N20/*.cpc. = 928 (adds subgroups, G06N20/10 = 89);
  G06N*.cpc. = 2,157. G06N.cpc. / G06N20.cpc. = 0.
- **Filterless unit q = `(a)`**: empty q → 400, `*` → 0, `*:*` →
  500. Token "a" verified universal on the Nov census (USPAT
  28,754 + US-PGPUB 27,389 = 56,143; identical total for "us").
- **Paging**: pageCount up to 1,000 accepted; start offsets
  verified to 25,730 of 25,734. The client pages at 100.
- **Record facts** (100–200 record surveys): guid
  "US-20260370217-A1" → publicationNumber US20260370217A1;
  kindCode agrees with the guid suffix 200/200 (can be a list —
  take first). inventorsShort = FIRST inventor only, "Last;
  First", exactly one ";" in 100/100, " et al." in 83% → ship
  ["First Last"]. assigneeName null on 72% of a fresh-applications
  page; applicantName never null → assignee = assigneeName[0]
  else applicantName[0] (documented in copy).
  cpcInventiveFlattened ";" separated, ~100% populated.
  priorityClaimsDate sparse on fresh apps (2/200) → earliest =
  priorityDate. **No abstract in the search payload — none is
  claimed anywhere.**
- **sourceUrl = official USPTO PDF**:
  https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/US{docNumber}
  — verified HTTP 200 application/pdf for a Nov A1, a Nov B1 and
  a Feb A1. (Google Patents doc pages returned 503 from the
  workspace and its index is frozen — not used.)

## Step 2 — platform egress gate (PASSED 2026-11-21)

Probe actor (magnova/url-feasibility-probe) extended with a
`ppubsTest` mode performing the full POST flow from inside
Apify. Gate run {q:"(machine learning)", windowDays:7}: controls
behaved (Rightmove 200 + marker, Zoopla 403); shell 200, session
200 + token issued, counts 200 (numResults 8,180 for the
2026-11-14→21 window), search 200, numFound 8,180, newest
datePublished 2026-11-19, allInWindow true, sample GUIDs
identical to the workspace run. The full POST flow works from
Apify IPs.

## Step 3 — build

Architecture mirrors the Google Patents sibling (same monitor
design, state semantics, reporter + drift plumbing, server mode),
with the transport and query construction replaced by the ppubs
mechanics above. 50 unit tests pass locally (client incl. real
captured fixtures, drift, reporter, server mode). Live workspace
smoke of the shipped client (2026-11-21): counts 8,180 for the
prefill window — identical to the platform gate — first page
100 records, all in-window, newest 2026-11-19.

## Step 4 — staging gates (ALL PASSED 2026-11-21)

Actor ID 0pNCVqMaQSVc4MECl (private staging). Runs on the Magnova
account; total staging usage ~$0.002 (budget was ~$0.50).

- **Smoke (recent window)**: run iOc10ePeFjvt4eyfu — prefill
  queries/window, maxResults 25. matchedTotal 8,180 (identical to
  the platform gate and the workspace count), 25 PATENT + SUMMARY,
  ALL records dated 2026-11-19 (the newest publication day).
  An empty recent window would have failed the gate — it is full.
- **Corpus (October 2026)**: run 1iDcHQtiHRFxhLmaD —
  matchedTotal 35,775 (identical to the Step-1 prototype count),
  25 delivered, all dated 2026-10-29, filteredOutDate 0.
- **Monitor lifecycle** (CPC G06N20/00 unit, 2-day window, 99
  matched): baseline kYnj2BtiJgs9AS9fx delivered 10, state = the
  10 delivered ids exactly (capped remainder stays unseen);
  rerun QyAQKg9v2VYwWXQ1Z delivered the NEXT 10 with 0 overlap
  and duplicatesSkipped 10; silent seed t1aetP31OYyV6tKQ1
  (monitorOnlyNew, fresh store) delivered 0, seededSilently 99,
  charged 0; post-seed rerun 36eUcGaxPK8SWj3ZM delivered 0 with
  duplicatesSkipped 99; after trimming 3 ids from the state,
  rerun lmgonR46xh89WyCfU delivered EXACTLY those 3
  (duplicatesSkipped 96).
- **Charge audit** (delayed re-read — counts lag at finish):
  25+25+10+10+0+0+3 = 73 delivered = 73 charged
  ({'patent-fetched': N} per run, exact on every run).
- **Claims-vs-data census** (50 PATENT records, smoke + corpus):
  every field named in the README table at 100% population
  (floors 0.85–1.00); priorityDate 18% vs its 0.02 floor
  (qualitative "where claimed" copy). PASS.
- **Personal-data scan**: independent scanner written from
  scratch (email / US phone / intl phone / SSN shapes);
  POSITIVE CONTROL passed (all 4 kinds found in a planted
  record); real scan over all 70 unique staging records:
  0 hits. (Inventor/assignee names are public register data —
  a documented product field, not a finding.)
- Build note: the dataset-schema validator rejects a field
  literally named `type`; the record field ships as
  `publicationType`.

## Step 5 — publish (DONE 2026-11-21)

Published via deploy.py publish (full-body PUT). Read-back
verified independently: isPublic True, **notice NONE**, title
"USPTO Patent Search — New Patent Alerts", description 296
chars, categories BUSINESS / AUTOMATION / LEAD_GENERATION,
pricing patent-fetched FREE/BRONZE $0.004, SILVER $0.0034,
GOLD/PLATINUM/DIAMOND $0.0028, no start fee; standby enabled
SINGLE_TENANT; actorPermissionLevel LIMITED_PERMISSIONS;
exampleRunInput = the schema prefill. Standby URL answers 401
unauthenticated (live + auth-gated):
https://magnova--uspto-patent-search.apify.actor
Store: https://apify.com/magnova/uspto-patent-search
GitHub: https://github.com/memeanalyst/uspto-patent-search

Notes:
- The actor GET's `readme` field reads empty — the live Google
  Patents sibling shows the same via this endpoint; the README
  ships in the build source (fleet pattern) and renders on the
  Store from there.
- A standby caller test with the outside test account was not
  run (the test token is not persisted locally — fleet-standard
  skip); server mode is unit-tested and mirrors the sibling's
  verified implementation.
- Icons A–D are in icons/ (media-generation-*.webp); upload is
  Hamzah's Console step. Muse's pick: A (navy/gold scroll +
  magnifier + seal) for the official-register feel, C (violet
  radar + bell) if he wants the alerts angle.
