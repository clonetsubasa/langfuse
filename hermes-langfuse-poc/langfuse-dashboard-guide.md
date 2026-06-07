# Langfuse Dashboard Guide for HermesAgent PoC

この資料は、ローカル self-host の Langfuse で HermesAgent の turn が見えたあとに、デフォルト UI のどこを見れば何が分かるかを整理したものです。

対象は `/Users/tsubasaclone/workspace/langfuse/hermes-langfuse-poc` のローカル PoC です。画面上の数値はスクショ取得時点の値なので、実行した turn 数やフィルタ条件で変わります。

## まず見る順番

1. **Home** で trace が届いているか、turn 全体の latency が悪化していないかを見る。
2. **Tracing** で 1 user turn ごとの `hermes.turn` を一覧し、遅い turn や対象 session を探す。
3. **Trace Detail** で、その turn の中身が LLM 待ちなのか tool 待ちなのかを見る。
4. **Sessions** で同じ `session_id` に属する複数 turn をまとめて追う。
5. **Users** で Slack user_id 単位の利用状況を見る。

## 1. Home

![Home dashboard](/Users/tsubasaclone/workspace/langfuse/hermes-langfuse-poc/screenshots/01-home-dashboard.png)

Home は「いま Langfuse に何が届いているか」を最初に見る画面です。

見る場所:

- **Traces**: `hermes.turn` の件数が増えていれば、HermesAgent の turn が Langfuse に届いています。`Unknown` が出る場合は、名前なし trace や PoC 外のイベントが混ざっています。
- **Traces by time**: いつ turn が発生したかを時系列で見ます。HermesAgent を動かした直後に山が立てば、送信経路は動いています。
- **Model costs / Model Usage**: 運用モードでは usage が取れる turn から token と概算costが見えます。古いPoC trace、usage未取得のturn、Langfuse側で価格未定義のmodelは `$0.00` や token `0` になることがあります。
- **Scores**: 今回は evaluation や score を送っていないため `0` で正常です。
- **Trace latency percentiles**: turn 全体の処理時間です。`p50` は普段の中央値、`p90/p95/p99` は遅い turn を見るための値です。
- **Generation latency / Observation latency percentiles**: LLM call と tool call の待ち時間を種類別に見ます。ここで `llm.call` が長ければモデル待ち、`tool.*` が長ければ tool 側の待ちが疑えます。
- **Model costs / Model Usage**: 運用モードでは usage が取れる turn から token と概算costが見えます。provider/modelによってはcostが未算出になる場合があります。

この画面で得られる示唆:

- `hermes.turn` が増えている: PoC の trace 送信は成功しています。
- latency の高い percentile だけ長い: 一部の turn だけが重い可能性があります。
- `tool.terminal` や `tool.search_files` が長い: LLM ではなく tool 実行がボトルネックかもしれません。
- cost/token が出ている: LLM使用量の概算把握に使えます。出ないtraceは古いPoC traceかusage未取得のturnです。

## 2. Tracing

![Tracing list](/Users/tsubasaclone/workspace/langfuse/hermes-langfuse-poc/screenshots/02-traces-list.png)

Tracing は「1 user turn = 1 trace」を探すための一覧です。

見る場所:

- **Name**: `hermes.turn` が HermesAgent の 1 turn です。
- **Timestamp**: turn の開始時刻です。HermesAgent を動かした時刻と照合します。
- **Input / Output**: 運用モードでは、Input に Slack/user prompt、Output に最終 assistant response が入ります。tool output 全文やファイル内容は引き続き送りません。
- **User ID**: Slack 経由の turn では `slack:<team_id>:<slack_user_id>` です。cron 実行の turn では `cron:<job_id>` です。
- **左側 Filters**: `Trace Name`, `Session ID`, `Latency`, `Environment` で絞り込めます。

よく使う見方:

- 直近の turn を見る: Timestamp で最新行を見る。
- HermesAgent だけ見る: `Trace Name = hermes.turn` で絞る。
- 特定会話を追う: `Session ID` で絞る。
- 遅い turn を探す: `Latency` filter や表示列を使って絞る。

この画面で得られる示唆:

- `hermes.turn` の行が増えている: turn 単位の trace 化はできています。
- Input/Output が入っている: どの依頼に対してどの最終応答を返したかを一覧から追えます。
- 同じ session_id の trace が複数ある: 一連の会話として追跡できます。

## 3. Trace Detail

![Trace detail](/Users/tsubasaclone/workspace/langfuse/hermes-langfuse-poc/screenshots/04-trace-observations.png)

Trace Detail は、1 turn の中で LLM call と tool call がどう並んだかを見る画面です。Tracing の `hermes.turn` 行をクリックして開きます。

見る場所:

- **左ペインの timeline/tree**: `llm.call`, `tool.*` がどの順番で実行されたかを見ます。
- **各 observation の秒数**: LLM call や tool call の latency です。
- **Latency badge**: turn 全体の処理時間です。
- **Session badge**: この turn が属する `session_id` です。
- **Metadata**: `session_id`, `platform`, `provider`, `model`, `turn_started_at`, `turn_ended_at`, `turn_latency_ms`, `tool_calls` などを確認します。
- **Input / Output**: 運用モードでは user prompt と最終 assistant response を確認できます。

