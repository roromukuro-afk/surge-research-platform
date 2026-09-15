# Initial Tradable Universe 定義 — `universe-1.0.0`

状態: **Phase 0.1 監査指摘 #10 による定義（下位の未決事項あり）** — 2026-09-15
版管理: 定義を変えるときは本ファイルを編集せず、`universe-definition-v1.1.0.md` 等を新規作成する。`universe_version` を Universe 判定結果・候補・Prediction に保存する。

3,000円 Hard Filter は Universe 定義とは別の段階で適用する（[entry-and-episode-lifecycle.md](entry-and-episode-lifecycle.md) §5）。

## 1. 日本株（JP）

### 含める
- 東証 **プライム / スタンダード / グロース** に上場する **普通株**

### 原則除外
- ETF / ETN
- REIT（インフラファンド等を含む投資法人系）
- 優先株
- ワラント / ユニット / ライツ
- TOKYO PRO Market

### 判定に使う公式データ（2026-09-15 確認）
| 項目 | ソース | 確認済み事項 |
|---|---|---|
| 市場区分 | J-Quants `GET /v2/equities/master` の `Mkt` | 公式コード表: `0111` プライム、`0112` スタンダード、`0113` グロース、`0105` TOKYO PRO MARKET、`0109` その他（旧区分 0101/0102/0104/0106/0107 も表に存在） |
| 商品区分 | 同 `ProdCat`（商品区分コード） | **コード値の一覧ページを確認できていない**（D-10b） |
| 更新 | 同 API | 翌営業日時点の銘柄情報は 17時半以降に取得可能（公式記載） |

判定式（暫定）: `Mkt IN ('0111','0112','0113')` かつ `ProdCat が普通株を示す値`（値は D-10b で確定）。

### 未決（D-10b）
- `ProdCat` の値と「普通株」の対応
- 東証の上記3市場に上場する**外国株式**の扱い
- 出資証券・優先出資証券などの扱い
- `0109 その他` に含まれる銘柄の中身

## 2. 米国株（US）

### 含める
- **NYSE / Nasdaq / NYSE American** 上場の **Common Stock**
- **適格 ADR**（「適格」の定義は D-10a）

### 原則除外
- ETF
- REIT
- Preferred
- Warrant / Unit / Right
- OTC
- **pre-merger SPAC**
- 上記3取引所以外にのみ上場する銘柄（NYSE Arca・Cboe BZX・IEX など。D-10d で確認）

### 判定に使う公式データ候補（2026-09-15 確認）
| 項目 | ソース | 確認済み事項 |
|---|---|---|
| 取引所・ETF/テスト銘柄フラグ | Nasdaq Trader `nasdaqlisted.txt` / `otherlisted.txt` | ETF フラグ、Test Issue フラグ、Financial Status、取引所コード（A = NYSE MKT、N = NYSE、P = NYSE ARCA、Z = BATS、V = IEXG）。優先株/ワラント/ユニットの明示的な種別列はない |
| 証券種別 | Massive All Tickers（`type`、`exchange`、`active`、`delisted_utc`） | `type` の値一覧は Ticker Types API で取得する仕様。**値一覧は未確認**（API キーが必要）。上場廃止銘柄は `active=false` で取得可能 |
| pre-merger SPAC | SEC EDGAR submissions の SIC コード | SEC 公式 SIC 表で **6770 = BLANK CHECKS** を確認 |
| REIT | SEC SIC コード | SEC 公式 SIC 表で **6798 = REAL ESTATE INVESTMENT TRUSTS** を確認。ただし SIC だけで全 REIT を網羅できるかは未確認（D-10c） |

### 未決
- D-10a: 適格 ADR の定義
- D-10c: REIT の判定方法（SIC 6798 以外の REIT の拾い方）
- D-10d: NYSE Arca / Cboe BZX / IEX にのみ上場する普通株を除外してよいか
- SPAC 合併完了後の銘柄を、SIC の変更前でも普通株として含める方法

## 3. 状態による除外（本定義では未規定）

売買停止、監理/整理銘柄、Nasdaq の Financial Status が異常な銘柄、テスト銘柄の扱いは監査指示に含まれていないため、**本版では除外条件に入れていない**（テスト銘柄は実在の株式ではないので除外）。追加する場合は D-10e で決め、`universe-1.1.0` とする。

## 4. 保存

| 保存先 | 内容 |
|---|---|
| Postgres `ref.universe_definitions` | universe_version、定義ファイルのパスと SHA-256、有効期間 |
| Postgres `universe.current_eligibility` | 最新判定（`universe_version`、含める/除外、除外理由コード） |
| Parquet `curated/universe_eligibility` | 全履歴 |

除外理由コード（案）: `NOT_TARGET_MARKET` / `NOT_COMMON_STOCK` / `ETF` / `REIT` / `PREFERRED` / `WARRANT` / `UNIT` / `RIGHT` / `OTC` / `SPAC_PRE_MERGER` / `TOKYO_PRO` / `TEST_ISSUE` / `DELISTED` / `TYPE_UNKNOWN` / `PRICE_ABOVE_3000` / `PRICE_MISSING` / `FX_MISSING`

`TYPE_UNKNOWN`（種別が判定できない）は黙って含めず、件数をカバレッジに記録する。
