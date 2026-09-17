# Live Activation: provider 調査（2026-09-17）

対象: D-103（US 価格）/ D-32（実 Analysis Provider）/ D-06b（JP 場中価格）
すべて**公式情報のみ**で確認した。第三者ブログ・フォーラムは裏取りの入口として
だけ使い、結論は一次情報に当たり直している。

---

## 1. D-103 — US 価格 / Alpaca

### 2026-09-17 訂正: **一つの id で二つの問いを扱っていた。**

当初「Alpaca Basic は不適格」と結論したが、これは latest 系エンドポイントについてのみ正しい。
historical 系は別扱いで、**end が15分以上前なら subscription なしで `feed=sip` が使える**
（Alpaca Market Data FAQ)。SIP は consolidated tape であり、Outcome Engine が必要とする系列そのもの。

したがって D-103 を分割した。

| id | 問い | 状態 |
|---|---|---|
| **D-103-EOD** | US の consolidated 日次 OHLCV を 0 円で取れるか | **Alpaca delayed SIP。adapter 実装済み・未 live** |
| **D-103-LIVE** | 判断時点で取引可能だった価格を 0 円で取れるか | **未解決。Basic の real-time は IEX のみ** |

詳細と一次情報、保存条項（`private_persistence_allowed = NOT_SPECIFIED`）、
adapter の設計判断は [alpaca-persistence-terms-2026-09-17.md](alpaca-persistence-terms-2026-09-17.md)。

**IEX の件は撤回していない。** 無料の real-time は IEX のみで、
AAPL 1日 IEX 12,630 約定 / 全取引所 535,134+ 約定。
セッション高値をこれで判定すると +20% 到達を系統的に取りこぼす。
だから `venue_basis` を bar に持たせ、whole-market でない bar が
Outcome パスへ渡ることを `assert_may_resolve_an_outcome()` が拒否する。

### 代替の調査状況

| 候補 | 状態 |
|---|---|
| **Alpaca delayed SIP** | **D-103-EOD の第一候補。** 保存条項が NOT_SPECIFIED のため smoke 止まり |
| Tiingo 無料 | **不可**。"Unique Symbols per Month: 500"。US universe に桁が足りない |
| Stooq bulk | **未確認**。規約原文をこの環境から取得できていない |
| EODHD | 既に `OPTIONAL_PAID` へ降格済み（Zero-Cost Core） |
| Yahoo Finance | 規約上の自動取得禁止により対象外 |

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

### 2026-09-17: adapter を実装した（調査だけで止めない）

`workers/src/surge/analysis/groq_provider.py`:

- `GroqHostedProvider`、`ProviderKind.HOSTED_LLM`
- **model は configurable**（`GROQ_MODEL`）。特定 model をコードへ固定しない
- Structured Outputs: `response_format` の `json_schema`、
  documented な model では `strict = true`。strict でない model では
  `strict = false` に落として**その事実を記録する**
- Structured Outputs を使っても**既存 validator を必ず通す**。
  schema が通ることと、答えが規則に反しないことは別である
- **data policy をデータとして持つ。** `InputUse` は 4 値
  （NOT_USED_FOR_TRAINING / USED_FOR_TRAINING / NOT_SPECIFIED / UNKNOWN）で、
  `NOT_USED_FOR_TRAINING` 以外には Canonical v5.1 を**送らない**。
  Gemini 無料 tier の policy も並べて持たせてあり、これは実行時の guard として効く
- ZDR は `zero_data_retention_available = true` /
  `zero_data_retention_enabled = None`。
  **有効かどうかはアカウントの事実であり、このコードはアカウントを見たことがない。**
  Production 推奨設定は「最初の本番リクエストの前に Data Controls で ZDR を有効化」

### free-tier capacity preflight

**Canonical v5.1 を勝手に短縮しない。bundle を勝手に削らない。**
これを構造にした: `preflight()` に truncation 引数が**存在しない**。

- token 見積りは CJK と Latin を分けて数える。
  `len/4` 方式では日本語の canonical prompt を実寸の 1/4 に見積もってしまう
- **アカウントの実 quota を未測定のうちは preflight を通さない。**
  ドキュメント記載値で通しておくと、通らなくなる瞬間まで通り続ける
- 超過時は `D-32_BLOCKED_FREE_QUOTA` を返す。prompt は削らない

### ユーザー作業（credential）

Groq の無料 tier は**クレジットカード不要**と各所で説明されている
（これは三次情報なので、実際の登録画面で確認いただく）。
**key はチャットに貼らない。`.env.local` へ置く。**

1. Groq Console でアカウントを作り、**Data Controls で Zero Data Retention を有効化**する。
2. API key を作成し、`.env.local` に `GROQ_API_KEY=...` として置く。
3. 使う model を `.env.local` に `GROQ_MODEL=...` として置く。
   strict な Structured Outputs を使うなら
   `openai/gpt-oss-120b` か `openai/gpt-oss-20b`。
4. 疎通と契約適合をまとめて確認するのは次の 1 コマンド。
   ネットワークを使わない contract test なので、key が無くても通る:

```bash
cd workers && python -m pytest tests/test_groq_provider.py -q
```

5. 実 key での 1 回目のリクエストでは、**レスポンスヘッダの rate limit を読み取り**、
   `Quota` に実測値を入れる。それまで preflight は通らない（意図的）。

---

## 3. D-06b — JP 場中価格

### 2026-09-17 訂正: **「条項の確認待ち」ではない。既に明示の禁止がある。**

**CURRENT VERDICT = PROHIBITED / NEEDS_WRITTEN_EXCEPTION**

立花証券・ｅ支店・ＡＰＩ専用ページ「３．ご利用方法」７．に、原文で:

> 当社提供情報は、蓄積、編集および加工等は禁止しております。

対象は「当社提供情報」であってニュースに限られない。
同ページが参照するインターネット取引規定 第18条には「蓄積」の語が無く、
禁止は第三者提供・営業使用・第三者提供目的の加工および再利用・
本人の証券投資以外の目的、という**目的による限定**の形をとる。
**二つの文書が食い違っている。厳しい方を採る。**

照会文は [tachibana-api-accumulation-enquiry.md](tachibana-api-accumulation-enquiry.md) に作成済み。
**口座開設を先に要求しない。** 口座を開いてから使えないと分かるのは順序が逆で、
口座開設自体がユーザー本人の契約行為でもある。

### 「価格が取れる」だけでは採らない

本システムは `decision_price` / `decision_price_observed_at` /
`entry_reference_price` / `entry_price_observed_at` を保存できなければ
Prediction を作れない（DB 制約）。蓄積を禁じる規約の下では、
0 円でリアルタイム価格が取れても Prediction は 1 件も作れない。

### `ZERO_COST JP LIVE ENTRY UNAVAILABLE` はまだ宣言しない

立花の回答が「禁止」であれば、auカブコム・楽天にも同じ形の照会文を作る。
**3 社とも不可と確定して初めて**、正式な coverage limitation として宣言する。
**有料 provider を勝手に採用しない。**

## 4. D-105（Cloudflare R2）は blocker から外す

監査の判断どおり。`ObjectStore` は provider 非依存の interface なので、
LocalObjectStore で Production を開始できる。
readiness command でも `object_store` は WARN であって gate ではない。
