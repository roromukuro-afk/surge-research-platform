# Phase 2.1 Provider API 仕様の確定記録（2026-09-17）

Phase 2.0 の Provider 選定に続き、**実装に必要なワイヤ仕様**を公式ドキュメントのみで確定させた。各対象は独立した検証エージェントが同じ公式ページを再取得し、フィールド名を一字ずつ照合している。**5件すべてで検証側が誤りまたは truncation を検出した**ため、以下は訂正後の内容である。

キーの要らない ECB と OpenFIGI は**実際に呼んで測定**した。J-Quants と EODHD は無料プランでもキーが必須のため、ドキュメントのみ（測定できた範囲は明記）。

---

## 1. J-Quants API v2

Base: `https://api.jquants.com/v2`　認証: `x-api-key` ヘッダのみ（v1 のトークン方式は廃止）。

### 1-1. エンベロープ

```json
{"data": [ {...}, {...} ], "pagination_key": "value1.value2."}
```

`pagination_key` が返ったら同じ値をクエリに載せて次を取る。`/v2/equities/master` のサンプルには `pagination_key` が無い。

### 1-2. `GET /v2/equities/bars/daily` — 44 フィールド

`code` または `date` の**いずれかが必須**。`date` のみを指定すると**全上場銘柄のその日のデータ**が返る（JPX 自身が銘柄ごとのループを避けるよう明記）。日付は `20210907` と `2021-09-07` の両形式を受け付ける。

| 群 | フィールド |
|---|---|
| 識別 | `Date`（YYYY-MM-DD）, `Code` |
| 調整前 | `O`, `H`, `L`, `C`, `Vo`（取引高）, `Va`（取引代金） |
| 制限値 | `UL`, `LL`（**文字列** "0"/"1"） |
| 調整 | `AdjFactor`, `AdjO`, `AdjH`, `AdjL`, `AdjC`, `AdjVo` |
| 前場（**Premium 限定**） | `MO` `MH` `ML` `MC` `MUL` `MLL` `MVo` `MVa` `MAdjO` `MAdjH` `MAdjL` `MAdjC` `MAdjVo` |
| 後場（**Premium 限定**） | `AO` `AH` `AL` `AC` `AUL` `ALL` `AVo` `AVa` `AAdjO` `AAdjH` `AAdjL` `AAdjC` `AAdjVo` |
| その他 | `MktCap`（百万円、ETF/ETN と無取引日は null）, `ExRT`（1 分割 / 2 併合 / 3 ライツイシュー、該当なしは null） |

**罠**:
- ドキュメントは全フィールドを "Required" と書くが、脚注は Premium 以外では M*/A* の**キー自体が存在しない**と言い、`MktCap`・`ExRT` は null になると言う。ここでの "Required" は「文書化されている」であって「常に存在する」ではない。
- 公式サンプルの `Code` は**5桁文字列** `"86970"`。4桁を指定すると、普通株と優先株の両方が上場している銘柄では**普通株だけが返る**（優先株が黙って落ちる）。
- `Vo` は整数ではなく浮動小数（`2202500.0`）。
- 調整済み系列は分割のたびに**遡って再計算**される（遡及期間に上限なし）。

### 1-3. `GET /v2/equities/master` — 14 フィールド

`code` / `date` とも任意。両方省略すると実行日時点の全銘柄。

`Date`, `Code`, `CoName`, `CoNameEn`, `S17`, `S17Nm`, `S33`, `S33Nm`, `ScaleCat`, `Mkt`, `MktNm`, `Mrgn`, `MrgnNm`, **`ProdCat`**

**`ProdCat`（2026-05-26 追加）が Phase 1 の宿題を直接解く**: 011 内国株券 / 012 優先出資証券 / 013 REIT / 014 ETF / 021 外国株券 / 022 外国REIT / 023 外国ETF / 024 外国株預託証券。Phase 1 は種別を**銘柄名から推定**していた（D-56・D-70）。これは JPX が種別そのものを宣言している。**ただし普通株と優先株式・種類株式の区別は ProdCat では付かない**（どちらも 011）ので、既存の銘柄名判定を置き換えるのではなく**突き合わせる**。

市場区分コード `Mkt`: 0101 / 0102 / 0104 / 0105 / 0106 / 0107 / 0109 / 0111 / 0112 / 0113。**0103・0108・0110 は存在しない**（連番と仮定しない）。33業種コードは `0050` のようにゼロ埋めがあるため**文字列として扱う**。

### 1-4. 測定できたこと

キー無し → HTTP 403 `{"message": "The api key is required."}`、無効キー → 403 `{"message": "The incoming api key is invalid or expired."}`（AWS API Gateway 経由）。**データ本体は測定できていない。**

---

## 2. EODHD

認証は `api_token` クエリパラメータのみ（公式 OpenAPI の `securitySchemes` が `in: query` と定義）。ヘッダ形式は存在しない。

### 2-1. 調整基準（本プロジェクトで最重要）

`/api/eod/{SYMBOL}` の公式記述:

| フィールド | 基準 |
|---|---|
| `open` / `high` / `low` / `close` | **RAW（無調整）** |
| `adjusted_close` | 分割・配当の**両方**で調整 |
| `volume` | **分割調整済み** |

同じ行の価格と数量が**別の株数基準**にある。`market.daily_bars` が列ごとに basis を持つのはこのため。

### 2-2. `/api/eod-bulk-last-day/US`

JSON: `code`, `exchange_short_name`, `date`, `open`, `high`, `low`, `close`, `adjusted_close`, `volume`, `prev_close`, `change`, `change_p`
CSV: `Code, Ex, Date, Open, High, Low, Close, Adjusted_close, Volume`（9列、JSON の後ろ3つが落ちる）

