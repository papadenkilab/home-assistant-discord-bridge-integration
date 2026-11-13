# Discord Bridge カスタム統合 for Home Assistant

Home Assistant の認証付き API 経由で Discord を使うためのカスタム統合です。新しいポートは開けません。外部のプログラム（例：Muse）は DuckDNS 等で既に公開されている Home Assistant の HTTPS アドレスに、Home Assistant の長期アクセストークンでアクセスします。

2つのモードが使えます（片方だけでも、両方でも可）。

- **直接モード**：この統合自体が Discord の REST API と通信します。Bot トークンはこの Home Assistant の中にだけ保存されます。Gateway には接続しない（REST のみ）ので、アドオンと同じトークンを使っても二重ログインになりません。
- **ブリッジモード**：宅内の Discord Bridge アドオンのキューを中継します。

## 関連プロジェクト：Discord Bridge アドオン

姉妹プロジェクトとして、Home Assistant アドオン版の **Discord Bridge** があります。

- リポジトリ：https://github.com/papadenkilab/home-assistant-discord-bridge

アドオン版は単独のコンテナとして Discord の Gateway に常駐し、許可したユーザーの発言を SQLite キューに保存します。外部のアシスタントは小さな HTTP API（ポート 8765、Tailscale 経由など）でキューを読み、返信やファイル添付、管理操作を行います。この統合のブリッジモードは、そのアドオンのキューを Home Assistant の認証付き API 越しに中継するためのものです。

### アドオン版との機能比較

| 項目 | この統合（カスタム統合） | アドオン版 |
| --- | --- | --- |
| 形態・導入 | HA 本体の中で動くカスタム統合。HACS のカスタムリポジトリ、または `custom_components` へ手動配置 | HA のアドオンとして別コンテナで動作。アドオンストアにリポジトリ登録して導入 |
| 外部からの接続先 | HA の既存の HTTPS（例：DuckDNS）に HA の長期アクセストークンで接続。**新しいポートは開けない** | アドオンの HTTP API（ポート 8765）。Tailscale 等のプライベートな経路経由が前提 |
| 認証 | Home Assistant の認証（既定では管理者ユーザーのみ許可） | 専用のキュートークン（ヘッダ `X-Queue-Token`） |
| Discord への接続 | REST のみ（Gateway には接続しない）。Bot トークンは HA 内に保存 | Gateway に常駐（WebSocket）し、発言をリアルタイムに受信。Bot トークンはアドオン設定内に保存 |
| 受信の仕組み | 履歴のポーリング取得。監視チャンネルの新着は既読位置（mark_read）で管理し、キューとは競合しない | 許可ユーザーの発言をキューに自動保存（👀 リアクション付き）。再接続時は履歴からバックフィル。受信時に添付ファイルもデータ領域へ保存 |
| 投稿・ファイル添付 | ○（添付は multipart、25MB まで） | ○（`/send`／`/send_file`、25MB まで） |
| フォーラム | 投稿の一覧・作成（タグ指定対応）、フォーラム情報の取得。**既存投稿へのタグ付け替え（v1.1.0〜）** | 送信先がフォーラムなら投稿を自動作成（タイトル指定可）、スレッド一覧の取得 |
| チャンネル管理 | 作成・変更（フォーラムのタグ付け替え含む）、チャンネル情報の取得 | 作成・編集・削除 |
| その他の管理・機能 | 投票の作成・結果取得・早期終了、サーバーの予定イベントの一覧・作成 | メッセージ削除・一括削除、ピン留め／解除、リアクション追加、スレッド作成、ロール付け外し、メンバー／ロール一覧 |
| 添付ファイルの受信 | 履歴取得で添付・埋め込み・スタンプの情報を含む（本体は Discord の URL から取得） | 受信時に本体をダウンロード保存し、API で取得可能 |
| 単体利用 | ○（直接モードのみでも動作） | ○（統合なしでも動作） |
| 併用 | ブリッジモードでアドオンのキューを中継できる | 統合のブリッジモードから中継される側になれる |

使い分けの目安：外部から HA の HTTPS だけで完結させたい、新しいポートを開けたくない場合はこの統合。発言をリアルタイムにキューへ貯めて確実に取りこぼしたくない、添付ファイルの本体ごと保存したい場合はアドオン版。両方入れて、受信はアドオン、外部向けの窓口はこの統合（ブリッジモード）という併用もできます。

