#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

compose() {
  docker compose -f docker-compose.yml -f docker-compose.tripo.yml -f docker-compose.tripo.cn.yml "$@"
}

read_env() {
  local key="$1"
  local line=""
  [[ -f .env ]] || return 0
  line="$(grep -E "^${key}=" .env | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

if [[ $# -gt 1 ]]; then
  echo "用法: ./start-tripo-cn.sh [中国区 Tripo API key]" >&2
  exit 1
fi

if [[ $# -eq 1 ]]; then
  tripo_key="$1"
else
  printf '中国区 Tripo API key: '
  IFS= read -r tripo_key
fi

if [[ -z "$tripo_key" || ! "$tripo_key" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "中国区 Tripo API key 为空，或含有不能写入 .env 的字符。" >&2
  exit 1
fi

master="$(read_env LITELLM_MASTER_KEY)"
salt="$(read_env LITELLM_SALT_KEY)"
if [[ -n "$master" && -n "$salt" ]]; then
  :
elif [[ -z "$master" && -z "$salt" ]]; then
  master="sk-$(openssl rand -hex 32)"
  salt="sk-$(openssl rand -hex 32)"
else
  echo "LITELLM_MASTER_KEY 和 LITELLM_SALT_KEY 需要同时保留。不要只留下其中一个。" >&2
  exit 1
fi

tmp_env="$(mktemp)"
if [[ -f .env ]]; then
  grep -Ev '^(LITELLM_MASTER_KEY|LITELLM_SALT_KEY|TRIPO_API_KEY|TRIPO_GLOBAL_API_KEY)=' .env >"$tmp_env" || true
fi
{
  cat "$tmp_env"
  printf 'LITELLM_MASTER_KEY=%s\n' "$master"
  printf 'LITELLM_SALT_KEY=%s\n' "$salt"
  printf 'TRIPO_API_KEY=%s\n' "$tripo_key"
} >.env
rm -f "$tmp_env"
chmod 600 .env

export LITELLM_ENV_FILE=.env

echo "正在启动 LiteLLM。"
compose up -d

deadline="$((SECONDS + 300))"
ready="0"
while (( SECONDS < deadline )); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:4000/health/liveliness || true)"
  if [[ "$code" == "200" ]]; then
    ready="1"
    break
  fi
  sleep 3
done

if [[ "$ready" != "1" ]]; then
  echo "服务没有在 300 秒内就绪。" >&2
  if compose logs --tail 200 litellm 2>&1 | grep -q 'password authentication failed for user "llmproxy"'; then
    echo "数据库密码和旧卷不一致。确认可以清空这个栈的数据库后执行下面的命令，再重新运行本脚本。" >&2
    echo "docker compose -f docker-compose.yml -f docker-compose.tripo.yml -f docker-compose.tripo.cn.yml down -v" >&2
  else
    echo "查看日志: docker compose -f docker-compose.yml -f docker-compose.tripo.yml -f docker-compose.tripo.cn.yml logs --tail 80 litellm" >&2
  fi
  exit 1
fi

printf '\n服务已就绪。\n\n'
printf '调用base url %s\n' "http://127.0.0.1:4000/cn.tripo/v3"
printf '管理界面 %s\n' "http://127.0.0.1:4000/ui"
printf '用户名 admin\n'
printf '密码 %s\n' "$master"
