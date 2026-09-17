# Live Activation: provider 調査（2026-09-17）

対象: D-103（US 価格）/ D-32（実 Analysis Provider）/ D-06b（JP 場中価格）
すべて**公式情報のみ**で確認した。第三者ブログ・フォーラムは裏取りの入口として
だけ使い、結論は一次情報に当たり直している。

---

## 1. D-103 — US 価格 / Alpaca

### 結論: **Alpaca Basic（無料）は本システムの用途に足りない。**

これは居住地条項とは**別の理由**で決まる。

Alpaca 自身の FAQ より:

> "Our free market data offering includes live data only from the IEX exchange"
> — [Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)

> "IEX (Investors Exchange) is a single stock exchange" / "All US exchanges are
> mandated by the regulators to report their activities (trades and quotes) to
> the consolidated tape."

同 FAQ の実例では、AAPL の 2023-09-29 で **IEX 12,630 約定 / 全取引所 535,134+ 約定**。

**なぜこれが致命的か**: 本システムの Outcome パス解決（Phase 9）は
**セッションの高値・安値**に対して target と failure line の到達順序を判定する。
IEX だけの bar の高値は「その日の高値」ではなく「IEX でついた高値」であり、
全取引所の約 2% でしかない。これで +20% 到達を判定すると、
**到達を系統的に取りこぼす**。failure line 側も同じ。

SIP（consolidated tape）は Algo Trader Plus（有料）。Zero-Cost Core により対象外。

### 監査が指示した確認項目への回答

| 項目 | 結果 |
|---|---|
| free account eligibility | 口座要件は US 居住者向けに明記。非 US は KYC 書類が要る |
| Japan resident availability | **公式には国別リストが無い**。「support へ問い合わせよ」とだけ書かれている |
| IEX / SIP availability | **確定。無料は IEX のみ。SIP は有料プラン** |
| historical EOD coverage | 2016 年以降。ただし直近 15 分は取得不可 |
| current quote / trade | Basic は IEX のみ |
| persistence terms | Market Data API ドキュメント上に保存・再配布の明示規定は見つからず（要 ToS 精読） |
| rate limits | historical 200 req/min、WebSocket 30 symbols |
| corporate actions | 未確認（Basic が不適格と判明したため打ち切り） |
| delisted coverage | 同上 |

### ユーザー作業が**減った**こと

当初 D-103 には「(b) 無料キーで `feed=sip` を1回叩いて確定する」という
ユーザー作業を置いていた。**これは不要になった。** Alpaca 自身が
無料 = IEX のみと明記しており、叩くまでもない。
居住地条項の書面確認（a）も、Basic のデータが用途に足りない以上、
**Alpaca を採る場合にのみ**必要になる。

### 代替の調査状況（未完）

| 候補 | 状態 |
|---|---|
| Tiingo 無料 | **不可**。"Unique Symbols per Month: 500"。US universe は数千銘柄あり桁が足りない |
| Stooq bulk | **未確認**。bulk の日足 CSV を無償配布しているが、利用規約の原文をこの環境から取得できなかった（ドメインがブラウザで許可されていない）。規約を読むまで採用しない |
| EODHD | 既に `OPTIONAL_PAID` へ降格済み（Zero-Cost Core） |
| Yahoo Finance | 規約上の自動取得禁止により従前より対象外 |

**現時点の US EOD は未解決。** Alpaca Basic が候補から落ちたことで、
D-103 は「Alpaca の可否」ではなく「US の consolidated EOD を 0 円で
合法に保存できる経路があるか」という問いに戻った。

---

## 2. D-32 — 実 Analysis Provider

### 結論: **Groq の無料 developer tier が第一候補。** ただし credential が要る。

判断軸は性能ではなく **入力の扱い**にした。Stage 3 の入力 bundle には
Canonical v5.1（ユーザーの投資手法そのもの）が含まれる。
これを学習素材にされるかどうかは、性能差より重い。

#### Groq（一次情報で確認）

Services Agreement より:

> "Groq is not permitted to use Inputs or Outputs for training or fine-tuning
> any AI Model Services or other models, unless explicitly granted permission or
> instructed by Customer."

