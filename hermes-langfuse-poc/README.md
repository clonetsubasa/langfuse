# HermesAgent Langfuse Local PoC

MacBook 上の Docker Compose で Langfuse を起動し、Dockerized HermesAgent から turn 単位の trace を送るためのローカル PoC です。本番化、外部公開、個人ツール連携は対象外です。

## 起動

```bash
cd /Users/tsubasaclone/workspace/langfuse
/opt/homebrew/bin/docker-compose --env-file hermes-langfuse-poc/.env \
  -f docker-compose.yml \
  -f hermes-langfuse-poc/docker-compose.poc.yml \
  up -d
```

準備完了確認:

```bash
curl -fsS http://localhost:3000/api/public/ready
```

Langfuse UI は `http://localhost:3000` で開きます。初期ログイン情報と Project API key は `hermes-langfuse-poc/.env` にあります。このファイルは git ignore 対象です。

Hermes 側は以下を確認します。

```bash
docker exec hermes bash -lc 'source /opt/hermes/.venv/bin/activate && uv pip install langfuse'
docker exec hermes bash -lc 'source /opt/hermes/.venv/bin/activate && hermes plugins enable observability/langfuse'
docker restart hermes
```

PoC plugin は `/Users/tsubasaclone/.hermes/plugins/observability/langfuse` にあります。再適用用のコピーは `hermes-langfuse-poc/runtime-overrides/plugins/observability/langfuse` に保存しています。

Slack user/channel/thread metadata と cron job metadata を plugin hook に渡すため、実行中の Hermes container では以下の最小差分を入れています。

- `/opt/hermes/run_agent.py`: `hermes-langfuse-poc/runtime-overrides/patches/hermes-run-agent-hook-metadata.patch`
- `/opt/hermes/cron/scheduler.py`: `hermes-langfuse-poc/runtime-overrides/patches/hermes-cron-scheduler-langfuse-metadata.patch`

## 停止

```bash
cd /Users/tsubasaclone/workspace/langfuse
/opt/homebrew/bin/docker-compose --env-file hermes-langfuse-poc/.env \
  -f docker-compose.yml \
  -f hermes-langfuse-poc/docker-compose.poc.yml \
  down
```

データも削除する場合だけ `down -v` を使います。

## 環境変数

Langfuse 側の初期化値は `hermes-langfuse-poc/.env` にあります。

HermesAgent は Docker コンテナ内からホストの Langfuse に接続するため、`/Users/tsubasaclone/.hermes/.env` では以下を使います。

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=http://host.docker.internal:3000
LANGFUSE_FLUSH_INTERVAL=5
LANGFUSE_CAPTURE_CONTENT=operational
LANGFUSE_MAX_CHARS=12000
LANGFUSE_TOOL_ERROR_MAX_CHARS=1200
```

HermesAgent を Mac ホスト上で直接動かす場合だけ、`LANGFUSE_BASE_URL=http://localhost:3000` に変更します。

Slack 経由の turn では、Langfuse 標準の `user_id` に `slack:<team_id>:<slack_user_id>` を入れます。`team_id` が取れない場合は `slack:<slack_user_id>` です。trace metadata には、取得できる範囲で `slack_user_id`, `slack_user_name`, `slack_team_id`, `slack_channel_id`, `slack_channel_type`, `slack_thread_ts` を入れます。

cron 実行の turn では、Langfuse 標準の `user_id` に `cron:<job_id>` を入れます。trace metadata には `cron_job_id`, `cron_job_name`, `cron_schedule`, `origin_platform`, `origin_chat_id`, `origin_thread_id` を入れます。cron は人間のSlackユーザーではなく scheduled actor として集計します。

## 確認

1. `curl -fsS http://localhost:3000/api/public/ready` が `OK` を返す。
2. `http://localhost:3000` にログインし、`HermesAgent PoC` project を開く。
3. HermesAgent を 1 turn 実行する。
4. 数秒待って Langfuse の Traces で `hermes.turn` が 1 件以上見えることを確認する。
5. trace の Input に user prompt、Output に最終 assistant response が入っていることを確認する。
6. Slack 経由の turn では trace の User ID が `slack:...` になり、metadata に Slack user/channel/thread 情報があることを確認する。
7. cron 実行の turn では trace の User ID が `cron:...` になり、metadata に cron job と origin 情報があることを確認する。
8. API key/password/token/private key、tool output 全文、ファイル内容が入っていないことを確認する。

画面の読み方は [langfuse-dashboard-guide.md](langfuse-dashboard-guide.md) にスクショ付きでまとめています。

Langfuse 停止時の確認:

```bash
cd /Users/tsubasaclone/workspace/langfuse
/opt/homebrew/bin/docker-compose --env-file hermes-langfuse-poc/.env \
  -f docker-compose.yml \
  -f hermes-langfuse-poc/docker-compose.poc.yml \
  stop langfuse-web langfuse-worker
```

この状態で HermesAgent を 1 turn 実行しても通常応答が止まらないことを確認します。確認後は `up -d` で再開します。

## 既知の制限

- ローカル PoC 専用です。バックアップ、HA、監視、権限設計は未対応です。
- trace は SDK の batching により数秒遅れて表示されることがあります。
- Hermes Docker からは `host.docker.internal` を使います。
- Hermes コンテナを base image から作り直した場合、`langfuse` SDK の再インストールと runtime override の再適用が必要です。
- `LANGFUSE_CAPTURE_CONTENT=operational` では user prompt と最終 assistant response を trace Input/Output に送ります。
- LLM generation には request summary、usage tokens、latency、finish reason を送ります。
- Slack user identity は Langfuse の標準 `user_id` に入れ、Slack固有情報は trace metadata に入れます。
- cron job identity は Langfuse の標準 `user_id` に `cron:<job_id>` として入れ、job name/schedule/origin は trace metadata に入れます。
- email は redaction 対象外です。
- API key/password/token/private key は redaction します。
- tool args は内容系フィールドを省略して送ります。tool output 全文とファイル内容は送りません。
