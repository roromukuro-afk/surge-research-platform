# D-103-EOD: Alpaca への保存可否の書面照会（送信用ドラフト）

状態: **ユーザー送信待ち** — 2026-09-17
関連: [alpaca-persistence-terms-2026-09-17.md](alpaca-persistence-terms-2026-09-17.md)、[D-178](../unresolved-decisions.md)

## なぜ照会が要るのか

Alpaca の Terms and Conditions は、market data を Content に含めたうえで

> "Content is provided exclusively for personal and noncommercial access and
> use. No part of the Service or Content may be copied, reproduced, republished,
> uploaded, posted, publicly displayed, encoded, translated, transmitted or
> distributed in any way (including "mirroring") to any other computer, server,
> web site or other medium **for publication or distribution or for any
> commercial enterprise**, without Alpaca's express prior written consent."

と定めている。個人・非商用の利用は**明示的に許諾**され、禁止は公開・配布・商業という
**目的に係っている**。本件（本人1名の非公開 DB）はそのどちらでもない。

しかし **private database という語はどこにも無い**。
本プロジェクトの規則では沈黙は同意ではないので、現在の判定は `NOT_SPECIFIED` であり、
それは「保存してよい」でも「してはいけない」でもない。**問い合わせて確定させる。**

回答が出るまでの運用は実装側で固定してある:

- `credential_smoke()` は **fetch → validate → report → discard**。保存は 0 件。
- `MarketIngestJob` は `PersistenceDecision` を必須引数で受け取り、
  `ALLOWED` 以外では**最初の 1 バイトを書く前に**例外で止まる。
- `market.provider_role_bindings` の Alpaca 行は `enabled = false`。

## 送信先

Alpaca Support（`support@alpaca.markets` またはサポートフォーム）。
Market Data の利用条件に関する照会である旨を件名に明記する。

## 件名

Market Data: permitted storage of historical SIP bars for personal research

## 本文（このまま送れる）

```
Alpaca Support

I would like to confirm what your Terms permit with respect to storing
historical market data, before I begin using the Market Data API.

ABOUT ME
I am one individual person, not a company or an organisation.
Everything described below is for my own personal, non-commercial
investment research. Nobody else has access to it.

WHAT I WOULD LIKE TO DO
1. Call the historical bars endpoint (/v2/stocks/bars) with
   feed=sip and an `end` parameter that is always more than
   15 minutes in the past, as your Market Data FAQ describes for
   accounts without a subscription.
2. Store the returned daily OHLCV values in a private database on
   my own infrastructure, which only I can access.
3. Compute technical features from those stored values.
4. Use those features to screen securities for my own investment
   decisions.
5. Check afterwards whether my own past decisions were correct, by
   comparing them against the stored price series.

WHAT I WOULD NOT DO
1. I would not provide the data, or any copy of it, to any third party.
2. I would not publish, redistribute or display the data publicly.
3. I would not sell it, or provide it as part of any product or service.
4. I would not use it for any commercial enterprise.
5. I would not present the data as coming from anywhere other than Alpaca.

MY QUESTIONS
(a) Is storing the data in a private database, as described above,
    permitted under your Terms and Conditions?

    I ask because your Terms grant "personal and noncommercial access
    and use" and prohibit copying "for publication or distribution or
    for any commercial enterprise", and I could not find any statement
    about keeping the data in a private, non-public database.

(b) If it is permitted, are there conditions I should observe - for
    example a limit on how much history I may keep, a retention period,
    or a requirement to delete the data if I close my account or stop
    using the Market Data API?

(c) Does the answer differ between the free Basic plan and a paid plan?

(d) For the delayed SIP data available without a subscription, am I
    subject to the NASDAQ or NYSE subscriber agreements? Your Terms
    mention agreeing to those when selecting the Pro plan, and I want
    to be sure whether they also apply to Basic.

I would be grateful for a written answer I can keep on file.

Thank you,
(name)
(email)
```

## 回答の扱い

| 回答 | 次の行動 |
|---|---|
| (a) 許可 | `private_persistence_allowed = ALLOWED` を新しい `policy_version` で記録し、回答文面を `docs/research/` に保存。role binding を有効化し、`PersistenceDecision` に `ALLOWED` を渡す |
| (a) 条件付き | 条件を満たす形だけを実装する。保持期間の指定があれば purge の lifecycle に入れる（`market.purge_requests` が既にある） |
| (a) 不可 | `PROHIBITED` として記録。**US EOD は Alpaca 以外を探す**。adapter は残す（規約が変われば使える） |
| (d) 取引所規約が Basic にも及ぶ | その規約の warehousing 条項を読み、必要なら再度照会する |
| 回答なし | **沈黙は同意ではない。** `NOT_SPECIFIED` のまま据え置き、smoke 以上のことはしない |

## この回答を待つ間に止めないこと

adapter・contract test・normalisation・`venue_basis` の guard は完成しており、
回答は**何を保存してよいか**だけを決める。
D-103-LIVE（判断時点で取引可能だった価格）は別の未解決問題で、
この回答では解決しない。
