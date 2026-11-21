# USPTO Patent Public Search (ppubs) API — reverse-engineered 2026-11-21

Source: https://ppubs.uspto.gov/pubwebapp/ (USPTO's official Patent Public Search).
Keyless: anonymous session, no account, no API key. Verified end-to-end from the workspace.

## Flow (all four steps required, in order)
1. GET https://ppubs.uspto.gov/pubwebapp/ (shell; sets nothing, but do it anyway)
2. POST https://ppubs.uspto.gov/api/users/me/session
   - body: the plain text `-1` (Content-Type: text/plain), headers X-Requested-With: XMLHttpRequest, Referer: https://ppubs.uspto.gov/pubwebapp/
   - Response JSON gives userCase.caseId (needed in the query). Response HEADER `x-access-token` is the auth token for all later calls (send it as the `x-access-token` header).
3. POST https://ppubs.uspto.gov/api/searches/counts with the query object (below).
   - Returns the query ENRICHED by the server (adds termGraph, expand*, hl, fl, originalQuery...). **searchWithBeFamily REJECTS the raw query with 400 "Invalid request content" — it only accepts the counts-enriched object.** Always run counts first and pass its response verbatim as `query`.
4. POST https://ppubs.uspto.gov/api/searches/searchWithBeFamily with:
   {"start":0, "pageCount":25, "sort":"score desc", "docFamilyFiltering":"noFiltering",
    "searchType":1, "familyIdEnglishOnly":null, "familyIdFirstPreferred":null,
    "familyIdSecondPreferred":null, "familyIdThirdPreferred":null,
    "showDocPerFamilyPref":null, "queryId":0, "tagDocSearch":false,
    "query": <counts-enriched query>}
   - Response: {numFound, perPage, page, totalPages, sort (server appends ",id asc"), patents: [...], numberOfFamilies, cursorMarker, ...}

## Query object that counts ACCEPTS (200, numResults 5,912,946 for "machine learning")
{"anchorDocIds":[], "querySource":"search", "caseId":<from session>, "facetFilters":[],
 "hl_snippets":"3", "op":"OR", "q":"machine learning", "excludeResultsAfter":null,
 "queryName":"machine learning", "highlights":"1", "qt":"brs", "spellCheck":false,
 "viewName":"grid", "plurals":true, "britishEquivalents":true,
 "databaseFilters":[{"databaseName":"USPAT","countryCodes":[]},{"databaseName":"US-PGPUB","countryCodes":[]}],
 "searchType":1, "cNumber":null, "ignorePersist":true,
 "userEnteredQuery":"machine learning", "extSearchQueryType":null}
Notes: highlights must be "1" (null FAILS counts with 400). querySource "BRS" FAILS; "search" works.
Databases: USPAT = grants, US-PGPUB = published applications. Drop one to filter by type.

## Result record fields (rich)
guid "US-20260046312-A1", publicationReferenceDocumentNumber, datePublished (ISO),
inventionTitle, applicantName[], assigneeName, inventorsShort ("Last; First" semicolon-sep),
applicationNumber, applicationFilingDate[], cpcInventiveFlattened ("H04L63/1491;..."),
ipcCodeFlattened, type (US-PGPUB/USPAT), pageCount, familyIdentifierCur, languageIndicator.

## NOT yet solved (build-stage prototyping, counts is a cheap oracle)
- Sort: only "score desc" verified. "date desc", "datePublished desc/asc", "pd desc",
  "publicationDate desc" all return 500. Find the real date-sort field or design around it.
- Date windows: classic BRS `.pd.` syntax does NOT behave (20261118.pd. barely filters;
  202611$.pd. returns 0; no `.pd.` anywhere in the app bundle). Leads: the query DTO has an
  `fq` field (Solr filter query, null in echoes) — try fq datePublished range; `excludeResultsAfter`
  is a query field (type/semantics unknown — test a date string); facetFilters may carry date ranges.
- Field codes for assignee/inventor/CPC filters in q: `.as.` seemed to parse (a combined query
  zeroed only because of the date term). Verify each alone via counts: samsung.as., an inventor
  .in., a CPC .cpc. or via databaseFilters-style filter objects.

## Egress status
- Workspace: Espacenet 403, PatentScope 403, Justia 403, ppubs full flow OK.
- Platform (Apify IPs, url-feasibility-probe): ppubs shell 200 + marker found; PatentScope
  search page 200 (fallback only); Espacenet 6KB bot-wall page (marker absent); Justia 403.
- The ppubs POST flow from platform IPs is NOT yet verified — it is the build's first gate.
