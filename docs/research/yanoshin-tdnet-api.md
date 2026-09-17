# Yanoshin TDnet WebAPI — measured behaviour

**2026-09-17.** Adopted as the zero-cost JP timely-disclosure **discovery** source, reopening what [D-122](../unresolved-decisions.md) had closed. Everything below was measured against the live service, not read off the documentation — and the two disagree in three places, one of which would silently break a collector.

The registry entry and its licence verdict are in `news.sources` / `news.source_policies` (migration `20260917220000`).

---

## What this source is, and is not

TDnet itself stays closed: its robots.txt disallows the whole host and its pages prohibit 複製. Yanoshin publishes an **index** of the same disclosures, and the operator frames it that way:

> 当サービスはインデックス情報を提供するものであり、提供元サーバへの書類URLリンクを提供しております。書類データは提供元のサイトを必ずご確認ください。

So the split is the operator's own, not one we invented:

| | Source | May we keep it? |
|---|---|---|
| Who disclosed what, when, under which code, and where the document is | Yanoshin index | **Yes** |
| The disclosure PDF / XBRL itself | TDnet | **No** — and reaching an index that links to it changes nothing |

`full_text_storage_allowed = PROHIBITED` on this source, so `news.assert_storage_allows` refuses a full-text write. The adapter's `fetch_body()` returns `None` unconditionally. Verification of what a disclosure actually *said* comes from the issuer's own IR feed or EDINET — sources that do permit storage — and joins the same material event as a second source.

## Access

- No authentication, no key, no account.
- `robots.txt` disallows `*.xml`, `*.json`, `*.rss` and `*.atom` **to Googlebot specifically**, then `User-Agent: * / Allow:/`. The machine-readable endpoints are closed to Googlebot and open to everyone else, so our collector is permitted. Worth reading carefully rather than skimming — the first block looks like a blanket ban.
- No published rate limit. `llms.txt` says only *"No rate limit is explicitly enforced, but please be reasonable with request frequency."* The 0.3 s pause in the adapter is **politeness, configurable**, not a documented figure being obeyed. No number was invented.
- No `ETag`, no `Last-Modified`, so conditional requests are impossible. Deduplication is ours to do.
- `Content-Type` is `text/html; charset=UTF-8` even for `.json`.

## Three places the documentation is wrong

### 1. `since_id` and `by_id` are parsed as Unix timestamps

`llms.txt` documents `since_id` as *"Return items with ID greater than this"*. It is not implemented that way. The value goes into a date parser:

| Request | `condition_desc` returned | Items |
|---|---|---|
| `since_id=1281122` (a real item id) | 最新順の適時開示情報一覧**(期間開始日1970/01/16)** | 10, including ids *below* the pivot |
| `since_id=1` | **(期間開始日1970/01/01)** | unfiltered |
| `since_id=1789000000` (an epoch in Sept 2026) | **(期間開始日2026/09/10)** | filtered by date, correctly |
| `since_id=abc` | no date clause at all | unfiltered |
| `by_id=1281122` | **(期間終了日1970/01/16)** | **0** |

A collector built on the documentation would either re-read the same window forever (`since_id`) or read nothing at all and look like a quiet day (`by_id`). The adapter **raises** if either is passed, so nobody reinstates the documented behaviour that does not exist.

`start_datetime` / `end_datetime` **do** work, and so does the `YYYYmmdd-YYYYmmdd` key.

### 2. The JSON nesting key is `Tdnet`, not `TDnet`

`llms.txt` shows `items[].TDnet`. The service sends `items[].Tdnet`. A parser that trusted the documentation returns zero items on every call — indistinguishable from a day on which nothing was disclosed. Both spellings are accepted so an upstream correction does not break us either.

### 3. Undocumented fields, and an undocumented parameter that does nothing

The response also carries `total_count`, `actions`, and five `url_report_type_*` fields absent from the documentation. `page` is accepted and **ignored**: `page=2` returns byte-identical results to no `page` at all, so it is not a pagination mechanism and is not relied on.

## Incremental collection

The cursor is a **client-side high-water mark on the item id**, which is what `since_id` was meant to be.

- Ids are assigned in **insert order, not publication order**. Live example: id `1281124` at 12:20 and id `1281125` at 12:15 — the higher id is the earlier disclosure. So the id is a watermark for "what is new", never a sort key for time.
- `recent?limit=300` returned 300 items spanning **2.3 days**, at roughly **130 disclosures a day**. A daily poll has about a day of headroom.
- When the page comes back **full**, it may have been truncated, and `window_is_safe()` returns false. The caller then re-reads by date range rather than assuming the gap is closed. This fired on the very first live pass.
- Date ranges are used for initial backfill, gap recovery and integrity checking — the three jobs the user's design assigns them, and the only filter the service implements correctly.
- Items can be revised in place under a stable id (`update_history` exists for exactly that), so a known id with a **different content hash** is a revision, not a duplicate.

