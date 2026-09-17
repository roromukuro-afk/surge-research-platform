# D-103: Alpaca を分割した理由と、保存の可否

状態: **adapter 実装済み / 保存条項は NOT_SPECIFIED** — 2026-09-17
関連: [D-103-EOD / D-103-LIVE](../unresolved-decisions.md)、`workers/src/surge/providers/alpaca_historical.py`

## 前回の結論を訂正する

2026-09-17 の最初の調査で「Alpaca Basic は不適格」と報告した。
**この結論は誤りだった。正確には、二つの問いを一つの id で扱っていた。**

誤っていなかった部分: **latest 系エンドポイントの無料枠は IEX のみ**。
Alpaca の FAQ が明記し、同 FAQ の実例で AAPL 1日 IEX 12,630 約定 /
全取引所 535,134+ 約定。Outcome のパス解決はセッション高値・安値に対して
到達順序を判定するので、IEX だけの高値では +20% 到達を系統的に取りこぼす。

見落としていた部分: **historical 系エンドポイントは別の扱い**。同じ FAQ が書いている。

> "For historical queries, the `end` parameter must be at least 15 minutes old
> to query SIP data without a subscription."
> — [Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)

> "All the latest endpoints (including the snapshot endpoint), require a
> subscription to be used with the SIP feed."

つまり **15分より前で終わる窓であれば、無料で consolidated tape（SIP）が読める**。
これは Outcome Engine が必要とする系列そのものである。

## したがって D-103 を分割する

| id | 問い | 状態 |
|---|---|---|
| **D-103-EOD** | US の consolidated な日次 OHLCV を 0 円で取得できるか | **Alpaca delayed SIP で解ける見込み。adapter 実装済み** |
| **D-103-LIVE** | 判断時点で実際に取引可能だった価格を 0 円で取得できるか | **未解決。Alpaca Basic では解けない** |

15 分遅れの系列は `entry_reference_price` になり得ない。
Outcome 用データと Entry 用データが同一 provider である必要はないので、
片方が解けたことは他方の解決を意味しない。
`capabilities()` に `REALTIME_DECISION` を含めず、
`assert_role_allowed` が latency class で拒否するようにしてある。

## adapter の設計上、譲っていない点

**`feed=sip` を必ず明示する。** ドキュメント上の既定値は現在 `sip` だが、
既定値が契約に応じて解決される種類のものは、無料キーで静かに `iex` になり得る。
`bars_url()` は `feed != "sip"` を例外にし、`to_canonical_bars()` は
**レスポンス側でも**もう一度確認する。リクエストで固定したことと
レスポンスが何であったかは別の事実だからである。

**`adjustment=raw` を明示する。** 3,000円 Hard Filter も Outcome も
「実際に取引された価格」で判定する（CLAUDE.md 1-9, 1-16）。

**15分の窓をリクエスト前に検査する。** API の 403 ではなく、
呼び出し側の言葉で理由を述べる。時計ずれのため 1 分の余裕を足している。

**`venue_basis` を bar に持たせた。** `CONSOLIDATED_SIP` /
`SINGLE_VENUE_IEX` を区別し、`assert_may_resolve_an_outcome()` が
whole-market でない bar を Outcome パスへ渡すことを拒否する。
IEX の調査結果を**メモではなく構造**にした。取りこぼした到達は
「上がらなかった銘柄」と見分けがつかず、後から気づく手段が無いためである。

**`asof` を渡せるようにした。** Alpaca は既定で当日時点のシンボル解決を行うので、
その後別の会社に再割当てされた ticker は別会社の履歴を返す（CLAUDE.md 1-10b）。

## 保存（persistence）の条項

監査の指示どおり、terms / subscriber agreement まで読んだ。

### 決め手となる条項（Alpaca Terms and Conditions、原文）

