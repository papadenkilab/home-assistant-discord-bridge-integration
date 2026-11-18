# Discord Bridge カスタム統合 for Home Assistant

Home Assistant の認証付き API 経由で Discord を使うためのカスタム統合です。新しいポートは開けません。外部のプログラムは、DuckDNS 等で既に公開されている Home Assistant の HTTPS アドレスに、Home Assistant の長期アクセストークンでアクセスします。

2つのモードが使えます（片方だけでも、両方でも可）。

- **直接モード**：この統合自体が Discord の REST API と通信します。Bot トークンはこの Home Assistant の中にだけ保存されます。Gateway には接続しない（REST のみ）ので、アドオンと同じトークンを使っても二重ログインになりません。
- **ブリッジモード**：宅内の Discord Bridge アドオンのキューを中継します。

できること（直接モード）：チャンネル履歴の取得（添付・埋め込み・スタンプ含む）、監視チャンネルの新着取得と既読管理、投稿とファイル添付（25MB まで）、フォーラム投稿の作成・一覧・タグ付け替え、チャンネルの作成・変更、投票の作成・結果取得・早期終了、サーバーの予定イベントの一覧・作成。

全 API は Home Assistant の認証が必須で、既定では管理者ユーザーのみ許可されます。API の詳細仕様（エンドポイント一覧、リクエスト形式、権限の対応、運用方法）は、作者の解説記事にまとめています。

## 関連プロジェクト：Discord Bridge アドオン

姉妹プロジェクトとして、Home Assistant アドオン版の **Discord Bridge** があります。アドオン版は Discord の Gateway に常駐し、許可したユーザーの発言をキューに保存します。この統合のブリッジモードは、そのキューを Home Assistant の認証付き API 越しに中継するためのものです。

- リポジトリ：https://github.com/papadenkilab/home-assistant-discord-bridge

## インストール

### Marketplace / HACS で入れる

Home Assistant 2026.11 以降は標準搭載の Marketplace、それ以前は HACS から入れられます。どちらのボタンも、自分の Home Assistant でこのリポジトリを開きます（カタログ未掲載のため、案内に従ってカスタムリポジトリとして追加してください）。

Marketplace（2026.11 以降）：

[![Home Assistant を開いて、Marketplace でこのリポジトリを表示する](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=papadenkilab&repository=home-assistant-discord-bridge-integration&category=integration)

HACS（2026.11 より前）：

[![Home Assistant を開いて、このリポジトリを HACS に登録するダイアログを表示する](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?repository=https%3A%2F%2Fgithub.com%2Fpapadenkilab%2Fhome-assistant-discord-bridge-integration&category=integration)

手動で追加する場合は、Marketplace の「Add from link」（HACS では「⋮」→「カスタムリポジトリ」）で、リポジトリに `https://github.com/papadenkilab/home-assistant-discord-bridge-integration`、カテゴリに「統合」を指定します。追加後、「Discord Bridge」をダウンロード（インストール）し、案内があれば Home Assistant を再起動します。

### 手動で入れる

`custom_components/discord_bridge/` を HA の `/config/custom_components/` にコピーし、Home Assistant を再起動します。

その後、「設定」→「デバイスとサービス」→「統合を追加」→「Discord Bridge」で入力します。

## 設定項目

- **Discord の Bot トークン（直接モード）**：アドオンの設定で使っている `discord_token` と同じ値です。
- **Discord サーバー（ギルド）の ID**：Discord の開発者モードでサーバーアイコンを右クリック→「IDをコピー」。
- **監視するチャンネルID（カンマ区切り）**：新着取得の対象です。
- **対象の Discord ユーザーID**：新着取得のフィルタに使います。その人の発言だけが返ります。
- **Bridge のアドレス／キュートークン（ブリッジモード）**：直接モードだけなら空欄で構いません（どちらか一方のトークンは必須）。
- **管理者ユーザーを必須にする**：既定 ON 推奨。

設定の検証では、入力されたトークンで実際に接続確認をします（メッセージ内容は保存されません）。

## Bot の権限について（直接モード）

履歴を読むには対象チャンネルの「チャンネルを見る」「メッセージ履歴を読む」、投稿するには「メッセージを送信」が必要です。チャンネルの作成・変更、投票、イベントには、それぞれサーバー／チャンネル側の対応する管理権限が必要です。権限が足りない操作は Discord が 403 を返し、この統合はそれをそのまま返します。

## ライセンス

MIT