## 直接モードでできること

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/discord_bridge/direct/health` | Bot 情報と参加サーバー一覧 |
| GET | `/api/discord_bridge/direct/channels?guild_id=` | サーバーのチャンネル一覧 |
| GET | `/api/discord_bridge/direct/messages?channel_id=&limit=&after=&before=` | チャンネル履歴。**添付ファイル（画像・動画）・埋め込み・スタンプも含む** |
| GET | `/api/discord_bridge/direct/new` | 監視チャンネルの「前回既読以降・対象ユーザーの発言」。既読位置は自動では進まない |
| POST | `/api/discord_bridge/direct/mark_read` | `{"channel_id", "message_id"}` で既読位置を進める（後戻りしない） |
| POST | `/api/discord_bridge/direct/send` | `{"channel_id", "content", "in_reply_to"(任意)}` を Discord に投稿。**ファイル添付も可**：multipart/form-data でフィールド `channel_id`・`content`（任意）とファイル部品 `file` を送ると、画像・動画・文書を添付して投稿します（Discord の上限 25MB） |
| GET | `/api/discord_bridge/direct/events?guild_id=` | サーバーの予定イベント一覧 |
| POST | `/api/discord_bridge/direct/events` | サーバーの予定イベントを作成（次節） |
| GET | `/api/discord_bridge/direct/forum?channel_id=` | フォーラムチャンネルの情報（利用可能タグ `available_tags` を含む） |
| GET | `/api/discord_bridge/direct/forum/posts?channel_id=&guild_id=` | フォーラムの投稿（アクティブ＋公開アーカイブ済み）一覧 |
| POST | `/api/discord_bridge/direct/forum/posts` | フォーラム投稿を作成（次節） |

### フォーラム投稿

フォーラムチャンネル（type 15）への投稿です。投稿1つがスレッドになり、種類や話題はタグで分けられます。

- JSON：`{"channel_id", "title", "content", "tags": ["画像"]}`。`tags` はフォーラムに定義されたタグの**名前またはID**で指定でき、統合側でIDに解決します。存在しないタグ名は 400 で、利用可能タグの一覧を返します。タグは最大5個（Discord の仕様）。
- ファイル添付：multipart/form-data でフィールド `channel_id`・`title`・`content`（任意）・`tags`（カンマ区切りの名前）とファイル部品 `file` を送ります（上限 25MB）。
- 対象チャンネルがフォーラムでない場合は 400 を返します。
- **既存のテキストチャンネルは Discord の仕様上フォーラムに変換できません。** フォーラムは新規作成し、旧チャンネルは改名してアーカイブする運用です（作成・改名とも次節のチャンネル管理で Bot が実行できます）。
- Bot にはフォーラムでの「メッセージを送信」「公開スレッドの作成」権限が必要です。

### 投票

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/discord_bridge/direct/poll` | 投票を作成。`{"channel_id", "question", "answers", "duration_hours", "allow_multiselect", "content"}` |
| GET | `/api/discord_bridge/direct/poll?channel_id=&message_id=` | 投票の現在の結果（選択肢ごとの票数・合計・確定済みか） |
| GET | `/api/discord_bridge/direct/poll/voters?channel_id=&message_id=&answer_id=` | その選択肢に投票したユーザー一覧 |
| POST | `/api/discord_bridge/direct/poll/expire` | 自分の投票を早期終了。`{"channel_id", "message_id"}` |

- 質問は300文字まで、選択肢は1〜10個（各55文字まで、絵文字付きも可）、期間は1〜768時間（既定24時間）、複数選択の可否を指定できます。
- Bot には「投票を送信（Send Polls）」権限が必要です。無い場合は Discord が 403 を返します。
- 投票は作成後に編集できません。ファイル添付と投票は同じメッセージに同時に入れられません。

### チャンネル管理

