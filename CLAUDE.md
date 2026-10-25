# X投稿 半自動化（x-posting/）

目的：Xで教採・臨採に関心のある読者を集め、noteの有料記事の売上を増やすこと。

## 絶対に守ること
- **承認されていない投稿は、Bufferに送らない・公開しない。** 送れるのは status が「承認」の投稿だけです（ツール側でも止めています）。
- 誇張や、虚偽の実績・数字を使わない。合格実績、販売数、読者の声は、ユーザーが事実として示したものだけを使う。
- 同じ投稿を繰り返さない。validate で、過去の投稿（投稿履歴.csv）と週内の投稿の類似度を確認する。
- Bufferのパスワードは使わない・保存しない。APIキーは環境変数 BUFFER_API_KEY で渡し、リポジトリに書かない。
- ブランドごとに `brands/<フォルダ>/` で分ける。「元教員の転職記録」とは混ぜない。

## 毎週の流れ（Claude向け）
1. `季節カレンダー.md` を確認し、今の時期の教採・臨採の話題をWebで調べて更新する（日付・制度は公式の情報で確認）。
2. `分析/実績レポート.md`（あれば）と `投稿履歴.csv` を読み、反応がよかったテーマ・種類を把握する。
3. note側の資料を参照する：`../note-writing/brands/<ブランド>/執筆スタイルガイド.md`、記事一覧、保存記事。
4. `週次/YYYY-Www.md` に7本の案を作る。内訳の目安は「情報提供5：note紹介2」。note紹介は `有料記事リスト.md` の記事だけを紹介する。
5. `validate` でエラーが0件であることを確認し、`review` の一覧をユーザーに見せて、確認・承認を依頼する。
6. ユーザーが**チャットで明示的に承認した投稿IDだけ**を `approve` する。Claudeが自分の判断で承認してはいけない。
7. `push --mode schedule --dry-run` で送る内容を見せ、ユーザーの了承を得てから `push --mode schedule` を実行し、登録結果を報告する。APIが使えない環境では `export` でコピペ用のファイルを作る。
8. 投稿が済んだら `投稿履歴.csv` に追記する。翌週、`実績.csv` と `note売上.csv` の記入をユーザーに依頼し、`report` を実行する。

## 週次ファイルの書き方
各投稿は `## ` 見出しで始め、次の項目を書きます。
- `id`：例 2026W44-1
- `datetime`：日本時間（YYYY-MM-DD HH:MM）
- `kind`：情報提供 / note紹介
- `theme`：投稿のテーマ
- `status`：未承認 / 承認 / 却下 / 登録済

本文は ```text ブロックに書きます。

## Buffer API について
- エンドポイントは https://api.buffer.com（GraphQL、`Authorization: Bearer <APIキー>`）。
- 使う操作：`account { organizations }` → `channels(input: { organizationId })` → `posts(input: { organizationId, filter: { status, channelIds } })` → `createPost(input: { text, channelId, schedulingType: automatic, mode: customScheduled, dueAt, saveToDraft })`。
- `check` で、投稿先が `buffer.json` の `expected_handle`（manabiyapro）のXチャンネルであることを確認します。一致するチャンネルがない、または複数ある場合は止まります。`push` も送る前に毎回この確認をします。
- 重複の防止：`登録記録.csv`（投稿ID・本文ハッシュ）と、Buffer上の予約・下書きの本文を照合します。
- 本物のAPIではまだ動作確認していません（模擬サーバーでのみ確認）。初回は `check` と `posts` の応答を確認し、スキーマに違いがあれば `tools/x_tool.py` の `GET_*` / `CREATE_POST` を直します。
- 無料プランは予約が1チャンネルあたり10件まで。1日1本なら1週間分（7本）が収まります。
