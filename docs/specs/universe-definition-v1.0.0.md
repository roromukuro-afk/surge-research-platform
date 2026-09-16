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

### 判定に使う公式データ（Phase 1 実装、2026-09-16 実測）

**採用ソース: JPX「東証上場銘柄一覧」（`data_j.xlsx`）の「市場・商品区分」列。** 認証不要で、市場区分と商品区分が1列で与えられるため、`Mkt` と `ProdCat` を突き合わせる必要がない。

| 区分（原文） | 件数（2026-09-16） | 判定 |
|---|---|---|
| プライム（内国株式） | 1,556 | 含める |
| スタンダード（内国株式） | 1,555 | 含める |
| グロース（内国株式） | 596 | 含める |
| ETF・ETN | 477 | 除外 `ETF` |
| PRO Market | 187 | 除外 `NOT_TARGET_SEGMENT` |
| REIT・ベンチャーファンド・カントリーファンド・インフラファンド | 63 | 除外 `REIT_OR_FUND` |
| プライム/スタンダード/グロース（外国株式） | 5 | **UNRESOLVED** `FOREIGN_STOCK_RULE_PENDING`（D-10b） |
| 出資証券 | 2 | **UNRESOLVED** `INVESTMENT_CERTIFICATE_RULE_PENDING`（D-10b） |
| 合計 | 4,441 | |

未知の区分が現れた場合は `UNKNOWN` として扱い、`TYPE_UNKNOWN` で unresolved にする（黙って除外しない）。

代替ソースとして J-Quants `GET /v2/equities/master`（`Mkt` = 0111/0112/0113、`ProdCat`）も使えるが、認証が必要で `ProdCat` のコード値一覧を公式ページで確認できていないため、Phase 1 では採用していない。

### 未決（D-10b、UNRESOLVED として可視化済み）
- 東証の対象3市場に上場する**外国株式**の扱い（現在5銘柄）
- 出資証券・優先出資証券などの扱い（現在2銘柄）

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

### 実装ノート（Phase 1.1、2026-09-16）— Depositary 系の判別

「Depositary Shares」という語は **ADR（原株が外国株）** と **優先株の分割受益証券（Depositary Shares Each Representing a 1/1,000th Interest in a Share of … Preferred Stock）** の両方で使われる。Phase 1 の実装は後者を優先株として正しく除外する一方で、**Global Depositary Shares / Depositary Receipt / NY Registry Shares** を PREFERRED に寄せていた。

Phase 1.1 で判別順を次のとおり明文化した（定義の変更ではなく、定義どおりに判別するための修正）。

1. 名称が優先株を明示している（`preferred` を含む、または「… represent … Preferred Stock」）→ `PREFERRED`
2. ADR / ADS / Global Depositary / Depositary Receipt / NY Registry → ADR として扱い、適格性は D-10a のため `UNRESOLVED` `ADR_ELIGIBILITY_UNDEFINED`
3. Depositary とだけあり原資産が読み取れない → `UNKNOWN`（`TYPE_UNKNOWN` で `UNRESOLVED`。黙って除外しない）

2026-09-16 実測の影響: 16 件が `EXCLUDED`（PREFERRED）から `UNRESOLVED` へ移動（`ADR_ELIGIBILITY_UNDEFINED` +1、`TYPE_UNKNOWN` +15）。`INCLUDED` は 4,877 のまま変わらない。

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

## 5. 下流工程での扱い（Phase 2 以降）

`UNRESOLVED` は「除外」ではない。**価格・FX の取得対象は `INCLUDED` ∪ `UNRESOLVED`** とし、`UNRESOLVED` を取得対象から黙って落とさない。

- 判定が付いてから価格を取り始めると、判定が付いた日より前の価格が欠落し、後追いで Prediction も Replay もできなくなる。
- `UNRESOLVED` の銘柄は **ENTRY 候補にはしない**（Universe に含まれていないため）。取得だけ行い、判定が `INCLUDED` に変わった時点で候補に上がる。**Prediction の対象は Eligibility が解決した `INCLUDED` のみ**（Phase 1.1a 監査 #11）。
- 取得対象の件数・内訳（`INCLUDED` / `UNRESOLVED`）は run ごとにカバレッジへ記録する。
- `EXCLUDED` は取得対象外。ただし理由コード別の件数はカバレッジに残す。
