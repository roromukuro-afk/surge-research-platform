# docs/prompts

プロンプト原文の保管場所。方針は [../prompt-versioning-policy.md](../prompt-versioning-policy.md)。

## ファイル

| ファイル | 状態 |
|---|---|
| `short-surge-v5.1.original.md` | **未受領**（会話内でユーザーが確定させた全文を受け取り次第、一字一句変更せず保存し、保存時点の SHA-256 を MANIFEST に登録する。formatting / normalization / typo correction をしない。addenda の内容を混ぜない） |
| [MANIFEST.md](MANIFEST.md) | 原文ファイル（プロンプト・要件・監査）と addenda の SHA-256 |
| [addenda/](addenda/) | v5.1 以後の確定追加仕様の所在一覧（v5.1 原文より優先） |

## 禁止事項

- 原文ファイルの要約・短縮・リライト・条件削除・配点変更・**整形**
- 整形したものを「原文」と呼ぶこと（整形版は `*.formatted.*` の別ファイル）
- 旧版の削除
- 追加仕様を原文ファイルに直接書き込むこと（必ず `addenda/` に別ファイル）