Bot に Discord の「チャンネルの管理（Manage Channels）」権限があれば、チャンネルの作成・変更もこの統合経由でできます。権限が無い場合は Discord が 403 を返し、そのまま返します。

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/discord_bridge/direct/channels` | チャンネル作成。`{"guild_id", "name", "type", "parent_id", "topic", "available_tags"}`。type 15 でフォーラム作成、`available_tags` はタグ名の文字列または `{"name", "emoji_name"}`（最大20個） |
| GET | `/api/discord_bridge/direct/channel?channel_id=` | チャンネル情報（フォーラムはタグ含む） |
| POST / PATCH | `/api/discord_bridge/direct/channel` | チャンネル変更。`{"channel_id", "name", "topic", "parent_id", "position"}`。フォーラムのタグ付け替えは `"tags": ["画像", "動画"]`（名前指定・既存タグのIDと絵文字は保持され、指定外のタグは削除）。生の形式が必要な場合は `"available_tags"` も可 |

- **既存のフォーラム投稿へのタグ付け替え**：同じ `POST / PATCH /api/discord_bridge/direct/channel` に `"applied_tags": ["GitHub", "公開済み"]` を渡すと、その投稿（スレッド）に付くタグを指定した集合に置き換えます。タグ名は親フォーラムで解決し、最大5個、指定外のタグは外れます。未知のタグ名は 400 で利用可能タグの一覧を返します。

履歴を読むだけなので、アドオンのキューや他のアシスタントと取り合いになりません。

### サーバーの予定イベント（Google カレンダー → Discord）

カレンダー等の外部の予定を、Discord サーバーの「イベント」に登録するための受け口です。

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "旅行：湖畔の家", "scheduled_start_time": "2026-12-20T09:00:00+09:00",
       "scheduled_end_time": "2026-12-22T18:00:00+09:00",
       "location": "湖畔の家", "description": "カレンダーより"}' \
  $HA/api/discord_bridge/direct/events
```

- Discord の仕様で、外部イベントには**場所（location）が必須**、終了時刻も必要です（省略すると開始の1時間後が入ります）。
- Bot にそのサーバーの「イベントの管理（Manage Events）」権限が必要です。無い場合は Discord 側が 403 を返します。
- 重複防止は呼び出し側の役割です（一覧で同名・同時刻の有無を確認してから作成する運用）。

## ブリッジモード

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/discord_bridge/health` | アドオンの `/health` |
| GET | `/api/discord_bridge/pending` | 対象ユーザーの未処理メッセージ |
| POST | `/api/discord_bridge/ack` | 対象ユーザーの ID だけ ack（他人の ID は `skipped_ids`） |
| POST | `/api/discord_bridge/send` | アドオン経由の投稿（`dedupe_id` 必須） |

## 共通の守り

- 全 API は Home Assistant の認証が必須。既定では**管理者ユーザー**のみ許可（設定で変更可）。
- 直接モードの `/new` と ブリッジモードの `/pending`・`/ack` には、設定した Discord ユーザーID（`author_id`）のフィルタ／ガードが効きます。
- トークン類はこの Home Assistant の設定内だけに保存され、外には出ません。

## インストールと設定

1. `custom_components/discord_bridge/` を HA の `/config/custom_components/` にコピーし、Home Assistant を再起動します。HACS を使う場合は、このリポジトリをカスタムリポジトリ（統合）として追加しても入れられます。
2. 「設定」→「デバイスとサービス」→「統合を追加」→「Discord Bridge」で入力します。

### 設定項目

- **Discord の Bot トークン（直接モード）**：アドオンの設定で使っている `discord_token` と同じ値です。アドオンの「設定」タブで確認できます（パスワード欄）。
- **Discord サーバー（ギルド）の ID**：Discord の開発者モードでサーバーアイコンを右クリック→「IDをコピー」。
- **監視するチャンネルID（カンマ区切り）**：`/direct/new` の対象です。チャンネル名を右クリック→「IDをコピー」。
- **対象の Discord ユーザーID**：`/new` のフィルタに使います。その人の発言だけが返ります。
- **Bridge のアドレス／キュートークン（ブリッジモード）**：直接モードだけなら空欄で構いません（どちらか一方のトークンは必須）。
- **管理者ユーザーを必須にする**：既定 ON 推奨。

設定の検証では、入力されたトークンで実際に接続確認をします（ブリッジは `/pending` の読み取り、Discord は Bot 情報の取得。メッセージ内容は保存されません）。

## Bot の権限について（直接モード）

- 履歴を読む：対象チャンネルの「チャンネルを見る」「メッセージ履歴を読む」。
- 投稿する：対象チャンネルの「メッセージを送信」。
- チャンネルを作成・変更する：サーバーの「チャンネルの管理」。無い場合はその操作だけ 403 になります。
- 投票を作る：対象チャンネルの「投票を送信」。
- イベントを作る：サーバーの「イベントの管理」。
権限が足りない操作は Discord が 403 を返し、この統合はそれをそのまま返します。

## テスト

`tests/` に、アドオンと Discord API の挙動を模したスタブ相手のテストがあります（36 件）。

```sh
python3 -m venv .venv
.venv/bin/pip install aiohttp pytest pytest-asyncio voluptuous
.venv/bin/python -m pytest -q
```

Home Assistant 本体への組み込み確認は実機で行ってください。直接モードの最短確認は `/api/discord_bridge/direct/health` です。

## ライセンス

MIT
