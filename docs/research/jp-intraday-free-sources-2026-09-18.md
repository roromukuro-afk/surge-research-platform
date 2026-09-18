# JP 場中の entry price: 無料の source はあるか（2026-09-18）

**問い**: 東証の銘柄について、判断の直後に **event timestamp（約定/気配の時刻）付きの価格を1つ**、1日に数回（25回未満）取得し、その1件と時刻・provenance を private DB に保存できる**無料**の手段はあるか。15〜20分遅延でも、各価格が自分の event timestamp を持つなら可（判断後の最初の print を遅れて拾える。ただし遅延データの使用可否は D-21 で未決）。日次のみ・「最終取引日」だけで時刻の無い価格は不可。

**方法**: 公式ページ（料金・プラン比較・取引所一覧・API docs・規約）のみ。サブエージェントによる一次調査で、**別パスでの再検証はまだしていない**。API key の取得・呼び出しはしていない。以前の評価（[zero-cost-provider-evaluation.md](zero-cost-provider-evaluation.md)）で規約により落ちた証券会社 API 3社・Massive・Tiingo・EODHD・Stooq は再調査していない。

## 結果

| provider（無料プラン） | 東証 | event timestamp | 保存 | 判定 |
|---|---|---|---|---|
| Twelve Data Basic | ✗ XJPX は Pro 以上。Basic は試用銘柄（7203 のみ） | `/quote` は足の時刻（約定時刻ではない） | Basic は「production systems」での利用を禁止（料金ページの注記） | 不可 |
| Finnhub Free | ✗ 国際データは無料対象外、東証は有料でも EOD | — | 解約時全削除 | 不可 |
| Alpha Vantage Free | 公式に記載なし | 無料は時刻なし（quote は日次更新） | 記載なし | 不可 |
| Financial Modeling Prep Basic | 記載なし（無料は EOD のみ） | — | 書面承認なしの複製・ダウンロード禁止 | 不可 |
| Marketstack Free | 無料は EOD のみ（intraday は US/IEX のみ） | — | 無料はテスト・評価用途 | 不可 |
| J-Quants Free | 日足 12 週遅延、分足・ティックは有料アドオン（翌日提供） | — | — | 不可（CLAUDE.md 1-8 でも場中不可） |
| iTick | 無料での日本対応は記載なし（日本株プランは有料） | `t` = 最終約定時刻（ms） | 保存条項なし、無断複製禁止 | 書面確認が要る |
| Google Finance（Sheets） | 東京、20分遅延 | `tradetime`（精度不明） | 書面同意なしのダウンロード・保存禁止 | 不可 |

**結論: 無料で「東証カバー + 場中の event timestamp + 保存が禁止されていない」を満たすものは見つからなかった。** 残るのは有料（Twelve Data Pro 等）か、証券口座を前提とする API（規約で蓄積禁止、D-175）。（この時点では既存の `mea-stock-screener` の流用は CLAUDE.md 1-1 と衝突するため未確認だった。下の追記を参照。）

## 追記（同日）: 採用した経路

新しい API 探しより先に、ユーザーの既存 screener（`mea-stock-screener`）の価格取得経路を確認した（CLAUDE.md 1-1 の今回限りの例外として明示許可、個人の投資研究用途）。Yahoo の JSON API（`v7/finance/quote`、cookie + crumb、`curl_cffi` の Chrome 偽装 session。欠けた銘柄は `v8/finance/chart` の meta）で、web ページの scraping ではない。東京は約 20 分遅延と宣言、実測の遅れは約 15 分、最終約定時刻は秒精度。いったん「遅延を待ち切って判断後の最新約定を採る」形で JP の entry price source にしたが（D-258 / D-259）、同日の前提修正（D-261: Prediction は引け後、基準価格は確定終値）で不要になり削除した。現在は同じ取得ロジックで**日足の確定終値**を読む（D-262。セッション終了 + 20 分以降に読んだものだけを確定とする）。場中の source 探しはこの文書の問いごと不要になった。上表の「無料 API」の結論とは別枠で、公開・再配布しない個人利用が前提。

## US の fallback（参考）

- Finnhub Free: US のリアルタイム quote と、ms 精度の約定 WebSocket（50 銘柄まで）。IEX のみか consolidated かは公式に記載なし。個人利用、解約時全削除。
- Twelve Data Basic: US リアルタイムはあるが時刻は分足、production 利用禁止。
- Alpha Vantage / FMP / Marketstack の無料: US リアルタイムなし。
