本地仓库已经停在这个 PR 的分支 `litellm_tripo_async_passthrough`，提交 `812d8739a9` 就是 [PR #50839](https://github.com/BerriAI/litellm/pull/50839) 的当前 tip。不要用官方 `main-stable` 镜像，那个镜像里没有这次的 Tripo 路由，必须在这份代码上 `--build`。

这次改动暴露两条透传路由：

- 国际：`/tripo/v3/...`，上游必须是 `https://openapi.tripo3d.ai/v3`
- 中国：`/cn.tripo/v3/...`，上游必须是 `https://openapi.tripo3d.com/v3`

部署必须是代理全局的（不要带 `team_id`），并且 `use_in_pass_through: true`。只配了某个 team 的部署会直接返回 503。

## 1. 准备密钥

在 `litellm/deploy/compose` 里写一份只给这次测试用的 `.env`。这个文件已被 gitignore 覆盖的是仓库根目录；compose 目录下的文件不要提交。

```bash
cd /Users/chenwanli/workspace/github.com/PapaYofen/litellm/litellm/deploy/compose

printf 'LITELLM_MASTER_KEY=sk-%s\nLITELLM_SALT_KEY=sk-%s\nTRIPO_API_KEY=%s\nTRIPO_GLOBAL_API_KEY=%s\n' \
  "$(openssl rand -hex 32)" \
  "$(openssl rand -hex 32)" \
  "你的中国区 Tripo key" \
  "你的国际区 Tripo key" > .env

export LITELLM_ENV_FILE=.env
```

`LITELLM_MASTER_KEY` 和 `LITELLM_SALT_KEY` 生成一次就不要再改。Compose 用这份 `.env` 做变量替换，容器也通过 `LITELLM_ENV_FILE` 读到 Tripo key。数据库地址不用写，compose 会把它指到旁边的 Postgres 容器。

只有一个区的 key 也可以测，另一个区会返回 503。

## 2. 写 Tripo 配置

同目录保存 `tripo.config.yaml`：

```yaml
model_list:
  - model_name: tripo/task
    litellm_params:
      model: tripo/task
      custom_llm_provider: tripo
      api_key: os.environ/TRIPO_API_KEY
      api_base: https://openapi.tripo3d.com/v3
      use_in_pass_through: true
  - model_name: tripo-global/task
    litellm_params:
      model: tripo/task
      custom_llm_provider: tripo
      api_key: os.environ/TRIPO_GLOBAL_API_KEY
      api_base: https://openapi.tripo3d.ai/v3
      use_in_pass_through: true

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY

litellm_settings:
  local_model_cost_map: true
```

`api_base` 的主机名决定区域：`.com` 是中国，`.ai` 是国际，路径必须正好以 `/v3` 结尾。`model_name` 不能空，请求方的 key 必须有权调用这个名字。Master key 可以。

## 3. 用 override 挂上配置

不要改仓库里的 `docker-compose.yml`。同目录再写 `docker-compose.tripo.yml`：

```yaml
services:
  litellm:
    command: ["--config", "/app/config.yaml"]
    volumes:
      - ./tripo.config.yaml:/app/config.yaml:ro
    environment:
      LITELLM_LOCAL_MODEL_COST_MAP: "True"
```

## 4. 构建并启动

第一次构建会编 Admin UI、Python 依赖和 Rust，时间会比较长。

```bash
docker compose -f docker-compose.yml -f docker-compose.tripo.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.tripo.yml ps
docker compose -f docker-compose.yml -f docker-compose.tripo.yml logs -f litellm
```

看到 `/health/liveliness` 返回 200 再发请求。如果日志里是 `password authentication failed for user "llmproxy"`，说明旧的 `litellm_postgres_data` 卷凭证对不上，先停掉并删卷再启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.tripo.yml down -v
docker compose -f docker-compose.yml -f docker-compose.tripo.yml up -d --build
```

`down -v` 会清掉这个栈里的数据库。

## 5. 不产生生成任务的检查

下面的余额查询只读账户，不会创建生成任务。把 master key 读进 shell，不要写进命令历史以外的地方：

```bash
set -a
source .env
set +a

curl -sS http://127.0.0.1:4000/health/liveliness

curl -sS http://127.0.0.1:4000/cn.tripo/v3/account/balance \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"

curl -sS http://127.0.0.1:4000/tripo/v3/account/balance \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

配好的区域应返回 HTTP 200，响应里 `code` 为 `0`。没配 deployment 的区域返回 503，正文是 `No Tripo use_in_pass_through deployment configured for ...`。

流式请求会在转发前被拒绝，用来确认计费不会被 streaming handler 绕过：

```bash
curl -sS -D - http://127.0.0.1:4000/tripo/v3/generation/text-to-model \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"v3.1-20260211","prompt":"A small wooden chair","stream":true}'
```

预期 HTTP 400，`Streaming is not supported for Tripo requests`。这条不会打到上游。

管理界面在 http://127.0.0.1:4000/ui ，用 `LITELLM_MASTER_KEY` 登录。

## 6. 可选：真实生成和计费

这一步会扣 Tripo credits。提交一次，然后只用返回的 `task_id` 查询，不要反复 POST。

```bash
curl -sS http://127.0.0.1:4000/cn.tripo/v3/generation/text-to-model \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"v3.1-20260211","prompt":"A cute orange cat sitting upright, stylized cartoon, smooth surfaces"}'
```

成功时 `code` 为 `0`，`data.task_id` 有值。然后轮询：

```bash
curl -sS "http://127.0.0.1:4000/cn.tripo/v3/tasks/<task_id>" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

状态离开 `queued` / `running` 后，响应里会有 `credits_consumed`。国际区把前缀换成 `/tripo/v3`。容器里的后台轮询大约每 10 秒一次，最长约 30 分钟；任务结束后，创建那次请求的 spend 会出现在 UI 的日志里。客户端自己的 GET 不会再开一条计费轮询。

容器重启会丢掉还在跑的计费任务，所以验证 spend 时不要在任务完成前重建容器。
