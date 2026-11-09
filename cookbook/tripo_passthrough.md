# Tripo passthrough

The built-in `/tripo/{endpoint}` route forwards native Tripo requests through the LiteLLM gateway. Generation returns a task ID immediately. Clients poll Tripo task status through the same gateway and download assets from the URLs in the response

## Configuration

Set the upstream credential on the proxy. Clients use their LiteLLM credential

```bash
export TRIPO_API_KEY="your-tripo-key"
export LITELLM_MASTER_KEY="your-gateway-key"
export DATABASE_URL="postgresql://user:password@localhost:5432/litellm"
litellm --config litellm/proxy/example_config_yaml/tripo_pass_through_config.yaml
```

`TRIPO_API_BASE` optionally overrides `https://openapi.tripo3d.ai`. Set it to the upstream origin or a path prefix, without appending `/v3` when the client already sends `/tripo/v3/...`. The proxy preserves a configured base path

No `model_list` entry is required. This integration reads `TRIPO_API_KEY` through LiteLLM's secret manager and does not resolve per-model Tripo credentials or accept caller-supplied upstream credentials

## Create and retrieve a task

Choose a model from the [Tripo API documentation](https://developers.tripo3d.ai/en/docs/quick-start) and set `TRIPO_MODEL` to its ID

```bash
curl http://localhost:4000/tripo/v3/generation/text-to-model \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  --data "{\"model\":\"$TRIPO_MODEL\",\"prompt\":\"a cute cat\"}"
```

The response retains Tripo's native format

```json
{"code": 0, "data": {"task_id": "task-id-from-tripo"}}
```

Use the returned task ID for subsequent requests

```bash
curl "http://localhost:4000/tripo/v3/tasks/$TASK_ID" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

A successful task includes output URLs. Download the model directly from the returned URL, without sending your gateway credential to the asset host

## Task isolation

A database is required. The proxy records task IDs and uploaded file tokens in `LiteLLM_ManagedObjectTable` before returning a successful creation response. Ownership is scoped to the creating gateway key and configured upstream credential and base URL. Another key, including another key in the same team, receives 404 for that resource

Single-task queries, `POST /v3/tasks/list`, model-processing task references and uploaded file references check ownership. GET account endpoints require a proxy administrator. The generation and model/mesh/animation processing endpoints allowed by the gateway, and `POST /v3/files` are enabled. Unsupported paths, v2 paths and direct storage object inputs are rejected. Use gateway-uploaded file tokens or public URLs for inputs. Upload a file with

```bash
curl http://localhost:4000/tripo/v3/files \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -F "file=@reference.png"
```

A task created outside this gateway is not automatically adopted. Rotating the upstream credential or changing the normalized upstream base URL changes its ownership scope. There is no automatic migration of existing records. Preserve the old proxy credential until outstanding tasks settle, or migrate the stored ownership explicitly. Output URLs remain bearer URLs with Tripo's expiration policy, so possession of a URL grants asset access independently of gateway authentication

## Asynchronous accounting

When `TRIPO_API_KEY` is configured at startup and the database scheduler is running, a job runs every 30 seconds per worker and polls persisted tasks without requiring client polling. Workers claim tasks through a database compare-and-set. Interrupted claims become eligible again after five minutes. Each worker poll claims at most 20 tasks and stops its batch on HTTP 429 or 503, leaving the claims for retry

For successful tasks, the proxy reads `data.credits_consumed` from the native task query. Missing credits remain pending, including when status is success. Failed, cancelled, banned and expired tasks settle with zero cost. Other statuses remain pending. HTTP errors, business errors and invalid usage values never become a fabricated zero-cost success

The default USD conversion is 0.01 per credit. Set `TRIPO_USD_PER_CREDIT` to a finite positive value when your deployment uses a different accounting rate. The rate is captured at task creation. The billed credits, captured rate, task ID and status remain in the final spend row metadata. For the four failure statuses, billed credits are zero even if the provider response contains a credit value. This is a credit-value conversion, not a reconstruction of the amount paid for discounted credit purchases

The final task charge uses a stable delivery ID in `LiteLLM_SpendLogDelivery`. The existing accounting worker applies it to key, user, team, organization, project and end-user spend, including key and team fixed budget windows read at settlement time. Repeated polling or a crash between recording a delivery and marking a task processed does not create a second charge while the delivery record is retained. Persisted key, user, team, organization and project budgets are checked before submitting new tasks

Accounting is eventual. In-flight work is not reserved against the gateway budget before completion, and multiple tasks can exceed a budget before their final cost arrives. HTTP request logs and the final task charge are separate records

The upstream creation and local database insert are not one distributed transaction. A process crash after Tripo accepts a task but before registration can leave an orphan task requiring administrator reconciliation against Tripo usage history. Requests are not automatically retried at this boundary. A production deployment should retain the upstream account history for reconciliation

## Validation

The focused unit tests cover local validation and ownership behavior

```bash
uv run pytest tests/unit/proxy/pass_through_endpoints/test_tripo.py \
  tests/unit/proxy/pass_through_endpoints/test_llm_passthrough_endpoints.py -q
```

With the integration harness dependencies installed, run the contracts against an owned proxy, PostgreSQL, Redis and a scripted upstream

```bash
INTEGRATION_WORKERS=2 python tests/integration/run.py providers \
  tests/integration/providers/test_tripo_passthrough_wire.py --workers 2
```

These contracts cover gateway behavior with synthetic upstream responses. They do not establish live Tripo compatibility or serve as real-provider PR proof

`scripts/start_tripo_proxy.sh` is a local convenience launcher. It can provision a Docker PostgreSQL instance when `DATABASE_URL` is absent. It starts a single proxy worker without Redis by default, so it is not the multi-worker QA setup. Run it with `--print-requirements` for its environment variables. Schema push is only for a disposable test database

For PR proof, use a copy of `litellm/proxy/dev_config.yaml` with the Tripo credential and database configured. Point both `router_settings.redis_host` / `redis_port` and `general_settings.coordination_redis.host` / `port` at the same dedicated Redis instance, including credentials when required. Start the proxy with

```bash
python litellm/proxy/proxy_cli.py --config /path/to/tripo-qa.yaml \
  --num_workers 2 --detailed_debug --reload --use_v2_migration_resolver 2>&1 | tee litellm.log
```

Check that two worker PIDs serve requests. If the checkout reports that reload ignores the worker count, record that limitation in the proof. Use matching setup at the merge base and tested tip, and retain the commands, responses and commit hashes in the PR

With a real Tripo credential configured on that proxy, set `LITELLM_MASTER_KEY`, `LITELLM_PROXY_URL` and `TRIPO_MODEL`, then run

```bash
E2E_TRIPO_STACK=1 E2E_POLL_TIMEOUT=600 E2E_POLL_INTERVAL=5 \
  uv run --group e2e-dev pytest tests/e2e/llm_translation/test_tripo_passthrough_e2e.py -v -s --reruns 0
```

This replaces `scripts/test_tripo_passthrough.sh`. The proxy URL variable is now `LITELLM_PROXY_URL`, replacing `LITELLM_BASE_URL`. The test purchases one generation, checks cross-key task access, and compares provider credits with the captured conversion rate, final spend row and key spend delta. Spend logging must be enabled. `TRIPO_PROMPT` optionally overrides the default chair prompt

`E2E_TRIPO_STACK` opts into the Tripo-configured stack. Without it, the test is deselected. Once enabled, missing configuration, provider errors, completion timeouts and accounting failures fail the run. `E2E_POLL_TIMEOUT` bounds each waiting phase and `E2E_POLL_INTERVAL` controls polling, replacing `TRIPO_POLL_SECONDS` and `TRIPO_POLL_INTERVAL`. The shared defaults are 120 and 5 seconds, so the command above allows 600 seconds per phase for generation and accounting

The test prints key aliases and task IDs, never key values. It deletes both keys after verified accounting. If accounting is incomplete after submission, it retains the owner key for reconciliation and deletes the foreign key. Inspect the task and spend through the administrator account before removing the retained owner key. `TRIPO_KEEP_TEST_KEYS` is no longer supported. Automatic generation retries and whole-test reruns are disabled to avoid purchasing duplicate tasks