この画面で得られる示唆:

- `llm.call` が支配的に長い: モデル応答や provider 側の待ちが主因です。
- `tool.*` が支配的に長い: shell、file search、外部 tool などの実行時間が主因です。
- tool が failed になっている: HermesAgent の通常応答は返っていても、内部 tool で失敗している可能性があります。
- Metadata に model/provider がある: どの実行基盤・モデルの turn かを後で絞り込めます。
- Input/Output が入っている: Slackからの依頼内容と最終応答を同じtrace上で確認できます。
- tool observation のOutputが要約になっている: tool output全文やファイル内容は送っていないことを確認できます。
- Metadata に `slack_user_id`, `slack_user_name`, `slack_channel_id`, `slack_channel_type`, `slack_thread_ts` がある: Slack のどのユーザー・チャンネル・スレッドから来た turn かを追えます。
- Metadata に `cron_job_id`, `cron_job_name`, `cron_schedule`, `origin_platform`, `origin_chat_id`, `origin_thread_id` がある: どの cron job が、どの配信元/配信先文脈で動いたかを追えます。

注意:

- Trace Detail へは一覧行をクリックして開くのが確実です。ローカル PoC では trace detail の直 URL が timestamp 条件などで開けないことがあります。

## 4. Sessions

![Sessions](/Users/tsubasaclone/workspace/langfuse/hermes-langfuse-poc/screenshots/04-sessions.png)

Sessions は `session_id` 単位で trace を束ねる画面です。

見る場所:

- **ID**: HermesAgent 側で付与した `session_id` です。
- **Created At**: session の最初の trace 時刻です。
- **Duration**: session 全体の継続時間です。
- **Environment**: `local` や `default` など、送信元環境を分けるための値です。
- **User IDs**: Slack 経由の turn では `slack:<team_id>:<slack_user_id>`、cron 実行の turn では `cron:<job_id>` が入ります。

この画面で得られる示唆:

- 長い Duration の session: 会話や定期実行が長く続いた可能性があります。
- `cron_...` の session: 自動実行系の turn として分けて見られます。User IDs の `cron:<job_id>` で同じjobの実行を追えます。
- `20260606_...` の session: 手動・通常会話系の turn として追いやすくなります。

## 5. Users

![Users](/Users/tsubasaclone/workspace/langfuse/hermes-langfuse-poc/screenshots/05-users.png)

Users は user_id を送っている場合に、ユーザー単位で trace、cost、score を集計する画面です。

Slack 経由の turn では、Langfuse 標準の `user_id` に `slack:<team_id>:<slack_user_id>` を送ります。Slack を複数人で同じ HermesAgent アカウントから使っていても、この画面では実際に話しかけた Slack ユーザー単位で分かれます。

cron 実行の turn では `user_id` に `cron:<job_id>` を送ります。人間ではなく scheduled actor として見えるため、同じ定期jobの実行回数、latency、costをまとめて追えます。

この画面で分かること:

- どの Slack ユーザーが HermesAgent を多く使っているか。
- どの cron job が HermesAgent を多く使っているか。
- 特定ユーザーの session や trace をまとめて追えるか。
- user 単位の cost や score を見られるか。

Slack user name、channel id、thread ts は user_id の標準列ではなく、trace metadata に入ります。ユーザー一覧では `user_id` を起点に入り、個別 trace の Metadata で channel/thread を確認します。

cron job name、schedule、origin platform/chat/thread も標準列ではなく、trace metadata に入ります。Usersでは `cron:<job_id>` を起点に入り、個別 trace の Metadata で job名や配信先を確認します。

## 現PoCで分かること

- HermesAgent の turn が Langfuse に届いているか。
- 1 turn あたりの処理時間。
- LLM call と tool call の latency。
- tool call の名前と成功/失敗。
- `session_id`, `platform`, `provider`, `model` による切り分け。
- Slack/user prompt と最終 assistant response。
- Slack user_id 別の利用状況。
- Slack channel/thread metadata。
- cron job 別の利用状況。
- cron job name/schedule/origin metadata。
- tool output全文やファイル内容を送っていないこと。

## 現PoCではまだ分からないこと

- Langfuse側で価格未定義のmodelの正確な cost。
- tool args の全量、tool output の全文。
- score/evaluation による品質推移。
- 本番運用向けの可用性、バックアップ、権限設計。

## 初回確認チェックリスト

1. Home の **Traces** で `hermes.turn` が増えている。
2. Tracing で最新行の **Name** が `hermes.turn` になっている。
3. Trace Detail の **Input / Output** に user prompt と最終 assistant response が入っている。
4. Trace Detail の **Metadata** に `session_id`, `platform`, `provider`, `model` がある。
5. Slack turn では Trace Detail の **Metadata** に `slack_user_id`, `slack_user_name`, `slack_channel_id`, `slack_thread_ts` がある。
6. cron turn では Trace Detail の **Metadata** に `cron_job_id`, `cron_job_name`, `cron_schedule`, `origin_platform`, `origin_chat_id`, `origin_thread_id` がある。
7. Trace Detail の左ペインに `llm.call` と `tool.*` が見える。
8. Sessions に `session_id` と User IDs が並んでいる。
9. tool output全文、ファイル内容、API key/password/token/private key が入っていない。
