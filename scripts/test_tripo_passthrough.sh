#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${LITELLM_BASE_URL:-http://127.0.0.1:4000}"
MASTER_KEY="${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY must be set}"
MODEL="${TRIPO_MODEL:?TRIPO_MODEL must be set to a supported Tripo model ID}"
PROMPT="${TRIPO_PROMPT:-a simple low-poly wooden chair}"
POLL_SECONDS="${TRIPO_POLL_SECONDS:-600}"
POLL_INTERVAL="${TRIPO_POLL_INTERVAL:-5}"
KEEP_KEYS="${TRIPO_KEEP_TEST_KEYS:-0}"

for command in curl jq python3; do
  command -v "$command" >/dev/null || { printf '%s is required\n' "$command" >&2; exit 1; }
done
for duration in "$POLL_SECONDS" "$POLL_INTERVAL"; do
  [[ "$duration" =~ ^[1-9][0-9]*$ ]] || { printf 'Polling durations must be positive integers\n' >&2; exit 1; }
done

work_dir="$(mktemp -d)"
key_a=""
key_b=""
retain_owner=0
cleanup() {
  local result=$?
  if [[ "$KEEP_KEYS" != "1" ]]; then
    local keys
    keys="$(jq -cn --arg a "$key_a" --arg b "$key_b" --argjson retain "$retain_owner" \
      '{keys: [if $retain == 0 then $a else empty end, $b] | map(select(length > 0))}')"
    if [[ "$(jq '.keys | length' <<<"$keys")" != "0" ]]; then
      if ! curl --silent --show-error --fail --max-time 30 --request POST "$BASE_URL/key/delete" \
        --header "Authorization: Bearer $MASTER_KEY" --header 'Content-Type: application/json' \
        --data "$keys" >/dev/null; then
        printf 'Test key cleanup failed. Remove keys with the printed aliases after reconciliation\n' >&2
        result=1
      fi
    fi
    if (( retain_owner == 1 )); then
      printf 'Owner key retained because task accounting is incomplete. Reconcile it using the printed alias\n' >&2
    fi
  else
    printf 'Test keys retained because TRIPO_KEEP_TEST_KEYS=1\n'
  fi
  rm -rf "$work_dir"
  exit "$result"
}
trap cleanup EXIT

request() {
  local method="$1" path="$2" key="$3" output="$4"
  shift 4
  local status
  status="$(curl --silent --show-error --max-time 60 --output "$output" --write-out '%{http_code}' \
    --request "$method" "$BASE_URL$path" --header "Authorization: Bearer $key" "$@")"
  printf '%s' "$status"
}

create_key() {
  local alias="$1" output="$work_dir/key-$1.json"
  local status
  status="$(request POST /key/generate "$MASTER_KEY" "$output" \
    --header 'Content-Type: application/json' --data "$(jq -cn --arg alias "$alias" '{key_alias:$alias}')")"
  [[ "$status" == "200" ]] || { printf 'Key creation failed, HTTP %s\n' "$status" >&2; return 1; }
  jq -er '.key' "$output"
}

[[ "$(request GET /health "$MASTER_KEY" "$work_dir/health.json")" == "200" ]] || exit 1
alias_a="tripo-test-a-$(date +%s)-$$"
alias_b="tripo-test-b-$(date +%s)-$$"
key_a="$(create_key "$alias_a")"
key_b="$(create_key "$alias_b")"
printf 'Test key aliases %s and %s\n' "$alias_a" "$alias_b"
for owner in a b; do
  if [[ "$owner" == "a" ]]; then key="$key_a"; else key="$key_b"; fi
  [[ "$(request GET /key/info "$key" "$work_dir/baseline-$owner.json")" == "200" ]] || exit 1
done
baseline_a="$(jq -er '.info.spend | numbers' "$work_dir/baseline-a.json")"
baseline_b="$(jq -er '.info.spend | numbers' "$work_dir/baseline-b.json")"

retain_owner=1
[[ "$(request POST /tripo/v3/generation/text-to-model "$key_a" "$work_dir/create.json" \
  --header 'Content-Type: application/json' \
  --data "$(jq -cn --arg model "$MODEL" --arg prompt "$PROMPT" '{model:$model,prompt:$prompt}')")" == "200" ]] || exit 1
task_id="$(jq -er 'select(.code == 0) | .data.task_id' "$work_dir/create.json")"
printf 'Created task %s\n' "$task_id"
[[ "$(request GET "/tripo/v3/tasks/$task_id" "$key_a" "$work_dir/owner.json")" == "200" ]] || exit 1
[[ "$(request GET "/tripo/v3/tasks/$task_id" "$key_b" "$work_dir/foreign.json")" == "404" ]] || exit 1
[[ "$(request POST /tripo/v3/tasks/list "$key_b" "$work_dir/batch.json" \
  --header 'Content-Type: application/json' \
  --data "$(jq -cn --arg id "$task_id" '{task_ids:[$id]}')")" == "404" ]] || exit 1
printf 'Task ownership isolation passed\n'

deadline=$((SECONDS + POLL_SECONDS))
task_succeeded=0
while (( SECONDS < deadline )); do
  [[ "$(request GET "/tripo/v3/tasks/$task_id" "$key_a" "$work_dir/poll.json")" == "200" ]] || exit 1
  status="$(jq -er 'select(.code == 0) | .data.status' "$work_dir/poll.json")"
  printf 'Task status %s\n' "$status"
  case "$status" in
    success) task_succeeded=1; break ;;
    failed|cancelled|banned|expired) printf 'Task did not succeed\n' >&2; exit 1 ;;
  esac
  sleep "$POLL_INTERVAL"
done
(( task_succeeded == 1 )) || { printf 'Task completion timed out\n' >&2; exit 1; }
credits="$(jq -er '.data.credits_consumed | numbers | select(. >= 0)' "$work_dir/poll.json")"
key_hash="$(printf '%s' "$key_a" | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
accounting_deadline=$((SECONDS + POLL_SECONDS))
while (( SECONDS < accounting_deadline )); do
  [[ "$(request GET /key/info "$key_a" "$work_dir/spend-a.json")" == "200" ]] || exit 1
  [[ "$(request GET /key/info "$key_b" "$work_dir/spend-b.json")" == "200" ]] || exit 1
  [[ "$(request GET "/spend/logs?api_key=$key_hash" "$MASTER_KEY" "$work_dir/logs.json")" == "200" ]] || exit 1
  if jq -e --arg id "$task_id" --argjson credits "$credits" --argjson baseline_a "$baseline_a" \
    --argjson baseline_b "$baseline_b" --slurpfile a "$work_dir/spend-a.json" --slurpfile b "$work_dir/spend-b.json" '
      def close($x; $y): ($x - $y | fabs) < 0.000000001;
      map(.metadata |= (if type == "string" then fromjson else . end))
      | map(select(.metadata.tripo_task_id == $id))
      | length == 1 and (.[0] as $row
        | ($row.metadata.tripo_usd_per_credit | tonumber) as $rate
        | $rate > 0
          and $row.metadata.usage_object.credits_consumed == $credits
          and close($row.spend; $credits * $rate)
          and close($a[0].info.spend - $baseline_a; $credits * $rate)
          and close($b[0].info.spend; $baseline_b))' "$work_dir/logs.json" >/dev/null; then
    retain_owner=0
    printf 'Exact task cost and key isolation verified, credits %s\n' "$credits"
    exit 0
  fi
  sleep "$POLL_INTERVAL"
done
printf 'Task accounting did not match provider credits and the captured rate before timeout\n' >&2
exit 1
