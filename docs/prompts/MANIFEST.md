# MANIFEST — 原文ファイルの SHA-256

登録されたファイルは内容を変更しない。変更が必要な場合は新しいファイルを作り、ここに新しい行を追加する（既存行は削除しない）。
CI（Phase 1 で実装）はこの表の SHA-256 と実ファイルを照合し、不一致なら失敗する。

| ファイル | 種別 | SHA-256 | バイト数 | 受領日 | 受領経路 / 由来 |
|---|---|---|---|---|---|
| `docs/prompts/short-surge-v5.1.original.md` | プロンプト原文（Canonical） | `32bf001eeec2c6a1e1bbde7256f6cb591a18f511bcae6383a9971845579f877a` | 32012 | 2026-09-16 | ユーザーが ChatGPT 会話で再提示した Canonical 本文を ChatGPT が GitHub へ直接登録（commit `1a4f34e`）。Claude Code は実ファイルから SHA-256 とバイト数を実測。無加工（2,275行、LF のみ、post-v5.1 用語の混入なし） |
| `docs/requirements/implementation-instructions-v1.0.original.txt` | 要件原文 | `5d449cbedd9334e186d62c555e784f05e6289ea705e576c2251863c721be9f74` | 23507 | 2026-09-15 | ユーザーのチャット本文を Claude Code が転記（UTF-8、LF）。会話内の本文を正本とする（D-22） |
| `docs/requirements/audit-2026-09-15-phase-0.1.original.txt` | 監査原文 | `52b426309ab1606f7be8bfd0b54b1fd46937e3b3426379ab4721cd9b602db229` | 5010 | 2026-09-15 | 同上（D-22） |
| `docs/prompts/addenda/v5.1-addendum-2026-09-15.md` | addendum（所在一覧） | `36eecbe64debc4688c4bf35159bdbb25ebe3b79caef556b9d9b7973646de3b36` | — | 2026-09-15 | Claude Code 作成 |
| `docs/prompts/addenda/v5.1-addendum-2026-09-15-phase0.1-audit.md` | addendum（所在一覧） | `e29b8f71f872523db790107423e91f852571949c1bdcc2bd0d385a3bf21c9814` | — | 2026-09-15 | Claude Code 作成。一部は phase0.2 addendum で置き換え |
| `docs/requirements/audit-2026-09-15-phase-0.2.original.txt` | 監査原文 | `af99d00b80a0b9f1ee9609676c72805526e70ef7ef057fa97660289488bfc7a9` | 5207 | 2026-09-15 | ユーザーのチャット本文を Claude Code が転記（UTF-8、LF）。元ファイルとのバイト一致は未確認（D-22） |
| `docs/prompts/addenda/v5.1-addendum-2026-09-15-phase0.2-audit.md` | addendum（所在一覧） | `dd9724381e41038782a50456ff896c9e508ef0f51eb43193b427e1ad54d9fbf6` | — | 2026-09-15 | Claude Code 作成。一部は phase0.2-final-patch addendum で置き換え |
| `docs/requirements/audit-2026-09-15-phase-0.2-final-patch.original.txt` | 監査原文 | `26386dafdb0090c1f1833f4f86031ac14d5308167742101dd88711bc32c59ad1` | 4774 | 2026-09-15 | 会話内の本文を保存時点で登録（UTF-8、LF） |
| `docs/prompts/addenda/v5.1-addendum-2026-09-15-phase0.2-final-patch.md` | addendum（所在一覧） | `10b0b6186cfacd26fc7a68417123f58a61d314ede78c385d12c6db13b486f249` | — | 2026-09-15 | Claude Code 作成 |
| `docs/requirements/audit-2026-09-16-phase-1-start-conditions.original.txt` | 監査原文（運用・開始条件。投資ロジックの変更なし） | `5af52996f594c6af99502251bcd8db5dc3efdd7720674b98bcb308e9d16271de` | 3574 | 2026-09-16 | 会話内の本文を保存時点で登録（UTF-8、LF） |

整形版（`docs/requirements/implementation-instructions-v1.0.formatted.md`）は原文ではないため登録しない。
