# News and disclosure sources — licence review

**2026-09-17.** Twelve candidates, researched against official pages only, each verdict then re-read by a second pass whose instruction was to refute it. Two verdicts changed. Four sources were then fetched live.

The registry and the machine-readable verdicts live in `news.sources` and `news.source_policies` (migration `20260917210000`). This document is the reasoning; the database is the enforcement.

---

## Superseded in part, 2026-09-17

The conclusion below — that Japanese timely disclosure is simply unavailable — was **withdrawn the same day**. Everything it says about TDnet remains true: the host disallows crawling, the pages prohibit reproduction, and the paid API is priced out of reach. The error was the step after that. **The document being unreachable does not make the fact of the disclosure unreachable**, and a third-party service publishes an index of exactly that.

See [yanoshin-tdnet-api.md](yanoshin-tdnet-api.md) and D-122 / D-125. The split that resolves it:

| | Where it comes from | May we keep it? |
|---|---|---|
| Who disclosed what, when, under which code | Yanoshin TDnet WebAPI | **Yes** |
| The disclosure document itself | TDnet | **No** — unchanged |

So Japan is no longer blind to timely disclosure; it is blind to the *contents* of a timely disclosure until a storable source (the issuer's own IR, EDINET) confirms it. That is a much smaller gap, and it is the one the sections below should now be read against.

## The original finding: TDnet itself cannot be collected

TDnet — 適時開示情報閲覧サービス, the timely-disclosure service — is the single most market-moving Japanese source there is, and it is closed to us on two independent grounds.

**robots.txt disallows the entire host.** `https://www.release.tdnet.info/robots.txt` is 26 bytes:

```
User-agent: *
Disallow: /
```

Re-requested as curl, python-requests, Googlebot and Chrome — byte-identical each time, so it is not user-agent conditional. Every page additionally carries `<meta name="robots" content="noindex,nofollow">`.

**The disclosure pages prohibit reproduction.** From the disclaimer written into the foot of every list page:

> 適時開示情報閲覧サービスに記載されている内容は、著作物として著作権法により保護されており、株式会社東京証券取引所に無断で転用、複製又は販売等を行うことは固く禁じます。

Writing the rows and PDFs into a private database is 複製 in the sense of the Copyright Act, and the clause prohibits 複製 done 無断 — without permission. It draws no public/private distinction and attaches no commercial-purpose qualifier.

**The paid route is priced for institutions.** TDnet API: 基本料金 月額70,000円 plus an information tier from 月額100,000円. TDnetDBS: 月額33,400円 per ID. Both sit outside the zero-cost core by two orders of magnitude.

### Why EDINET is not a substitute for the disclosure itself

EDINET carries **statutory periodic filings** — 有価証券報告書, 四半期報告書, 臨時報告書. TDnet carries **the announcement that moves the price on the day**: earnings revisions, guidance changes, M&A, capital moves. They overlap only at the edges.

So the Japanese material side is weaker than the US side, where EDGAR's 8-K is the near-equivalent of a timely disclosure and the licence is the most permissive of the whole set.

**How much weaker changed on the same day.** With the Yanoshin index, Japan gets the *signal* — an issuer disclosed something of a known type at a known minute — within minutes, and the *contents* only once a storable source confirms them. The remaining gap is contents, not existence.

---

## What qualified

| Source | Storage | Verdict | Access | Auth | Timestamp quality |
|---|---|---|---|---|---|
| **SEC EDGAR** | ALLOWED | QUALIFIES (caveat) | Atom + full-text JSON + daily index | none, but a contact User-Agent is **enforced** | Acceptance time, to the second |
| **EDINET API v2** | ALLOWED | QUALIFIES (caveat) | REST/JSON | free API key | `submitDateTime`, to the minute |
| **日本銀行** | ALLOWED | QUALIFIES (caveat) | RSS | none | `pubDate`, RFC-822 with +0900 |
| **Federal Reserve** | ALLOWED | QUALIFIES (caveat) | RSS | none | RSS `pubDate` |
| **U.S. Treasury** | ALLOWED | QUALIFIES (caveat) | JSON index | none | ISO-8601 UTC per item |
| **White House** | ALLOWED | QUALIFIES (caveat) | RSS | none | RSS `pubDate` |
| **BLS / Census / BEA** | ALLOWED | QUALIFIES (caveat) | REST per agency | free key per agency | varies sharply by agency |
| **財務省** | ALLOWED | QUALIFIES (caveat) | HTML only | none | date only |
| **経済産業省** | ALLOWED | QUALIFIES (caveat) | HTML only | none | date only |
| **内閣府 / 首相官邸 / e-Gov** | ALLOWED | QUALIFIES (caveat) | RSS 1.0 | none | `dc:date`, JST |
| **Company IR (Q4-hosted)** | NOT_SPECIFIED | QUALIFIES (caveat) | RSS | none | inconsistent granularity |

Three licence families carry almost all of it:

**公共データ利用規約（第1.0版）**, adopted by EDINET, MOF, METI and the Cabinet Office estates. §1 grants 複製、公衆送信、翻訳・変形等の翻案等 freely and permits commercial use. This is an affirmative grant, not silence — which is what lets it pass a rule that treats silence as refusal.

**US federal works**, public domain under 17 U.S.C. 105. SEC states it explicitly: information on sec.gov "may be copied or further distributed by users of the web site without the SEC's permission."

**The Bank of Japan's own copyright terms**, which permit 転載・複製 except for commercial purposes, except where a part is marked as reserved, and except for photographs and images. This project makes no commercial use, so it falls inside the permission — but a collector must keep text and not figures.

### The one NOT_SPECIFIED

Company IR feeds get `full_text_storage_allowed = NOT_SPECIFIED`. The platform terms permit personal non-commercial access and prohibit redistribution; they say nothing about keeping the body. So the collector stores metadata only and records why on the row. That is the quad-state doing its job rather than a gap in the research.

---

## What the adversarial pass changed

Two verdicts, in **opposite directions** — which is the argument for doing it at all. A verification pass that only ever catches optimism is a pass that has learned to agree.

**U.S. Treasury: reversed.** The first pass had never opened the Bureau of the Fiscal Service site-wide terms, which by their own text extend to `fiscaldata.treasury.gov`. The second pass read them and found nothing prohibiting storage, so the verdict survived — but it survived on evidence rather than on a gap.

**Company IR feeds: reversed from REJECTED.** The first pass rejected all IR feeds outright without reading the terms of the largest live Japanese feed it had itself verified. The second pass read them and overturned the rejection.

Nine of the remaining ten verdicts were confirmed, but **eight of them came back with corrections**: terms pages read only in part, access claims that did not reproduce, a rate limit stated as fact that was never published. The pattern across this review and the market-data one before it is consistent — first passes stop at the first page that answers the question, and the answer is often on the second.

---

## Live verification

Four credential-free feeds were fetched, then refetched conditionally.

| Source | HTTP | Items | Newest | Conditional refetch |
|---|---|---|---|---|
| 日本銀行 | 200 | 56 | 資金循環統計（速報） | **200** — sends no ETag or Last-Modified |
| Federal Reserve | 200 | 20 | FOMC statement | **304** |
| White House | 200 | 30 | Presidential action | **304** |
| 内閣府 ESRI | 200 | 10 | 機械受注統計調査報告 | 200 — weekly-scale feed |

Two things came out of running it that review had not produced.

**The Bank of Japan sends no cache validators.** No ETag, no Last-Modified. A polite collector cannot get a 304 from it, so it will re-download the feed every poll and must deduplicate on content instead. That is a fact about the source, recorded rather than worked around.

**The parser had a real bug.** The Cabinet Office feed is RSS 1.0 (RDF), which puts `title`, `link` and `description` in the RSS 1.0 namespace. The parser searched for the bare names, found nothing, and returned zero items — indistinguishable from a day on which the agency published nothing. Fixed, with a regression test built from the live payload's shape.

The knowledge lag the run recorded is worth keeping in view: the FOMC statement was published at 18:00 UTC and became knowable to this system at 04:58 UTC the next day, eleven hours later, because no collector was running. `available_to_model_at` records that honestly rather than back-dating it to publication.

---

## Rejected, and kept rejected

Both rejected sources are registered in `news.sources` with `enabled = false` and a policy whose permissions are all `PROHIBITED`. Any collector that reaches for them gets a `LicenseViolation` naming the clause.

| Source | Why |
|---|---|
| **TDnet** | robots.txt disallows the host; the pages prohibit 複製 without TSE permission. Either alone would decide it. |
| **JPX news** | 「有料・無料を問わずJPXからの許諾を得ている場合を除き、商用目的によるデータ収集のほか如何なる用途に関わらず二次利用及び再配信はできません」 — refused regardless of purpose. The English translation narrows this to commercial purposes; the same page states the Japanese text prevails. |

The JPX case is worth keeping as a pattern: **robots.txt permits the crawl and the terms refuse the keeping.** They are different questions and this source answers them differently, which is why `robots_allows_path` and `full_text_storage_allowed` are separate columns.

---

## Open questions

Neither blocks development; both are recorded rather than assumed away.

1. **Whether TSE would license an individual for private non-redistributed storage.** The clause prohibits 複製 done 無断, which implies permission is obtainable — but no published procedure, price or eligibility criterion for a personal licence exists anywhere on jpx.co.jp. The only surfaced route is the corporate contract system. This is the same shape as D-102 and would go in the same written enquiry.
2. **東証上場会社情報サービス** (`www2.jpx.co.jp`), the free ten-year archive of the same documents, publishes no robots.txt and its own terms were not read. Its position is genuinely unknown, not assumed favourable.
