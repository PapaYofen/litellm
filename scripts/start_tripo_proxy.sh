#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"
LITELLM_BIN="${LITELLM_BIN:-$VENV_DIR/bin/litellm}"
CONFIG="${TRIPO_PROXY_CONFIG:-$REPO_ROOT/litellm/proxy/example_config_yaml/tripo_pass_through_config.yaml}"
PORT="${LITELLM_PORT:-4000}"

print_requirements() {
  cat <<'REQUIREMENTS'
Startup environment variables
  TRIPO_API_KEY             Required, upstream Tripo API key
  LITELLM_MASTER_KEY        Required, LiteLLM administrator key

Database mode, choose one
  DATABASE_URL              Optional, existing PostgreSQL URL. When set, Docker is not started
  TRIPO_POSTGRES_DOCKER     Optional, defaults to 1. Set 0 only with DATABASE_URL

Docker PostgreSQL variables, used when DATABASE_URL is empty
  TRIPO_POSTGRES_CONTAINER  Optional, defaults to tripo-litellm-postgres
  TRIPO_POSTGRES_IMAGE      Optional, defaults to postgres:16-alpine
  TRIPO_POSTGRES_HOST       Optional, defaults to 127.0.0.1
  TRIPO_POSTGRES_PORT       Optional, defaults to 15440
  TRIPO_POSTGRES_USER       Optional, defaults to litellm
  TRIPO_POSTGRES_PASSWORD   Required for Docker PostgreSQL
  TRIPO_POSTGRES_DB         Optional, defaults to litellm_tripo_test

Optional provider and service variables
  TRIPO_API_BASE            Defaults to https://openapi.tripo3d.ai
  TRIPO_USD_PER_CREDIT      Defaults to 0.01
  LITELLM_PORT              Defaults to 4000
  TRIPO_POSTGRES_TIMEOUT    Defaults to 60 seconds
  TRIPO_USE_PRISMA_DB_PUSH  Defaults to 1 only for a newly created test container
  HTTP_PROXY                Optional, for example http://127.0.0.1:7897
  HTTPS_PROXY               Optional, for example http://127.0.0.1:7897
  NO_PROXY                   Optional, defaults to localhost,127.0.0.1,::1

  When DATABASE_URL is supplied directly, URL-encode any URL-sensitive password characters.
REQUIREMENTS
}

if [[ "${1:-}" == "--print-requirements" ]]; then
  print_requirements
  exit 0
fi

missing_variables=()
[[ -n "${TRIPO_API_KEY:-}" ]] || missing_variables+=(TRIPO_API_KEY)
[[ -n "${LITELLM_MASTER_KEY:-}" ]] || missing_variables+=(LITELLM_MASTER_KEY)
if [[ -z "${DATABASE_URL:-}" && "${TRIPO_POSTGRES_DOCKER:-1}" == "1" && -z "${TRIPO_POSTGRES_PASSWORD:-}" ]]; then
  missing_variables+=(TRIPO_POSTGRES_PASSWORD)
