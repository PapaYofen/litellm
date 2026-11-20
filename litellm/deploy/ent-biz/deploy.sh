#!/usr/bin/env bash
# 用当前仓库构建带 Tripo 透传的 LiteLLM 镜像，推到 ent-biz 已有的 ACR，
# 然后 kubectl apply 同目录的 litellm.yaml。
#
# 用法（在仓库根目录或任意目录都可以）：
#   litellm/deploy/ent-biz/deploy.sh
#   litellm/deploy/ent-biz/deploy.sh --build-push
#
# --build-push 只构建并推送镜像，不检查集群，不读 .env，不创建 Secret，不 kubectl apply。
# 密钥放在同目录的 .env，这个文件名已被仓库 gitignore 覆盖，不要提交。完整部署才会用到它。
# 第一次跑会补上 LITELLM_MASTER_KEY、LITELLM_SALT_KEY、POSTGRES_PASSWORD。
# 这三项生成之后不要改。TRIPO_API_KEY 和 TRIPO_GLOBAL_API_KEY 需要自己填，
# 脚本发现是空的会停下来，不会开始构建。
#
# 已有的 Secret litellm-env 不会被覆盖。要换密钥就先删 Secret。
# 数据库密码存在这个 Secret 里，删掉 PVC litellm-db-data 才会清空库。
#
# 容器重启会丢掉还在跑的 Tripo 计费轮询。有任务没完成时不要重复跑这个脚本。
# 只要重新套用清单、不构建镜像：SKIP_BUILD=1 litellm/deploy/ent-biz/deploy.sh
# --build-push 和 SKIP_BUILD=1 不能一起用。
set -euo pipefail

BUILD_PUSH_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-push)
      BUILD_PUSH_ONLY=1
      shift
      ;;
    *)
      echo "未知参数: $1 。只构建并推送用 --build-push 。" >&2
      exit 1
      ;;
  esac
done

if [[ "$BUILD_PUSH_ONLY" == "1" && "${SKIP_BUILD:-}" == "1" ]]; then
  echo "--build-push 和 SKIP_BUILD=1 不能一起用。" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENV_FILE="${LITELLM_ENV_FILE:-$SCRIPT_DIR/.env}"
NAMESPACE="ent-biz"
SECRET_NAME="litellm-env"
COMMIT="$(git -C "$ROOT" rev-parse HEAD | cut -c1-8)"
IMAGE="tripo-registry-registry.cn-beijing.cr.aliyuncs.com/tripo/ent-biz:litellm-tripo-${COMMIT}"
# 集群是阿里云北京的 ACK，节点是 amd64。在 Apple Silicon 上不指定平台会打出 arm 镜像。
PLATFORM="linux/amd64"

read_env() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
  local value="${line#"${key}"=}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

set_env_if_empty() {
  local key="$1"
  local value="$2"
  if [[ -n "$(read_env "$key")" ]]; then
    return
  fi
  local tmp
  tmp="$(mktemp)"
  grep -v -E "^${key}=" "$ENV_FILE" >"$tmp" || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
}

require_env() {
  local key="$1"
  if [[ -z "$(read_env "$key")" ]]; then
    echo "填好 ${ENV_FILE} 里的 ${key} 再跑。脚本不会开始构建。" >&2
    exit 1
  fi
}

if [[ "$BUILD_PUSH_ONLY" != "1" ]]; then
  if [[ ! -f "$ENV_FILE" ]]; then
    touch "$ENV_FILE"
  fi

  set_env_if_empty LITELLM_MASTER_KEY "sk-$(openssl rand -hex 32)"
  set_env_if_empty LITELLM_SALT_KEY "sk-$(openssl rand -hex 32)"
  set_env_if_empty POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  require_env TRIPO_API_KEY
  require_env TRIPO_GLOBAL_API_KEY

  echo "检查集群 ${NAMESPACE}"
  kubectl -n "$NAMESPACE" get deploy tripo-blind-test >/dev/null
fi

if [[ "${SKIP_BUILD:-}" != "1" ]]; then
  echo "构建 ${IMAGE} 。第一次会编 Admin UI、Python 依赖和 Rust，时间比较长。"
  docker build \
    --platform "$PLATFORM" \
    -f "$ROOT/litellm/deploy/docker/Dockerfile" \
    --target runtime \
    -t "$IMAGE" \
    "$ROOT"
  echo "推送 ${IMAGE} 。本机需要已经 docker login tripo-registry-registry.cn-beijing.cr.aliyuncs.com"
  docker push "$IMAGE"
else
  echo "SKIP_BUILD=1，不构建镜像。Deployment 仍会指向 ${IMAGE}，仓库里要已经有这个标签。"
fi

if [[ "$BUILD_PUSH_ONLY" == "1" ]]; then
  echo "已推送 ${IMAGE} 。集群还没动。回到有 kubectl 的机器后执行 SKIP_BUILD=1 litellm/deploy/ent-biz/deploy.sh"
  exit 0
fi

if kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  echo "Secret ${SECRET_NAME} 已在，保持原值。改 .env 不会轮换它。"
else
  postgres_password="$(read_env POSTGRES_PASSWORD)"
  kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
    --from-literal=POSTGRES_PASSWORD="$postgres_password" \
    --from-literal=DATABASE_URL="postgresql://llmproxy:${postgres_password}@litellm-db:5432/litellm" \
    --from-literal=LITELLM_MASTER_KEY="$(read_env LITELLM_MASTER_KEY)" \
    --from-literal=LITELLM_SALT_KEY="$(read_env LITELLM_SALT_KEY)" \
    --from-literal=TRIPO_API_KEY="$(read_env TRIPO_API_KEY)" \
    --from-literal=TRIPO_GLOBAL_API_KEY="$(read_env TRIPO_GLOBAL_API_KEY)"
  unset postgres_password
fi

rendered="$(mktemp)"
sed "s|litellm-tripo-COMMIT|litellm-tripo-${COMMIT}|g" "$SCRIPT_DIR/litellm.yaml" >"$rendered"
kubectl apply -f "$rendered"
rm -f "$rendered"
# 标签变了，Deployment 的 image 字段会变，滚动更新自己会发生。
kubectl -n "$NAMESPACE" rollout status deploy/litellm-db --timeout=180s
kubectl -n "$NAMESPACE" rollout status deploy/litellm --timeout=600s

cat <<'EOF'

清单已套用。集群外用 ALB 域名，不要走 tripo-blind-test.vast-internal.com，那个域名会被备案拦截。
Master key 在 litellm/deploy/ent-biz/.env 的 LITELLM_MASTER_KEY，下面命令会从当前 shell 读它。

set -a
source litellm/deploy/ent-biz/.env
set +a

curl -sS http://alb-nsomc38go29ucqn14c.cn-beijing.alb.aliyuncsslb.com/litellm/health/liveliness

curl -sS http://alb-nsomc38go29ucqn14c.cn-beijing.alb.aliyuncsslb.com/litellm/cn.tripo/v3/account/balance \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"

curl -sS http://alb-nsomc38go29ucqn14c.cn-beijing.alb.aliyuncsslb.com/litellm/tripo/v3/account/balance \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"

管理界面：http://alb-nsomc38go29ucqn14c.cn-beijing.alb.aliyuncsslb.com/litellm/ui
EOF