`filter=extended` は列を**追加**する（置き換えない）。`type=splits` / `type=dividends` に切り替えると別スキーマになり、**命名規則も変わる**（価格は snake_case、配当は camelCase、splits は venue を `exchange` と呼ぶ）。bulk の配当は金額が**文字列**、`/api/div` では数値。

### 2-3. 分割比率のパース

`split` は `"新株/旧株"` の文字列。**小さな整数の組と仮定してはならない**。実例: GE `104.000000/100.000000`、Ford `1748175.000000/1000000.000000`、BMW `71371.000000/2745.000000`。スラッシュで分割して両方を数値として割る。分子だけを読む実装は調整係数を壊す。

### 2-4. 未解決の矛盾（`/api/id-mapping`）

公式ドキュメント2つが**両立しない**:

| 出典 | レスポンス形 |
|---|---|
| 散文ページ | `{"meta":{...},"data":[{"symbol","isin","figi","lei","cusip","cik"}],"links":{...}}` |
| 公式 OpenAPI（`https://eodhistoricaldata.github.io/EODHD-openapi/openapi.yaml`） | 裸の配列 `[{"Code","Exchange","Name","ISIN","FIGI","LEI","CUSIP","CIK"}]` |

どちらでもない側に実装すると**全列が黙って null になる**。Phase 2.1 では id-mapping を使わない。使う前に実レスポンスで決着させる（**D-80**）。

### 2-5. その他

- `delisted=1` は結果集合を**置き換える**（追加ではない）。
- 米国銘柄の**約29%に ISIN が無い**（主にファンドと OTC）。ISIN をキーにしない。
- `Isin` は `null` になり得る（上場廃止銘柄のサンプルでも null）。
- EOD の公表時刻は公式に記載が無い。

---

## 3. ECB（実測済み）

`https://data-api.ecb.europa.eu/service/data/EXR/D.USD+JPY.EUR.SP00.A?format=csvdata`

**両レッグが1リクエストで返る**（CURRENCY 次元がリストを受け付ける）。実測ヘッダは32列で、`KEY, FREQ, CURRENCY, CURRENCY_DENOM, EXR_TYPE, EXR_SUFFIX, TIME_PERIOD, OBS_VALUE, OBS_STATUS, ..., DECIMALS, ..., TITLE, TITLE_COMPL, UNIT, UNIT_MULT` を含む。**位置ではなく列名で読む**（ECB は列を追加できる）。

- `Last-Modified` が返る（実測 `Wed, 16 Sep 2026 13:57:55 GMT` ≒ 16:00 CET の公表）。`If-Modified-Since` を付けると **304** が返る（実測）。ETag は無い。
- `DECIMALS` は USD/EUR = 4、JPY/EUR = 2。**`DECIMALS` のコードリストは 0〜15 の16種**（3種ではない）。CHECK 制約や enum にしない。
- 非公表日（TARGET 休業日）は**行自体が無い**。値が空文字の行もあり得るので、空値はスキップして 0 と読まない。
- エラー本文は Content-Type で形が変わる（400/500 は HTML、404/406 は `application/problem+json`）。

---

## 4. OpenFIGI（実測済み）

`POST https://api.openfigi.com/v3/mapping`、本文はジョブの配列、**レスポンスは要求と同順の配列**。

各要素は `{"data":[...]}` / `{"warning": "..."}` / `{"error": "..."}` のいずれか。`data` の要素は `figi, compositeFIGI, shareClassFIGI, ticker, exchCode, securityType, securityType2, name, securityDescription, marketSector`。

- レート制限ヘッダ（実測）: `ratelimit-policy: 25;w=60`, `ratelimit-limit`, `ratelimit-remaining`, `ratelimit-reset`。キー無し 25/分・10 jobs/req、キー有り 25/6秒・100 jobs/req。
- **株式クラスの表記はスラッシュ**。実測: `BRK/B` は解決、`BRK.B` と `BRK-B` は `No identifier found.`。SEC は `BRK-B` と書くので変換が要る。
- 上場廃止銘柄は `includeUnlistedEquities: true` が必須。実測: `TWTR` は付ければ解決。
- **`FRCB` は2社を返す**（FIRST CONTL BANCSHARES と FIRST REPUBLIC BANK/CA）。ticker 経由のマッピングは一意ではない。
- ライセンスは**再配布まで明示的に許諾**されている（"including redistribution of the FIGI Identifiers to your customers for their use"）。

---

## 5. Cloudflare R2

**条件付き書き込みは公式にサポートされている**。S3 互換表の PutObject 行に `If-Match` / `If-Modified-Since` / `If-None-Match` / `If-Unmodified-Since` がすべて ✅。リリースノートが `If-None-Match` の**ワイルドカード `*`** 対応を明記しており、これが write-once の形。

- 衝突時のステータスは **412 PreconditionFailed**（エラーコード 10031）。R2 は AWS の 409 ConditionalRequestConflict を返さない。
- Versioning と S3 Object Lock は**未実装**。Bucket Lock（Cloudflare 独自 API）が代替。
- `UploadPartCopy` には条件付きヘッダが**無い**。
- Jurisdiction 付きバケットは `https://<account>.eu.r2.cloudflarestorage.com` のような**専用エンドポイント経由でしかアクセスできない**（クライアントを jurisdiction ごとに分ける必要がある）。本プロジェクトは jurisdiction 無し。
- `boto3` 設定: `endpoint_url=https://<account>.r2.cloudflarestorage.com`, `region_name="auto"`。

---

## 6. 本書の限界

- J-Quants と EODHD の**実レスポンスは測定していない**。フィールド名はすべてドキュメント由来である。smoke test が実際に返ったキー一覧を出力するので、契約直後にここを更新する。
- 料金・規約・仕様ページは予告なく変わる。本書は 2026-09-17 時点。