> "As between the parties, Customer retains all Intellectual Property Rights in
> Customer Data (including in Inputs and Outputs)."

データ保持（[Your Data in GroqCloud](https://console.groq.com/docs/your-data)）:

> "By default, Groq does not retain customer data for inference requests."

> "All customers may enable Zero Data Retention (ZDR) in Data Controls settings."

同ページは**無料 tier と有料 tier を区別していない** — 保持方針は
アカウント種別によらず同一と読める。

#### Google Gemini API 無料 tier（比較対象。一次情報で確認）

[Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms) より:

> "Google uses the content you submit to the Services and any generated
> responses to provide, improve, and develop Google products and services"

> "human reviewers may read, annotate, and process your API input and output"

**無料 tier では学習に使われ、人間がレビューし得る。**
（有料 tier は扱いが異なるが、有料は Zero-Cost Core により対象外。）

#### したがって

| Provider | 学習に使うか | 既定の保持 | 月額 |
|---|---|---|---|
| **Groq 無料** | **使わない（契約上）** | **しない** | 0 円 |
| Gemini 無料 | **使う。人間レビューあり** | — | 0 円 |

### 残る確認事項（credential 取得後）

1. 日本語の実出力品質（Groq が載せているのは open-weight モデル。CJK は
   モデルによって差が大きい）
2. structured JSON output の実挙動
3. 無料 tier の rate limit が日次 EOD バッチに足りるか
4. 金融分野の利用に関する記載（「financial advice に依拠するな」は
   免責であって利用禁止条項ではないと読めるが、採用前に精読する）

### ユーザー作業（credential）

Groq の無料 tier は**クレジットカード不要**と各所で説明されている
（これは三次情報なので、実際の登録画面で確認いただく）。
API key が必要になった時点で、番号付きの最小手順を別途お出しする。
**key はチャットに貼らない。`.env.local` へ置く。**

---

## 3. D-06b — JP 場中価格

### 結論: **「入手不能」ではない。「口座契約が要る」。**

| 候補 | 料金 | 実時間性 | 前提 |
|---|---|---|---|
| 立花証券 e支店 API | **0 円**（公式に「利用料金 無料 0円」） | リアルタイム株価・板 | **証券口座の開設** |
| kabuステーション API（auカブコム） | 条件付き無料 | ほぼリアルタイム | **証券口座の開設** |
| J-Quants | — | **不可**（日次更新） | CLAUDE.md 1-8 により場中利用禁止 |

いずれも**証券口座の開設**が前提で、これは停止条件 1・2・3
（契約・credential・本人確認）に該当する。したがって D-06b は
コードでは解けない。

**蓄積禁止の件を忘れていない。** 以前の調査では、立花・auカブコム・楽天の
3社とも規約上データの蓄積を禁じていた。今回、立花の API ページ自体には
保存・二次利用の記載が無く、条件は約款側にある。
**約款の該当条項を読むまで採用しない。** 口座開設が先に要る以上、
その確認はユーザーが口座を検討する時点で行うのが順序として自然。

### 「価格が取れる」だけでは採らない

監査の指摘どおり、必要なのは価格の取得ではなく
**entry / audit に必要な snapshot を合法に記録できること**である。
本システムは `decision_price` / `decision_price_observed_at` /
`entry_reference_price` / `entry_price_observed_at` を保存しなければ
Prediction を作れない（DB 制約）。蓄積を禁じる規約の下では、
価格が取れても Prediction は作れない。

### 現時点で `ZERO_COST JP LIVE ENTRY UNAVAILABLE` とは**宣言しない**

0 円の候補が実在し、残るのが契約と規約確認だからである。
約款が蓄積を禁じていることが確認できた時点で、
正式な coverage limitation として宣言する。
**有料 provider を勝手に採用しない。**

---

## 4. D-105（Cloudflare R2）は blocker から外す

監査の判断どおり。`ObjectStore` は provider 非依存の interface なので、
LocalObjectStore で Production を開始できる。
readiness command でも `object_store` は WARN であって gate ではない。