## Company code normalisation

Codes arrive as five characters. The trailing zero is **not** stripped unconditionally.

| Raw | Normalised | Why |
|---|---|---|
| `72030` | `7203` | fifth character `0` is the ordinary-share suffix |
| `130A0` | `130A` | same, with an alphanumeric base |
| `587A4` | **`587A4`** | fifth character is not `0` — an ETN |
| `13264` | **`13264`** | an ETF (SPDR Gold) |
| `15574` | **`15574`** | an ETF (SPDR S&P 500) |

Of 300 live items, **280 were stripped and 20 were kept** — and every kept one was a fund. Stripping them would have merged each with whatever four-digit issuer shares its prefix. The raw code is always stored beside the normalised one, and a database check constraint keeps the two in step so the wrong branch cannot be recorded.

### Normalisation and lookup are different jobs

Keeping `13264` whole is right, and on its own it resolves nothing: the security master carries that ETF as **`1326`**. All 477 ETFs in the master are held under four-character codes; TDnet reports them with a fifth character.

So the lookup tries the exact form **first** and the four-character base **second**, and records which one matched:

| Match | `mapping_confidence` |
|---|---|
| exact code against the exchange's listing | `REGISTRY_ANCHORED` |
| four-character base | `PROVISIONAL` |

That is not the same thing as stripping the character and forgetting. The rule still never silently merges; a weaker claim is stored as a weaker claim, and `matched_lookup_key` says which key produced it.

### How much of a live page resolves

238 distinct normalised codes from one live pass, checked against the Phase 1 master:

- **224 resolved (94.1 %)**
- **9 were ETFs** reachable only through the base fallback (`13264`→`1326`, `587A4`→`587A`, …)
- **5 did not resolve at all**: `3260`, `619A`, `621A`, `625A`, `9388`. None is present in `ref.listings` under any form, so these are listings newer than the Phase 1 snapshot. That is a **security master freshness finding**, not a normalisation bug, and it is what `unmapped_company_codes` exists to surface.

## Title classification

The body is out of reach, so the disclosure type is derived from the headline. Measured against the same 300 live titles: **5.3 % fall through to `OTHER`**, and `OTHER` still produces a material candidate — the classifier types things, it never excludes them.

The disambiguations that needed real data rather than imagination:

- 自己株式 takes four verbs — 取得 / 買付 / 処分 / 消却 — which are four different corporate actions. And 「譲渡制限付株式報酬としての自己株式**処分**」 has no `の`, so the pattern allows for the gap.
- 業績予想の修正 and 配当予想の修正 share their ending; only the prefix separates them, so dividends are checked first.
- 異動 means whatever precedes it: 子会社 (M&A), 代表取締役 (board), 主要株主 (ownership), 公認会計士等 (auditor).
- 決算短信 is the earnings release; 決算説明資料 is the deck that follows it.
- 「連結子会社からの**配当金受領**」 is money arriving from a subsidiary, not a distribution to shareholders — a pattern anchored on 配当 alone would type it as one.
- An asset sale that books 特別利益 is typed by the gain: that is the number that moves the price.

Every classification is `PROVISIONAL` and carries the pattern it matched. 訂正 and 開示事項の経過 are **flags, not types** — a corrected earnings release is still an earnings release.

## Live verification

One end-to-end discovery pass, 2026-09-17:

```
items_retrieved            300
unique_items               300
new_items                  300
duplicate_items              0
document_url_present       300
xbrl_url_present            22
metadata_only_events       300     (no verification source wired yet)
fetch_errors                 0
last_seen_id           1281127
collection_lag_seconds    1422.3   (~24 minutes behind the newest disclosure)
window_was_full           True     -> re-read 2026-09-16..2026-09-17 by date range
```

No document body was fetched. Every row stored `METADATA_ONLY` with the reason recorded on it.

## Open

- **Attribution.** The policy records `attribution_required = REQUIRED` as a courtesy to an unofficial free service. Nothing on the site demands it; it costs nothing and seems right.
- **It is unofficial.** A single operator, running since 2009, with no service commitment. Treat availability as best-effort: the coverage counters exist so a silent stop is visible, and EDINET remains the licensed path to statutory filings if this one ever goes away.
