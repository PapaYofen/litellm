# 用 LiteLLM 调用 Tripo(基于 LiteLLM 1.110.0适配TRIPO)

Tripo 开发者文档里的接口不用改。把文档里的 base url 换成下面的 LiteLLM 地址，`Authorization` 换成 LiteLLM 的 key。请求体、路径和查询参数仍按 Tripo 文档写。文档在 https://developers.tripo3d.com/zh/docs/introduction 。

中国站文档里的 `https://openapi.tripo3d.com/v3` 换成 `http://127.0.0.1:4000/cn.tripo/v3`。国际站文档里的 `https://openapi.tripo3d.ai/v3` 换成 `http://127.0.0.1:4000/tripo/v3`。前面另有反向代理加了路径前缀时，把同样的前缀加到这两个地址上。默认不设置 `SERVER_ROOT_PATH`。

调用时带 LiteLLM 的 key，格式是 `Authorization: Bearer <LiteLLM key>`。这把 key 是下面生成的 `LITELLM_MASTER_KEY`，或在 UI 里发出的 Virtual Key。不要把 Tripo 自己的 `tsk_` key 放进调用请求。LiteLLM 会换成配置里的 Tripo key 再转给上游。

官方 `main-stable` 镜像里没有这次的路由。在这份代码上 `--build`。

## 1. 配置并启动

在 `litellm/deploy/compose` 里写 `.env`，不要提交。两个区都要时：

```bash
cd litellm/deploy/compose

printf 'LITELLM_MASTER_KEY=sk-%s\nLITELLM_SALT_KEY=sk-%s\nTRIPO_API_KEY=%s\nTRIPO_GLOBAL_API_KEY=%s\n' \
  "$(openssl rand -hex 32)" \
  "$(openssl rand -hex 32)" \
  "你的中国区 Tripo key" \
  "你的国际区 Tripo key" > .env

export LITELLM_ENV_FILE=.env
```

`LITELLM_MASTER_KEY` 和 `LITELLM_SALT_KEY` 生成一次就不要再改。改了 salt 之后，库里已经加密的 key 读不出来。数据库地址不用写，compose 会把它指到旁边的 Postgres。

只接中国站时，`.env` 里不要写 `TRIPO_GLOBAL_API_KEY`。

```bash
LITELLM_MASTER_KEY=sk-你的master-key
LITELLM_SALT_KEY=sk-你的salt-key
TRIPO_API_KEY=你的中国区Tripo-key
```

同目录已有 `tripo.config.yaml`。只接中国站时，删掉 `tripo-global/task` 那一段。

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

`use_in_pass_through` 必须是 `true`。`api_base` 必须正好是上面两个地址之一，以 `/v3` 结尾。配置里不要写 `team_id`。

同目录已有 `docker-compose.tripo.yml`，不要改仓库里的 `docker-compose.yml`。

```yaml
services:
  litellm:
    command: ["--config", "/app/config.yaml"]
    volumes:
      - ./tripo.config.yaml:/app/config.yaml:ro
    environment:
      LITELLM_LOCAL_MODEL_COST_MAP: "True"
```

第一次构建会编 Admin UI、Python 依赖和 Rust，时间会比较长。下面的命令都在 `litellm/deploy/compose` 里执行。

```bash
docker compose -f docker-compose.yml -f docker-compose.tripo.yml up -d --build
```

如果日志里是 `password authentication failed for user "llmproxy"`，先删掉旧的数据库卷再启动。`down -v` 会清掉这个栈里的数据库。

```bash
docker compose -f docker-compose.yml -f docker-compose.tripo.yml down -v
docker compose -f docker-compose.yml -f docker-compose.tripo.yml up -d --build
```

## 2. 确认服务在线

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/liveliness
```

打印 `200` 就可以发请求。管理界面在 http://127.0.0.1:4000/ui ，用户名是 `admin`，密码是 `.env` 里的 `LITELLM_MASTER_KEY`。

给别人用时发 Virtual Key。左侧打开 Virtual Keys，点 Create New Key，Key Type 选 AI APIs。Models 选 `tripo/task` 或 `tripo-global/task`，留空表示全部模型都能调。创建后把弹窗里的 `sk-` 复制走，只显示一次。自己验证用 `LITELLM_MASTER_KEY` 即可。

## 3. 调用

先把 key 读进 shell。下面的中国站例子把 `$LITELLM_MASTER_KEY` 换成 Virtual Key 也一样。国际站把路径里的 `/cn.tripo/v3` 换成 `/tripo/v3`。

```bash
set -a
source .env
set +a
```

查余额。这个请求不创建任务。

```bash
curl -sS http://127.0.0.1:4000/cn.tripo/v3/account/balance \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

返回 HTTP 200 且 body 里 `code` 为 0，说明中国站已经通到 Tripo。没配这个区时返回 503。

文生模型。这是 Tripo 文档里的 `POST /generation/text-to-model`，会扣 credits。提交一次，然后用返回的 `data.task_id` 查询。

```bash
curl -sS http://127.0.0.1:4000/cn.tripo/v3/generation/text-to-model \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"v3.1-20260211","prompt":"A cute orange cat sitting upright, stylized cartoon, smooth surfaces","texture":false,"pbr":false}'
```

成功时 `code` 为 0，`data.task_id` 有值，`status` 一般是 `queued`。接着查任务，直到 `status` 为 `success`、`failed`、`cancelled`、`canceled`、`banned` 或 `expired`。

```bash
curl -sS "http://127.0.0.1:4000/cn.tripo/v3/tasks/TASK_ID" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

上传图片再图生模型。先拿 `data.file_token`，再填进下一步的 `input`。

```bash
curl -sS -X POST http://127.0.0.1:4000/cn.tripo/v3/files \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -F "file=@chair.png"

curl -sS -X POST http://127.0.0.1:4000/cn.tripo/v3/generation/image-to-model \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"FILE_TOKEN","model":"v3.1-20260211","texture":false,"pbr":false}'
```

路径必须带 `v3/`。文档里的其它接口同样处理，把 base url 和 key 换掉，路径原样接在后面。