fi
if (( ${#missing_variables[@]} > 0 )); then
  print_requirements >&2
  printf 'Missing required environment variables: %s\n' "${missing_variables[*]}" >&2
  exit 1
fi

created_database=0
if [[ -z "${DATABASE_URL:-}" ]]; then
  TRIPO_POSTGRES_DOCKER="${TRIPO_POSTGRES_DOCKER:-1}"
  if [[ "$TRIPO_POSTGRES_DOCKER" != "1" ]]; then
    print_requirements >&2
    printf 'DATABASE_URL is empty while TRIPO_POSTGRES_DOCKER is 0\n' >&2
    exit 1
  fi
  : "${TRIPO_POSTGRES_PASSWORD:?Set TRIPO_POSTGRES_PASSWORD for the Docker PostgreSQL dependency}"
  TRIPO_POSTGRES_CONTAINER="${TRIPO_POSTGRES_CONTAINER:-tripo-litellm-postgres}"
  TRIPO_POSTGRES_IMAGE="${TRIPO_POSTGRES_IMAGE:-postgres:16-alpine}"
  TRIPO_POSTGRES_HOST="${TRIPO_POSTGRES_HOST:-127.0.0.1}"
  TRIPO_POSTGRES_PORT="${TRIPO_POSTGRES_PORT:-15440}"
  TRIPO_POSTGRES_USER="${TRIPO_POSTGRES_USER:-litellm}"
  TRIPO_POSTGRES_DB="${TRIPO_POSTGRES_DB:-litellm_tripo_test}"
  TRIPO_POSTGRES_TIMEOUT="${TRIPO_POSTGRES_TIMEOUT:-60}"

  if ! command -v docker >/dev/null 2>&1; then
    printf 'Docker is required when DATABASE_URL is not set\n' >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    printf 'Docker is not running or is unavailable\n' >&2
    exit 1
  fi

  container_exists=0
  container_status=""
  if docker container inspect "$TRIPO_POSTGRES_CONTAINER" >/dev/null 2>&1; then
    container_exists=1
    container_status="$(docker container inspect -f '{{.State.Status}}' "$TRIPO_POSTGRES_CONTAINER")"
  fi
  if [[ "$container_status" != "running" ]] \
    && lsof -nP -iTCP:"$TRIPO_POSTGRES_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    printf 'PostgreSQL host port %s is already allocated\n' "$TRIPO_POSTGRES_PORT" >&2
    printf 'Set TRIPO_POSTGRES_PORT to a free port, for example 15441, and run the script again\n' >&2
    printf 'If the old test container is disposable, remove it with: docker rm -f %s\n' "$TRIPO_POSTGRES_CONTAINER" >&2
    exit 1
  fi

  if [[ "$container_exists" == "1" ]]; then
    if [[ "$container_status" != "running" ]]; then
      docker start "$TRIPO_POSTGRES_CONTAINER" >/dev/null
    fi
  else
    docker run --detach \
      --name "$TRIPO_POSTGRES_CONTAINER" \
      --restart unless-stopped \
      --env "POSTGRES_USER=$TRIPO_POSTGRES_USER" \
      --env "POSTGRES_PASSWORD=$TRIPO_POSTGRES_PASSWORD" \
      --env "POSTGRES_DB=$TRIPO_POSTGRES_DB" \
      --publish "$TRIPO_POSTGRES_HOST:$TRIPO_POSTGRES_PORT:5432" \
      "$TRIPO_POSTGRES_IMAGE" >/dev/null
    created_database=1
  fi

  printf 'Waiting for PostgreSQL container %s\n' "$TRIPO_POSTGRES_CONTAINER"
  ready=0
  for ((attempt = 1; attempt <= TRIPO_POSTGRES_TIMEOUT; attempt++)); do
    if docker exec "$TRIPO_POSTGRES_CONTAINER" pg_isready \
      --username "$TRIPO_POSTGRES_USER" --dbname "$TRIPO_POSTGRES_DB" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" != "1" ]]; then
    printf 'PostgreSQL did not become ready within %ss\n' "$TRIPO_POSTGRES_TIMEOUT" >&2
    docker logs --tail 40 "$TRIPO_POSTGRES_CONTAINER" >&2 || true
    exit 1
  fi

  encoded_password="$(TRIPO_POSTGRES_PASSWORD="$TRIPO_POSTGRES_PASSWORD" python3 -c 'from os import environ; from urllib.parse import quote; print(quote(environ["TRIPO_POSTGRES_PASSWORD"], safe=""))')"
  export DATABASE_URL="postgresql://${TRIPO_POSTGRES_USER}:${encoded_password}@${TRIPO_POSTGRES_HOST}:${TRIPO_POSTGRES_PORT}/${TRIPO_POSTGRES_DB}"
fi

if [[ ! -x "$LITELLM_BIN" ]]; then
  printf 'LiteLLM executable not found: %s\n' "$LITELLM_BIN" >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  printf 'LiteLLM config not found: %s\n' "$CONFIG" >&2
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  printf 'LiteLLM host port %s is already allocated\n' "$PORT" >&2
  printf 'Stop the existing service or set LITELLM_PORT to a free port, for example 4001\n' >&2
  exit 1
fi

export TRIPO_API_BASE="${TRIPO_API_BASE:-https://openapi.tripo3d.ai}"
export TRIPO_USD_PER_CREDIT="${TRIPO_USD_PER_CREDIT:-0.01}"
export LITELLM_LOCAL_MODEL_COST_MAP="${LITELLM_LOCAL_MODEL_COST_MAP:-True}"
export PYTHON_DOTENV_DISABLED="${PYTHON_DOTENV_DISABLED:-1}"
if [[ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}${http_proxy:-}${https_proxy:-}" ]]; then
  export AIOHTTP_TRUST_ENV="${AIOHTTP_TRUST_ENV:-True}"
  export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,::1}"
  export no_proxy="${no_proxy:-$NO_PROXY}"
  printf 'HTTP proxy environment enabled for upstream requests\n'
fi

printf 'Starting LiteLLM Tripo passthrough on http://127.0.0.1:%s\n' "$PORT"
printf 'Config: %s\n' "$CONFIG"
printf 'Upstream: %s\n' "$TRIPO_API_BASE"
printf 'Database: configured\n'
printf 'Stop with Ctrl-C\n'

args=(--config "$CONFIG" --port "$PORT")
if [[ "${TRIPO_USE_PRISMA_DB_PUSH:-$created_database}" == "1" ]]; then
  args+=(--use_prisma_db_push)
fi

exec "$LITELLM_BIN" "${args[@]}"