> "Content is provided exclusively for personal and noncommercial access and
> use. No part of the Service or Content may be copied, reproduced, republished,
> uploaded, posted, publicly displayed, encoded, translated, transmitted or
> distributed in any way (including "mirroring") to any other computer, server,
> web site or other medium for publication or distribution or for any commercial
> enterprise, without Alpaca's express prior written consent."

同文書は Content の定義に
"market data such as quotations for securities transactions and/or last sale
information for completed securities transactions" を明示的に含めている。

### 読み方

- 「personal and noncommercial access and use」は**明示的に許諾**されている。
- 禁止は「publication or distribution or for any commercial enterprise」という
  **目的に係っている**。本人のみがアクセスする非公開 DB は、公開でも配布でも
  商業でもない。
- しかし **private database という語はどこにも無い**。

さらに Alpaca のサポートページは端的に:

> "Unfortunately, you cannot redistribute Alpaca API data."
> — [Can I redistribute Alpaca API data via my platform?](https://alpaca.markets/support/redistribute-alpaca-api)

また Terms は、NASDAQ OMX / NYSE の subscriber agreement に同意することを
**「Pro プランを選択した場合」**として定めている。
Basic の遅延 SIP が取引所側の warehousing 条項を引き継ぐのかは**書かれていない**。

### 判定

| 行為 | 判定 |
|---|---|
| 再配布 | **PROHIBITED**（明示） |
| 公開表示 | **PROHIBITED** |
| 第三者提供 | **PROHIBITED** |
| 商用利用 | **PROHIBITED** |
| **非公開 DB への保存** | **NOT_SPECIFIED** |

`market.provider_license_policies` に
`private_persistence_allowed` 列を新設して記録した。
表示・再配布・商用利用はそれぞれ列があったのに、
**「そもそも保存してよいか」だけ列が無かった**。
保存しているすべての行が依存している許諾である。

### したがって何をするか

監査の指示どおり **credential smoke までは進め、large historical ingest は開始しない**。
これをコメントではなく guard にした:

```
SMOKE_MAX_SYMBOLS = 5
SMOKE_MAX_SESSIONS = 10
assert_smoke_sized(...)  -> PersistenceTermsUnconfirmed
```

`market.provider_role_bindings` の `EOD_CURRENT_US` / `EOD_HISTORY_US` は
**enabled = false** で登録した。有効化が、履歴の蓄積を始める行為そのものだからである。

## 残りの確認項目

| 項目 | 結果 |
|---|---|
| free account eligibility | 口座要件は US 居住者向けに明記。非 US は KYC 書類が要る |
| Japan resident availability | **公式には国別リストが無い**。support へ照会する形 |
| IEX / SIP availability | **確定**。latest は有料、historical は 15分遅れなら無料で SIP |
| historical EOD coverage | 2016 年以降 |
| persistence terms | **NOT_SPECIFIED**（上記） |
| rate limits | historical 200 req/min |
| corporate actions | 未確認。ingest を始める前に確認する |
| delisted coverage | 未確認。同上 |

## ユーザー作業（credential が必要になった時点で）

**API key をチャットに貼らない。`.env.local` に置く。**

1. Alpaca でアカウントを作る（Paper trading のみでも market data key は発行される）。
2. 発行された Key ID と Secret Key を、リポジトリの外の `.env.local` に置く。
   変数名は `APCA_API_KEY_ID` と `APCA_API_SECRET_KEY`（Alpaca の公式名）。
3. 次の 1 コマンドで疎通と feed を同時に確認する。
   成功すれば `venue_basis: CONSOLIDATED_SIP` の bar が数本返る。
   401 なら key、403 なら feed か窓の指定が問題。

```bash
cd workers && python -m pytest tests/test_alpaca_historical.py -q
```

（上はネットワークを使わない contract test。実 key での smoke コマンドは
adapter の `fetch_bars` を使うが、**保存条項が NOT_SPECIFIED の間は
`assert_smoke_sized` の上限内でしか実行しない**。）

4. 居住地に関する書面確認は、Alpaca を正式採用する場合にのみ必要になる。
