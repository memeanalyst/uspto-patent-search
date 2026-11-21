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

## Step 4 — staging gates

(to be filled in as staging runs land)

## Step 5 — publish

(to be filled in at publish)
