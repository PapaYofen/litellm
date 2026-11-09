import json
from collections.abc import Iterator
from hashlib import sha256
from typing import Final
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from integration._support.client import Gateway, eventually, gateway_from_environment
from integration._support.process import OwnedProxy, owned_proxy_process
from integration._support.wire import Reply, Request, Wire, wire_server

_API_KEY: Final = "scripted-tripo-key"
_TASK: Final = {"code": 0, "data": {"task_id": "task-scripted"}}
_RESULT: Final = {
    "code": 0,
    "data": {
        "credits_consumed": 12.5,
        "task_id": "task-scripted",
        "status": "success",
        "progress": 100,
        "output": {"model_url": "https://assets.example/model.glb"},
    },
}
_ERROR: Final = {"code": 2000, "message": "scripted upstream rejection"}


def _respond(request: Request) -> Reply:
    if "/rejected" in request.target:
        return Reply(status=429, body=json.dumps(_ERROR).encode(), headers={"retry-after": "7"})
    if "/business-error" in request.target:
        return Reply(body=json.dumps(_ERROR).encode())
    if request.target.endswith("/v3/files"):
        return Reply(body=json.dumps({"code": 0, "data": {"file_token": "file_" + uuid4().hex}}).encode())
    if request.method == "GET":
        task_id: Final = urlsplit(request.target).path.rsplit("/", 1)[-1]
        details: Final = {**_RESULT["data"], "task_id": task_id}
        if task_id.startswith("failed-"):
            return Reply(
                body=json.dumps({"code": 0, "data": {**details, "status": "failed", "credits_consumed": 0}}).encode()
            )
        if task_id.startswith("missing-"):
            return Reply(
                body=json.dumps(
                    {"code": 0, "data": {key: value for key, value in details.items() if key != "credits_consumed"}}
                ).encode()
            )
        return Reply(body=json.dumps({"code": 0, "data": details}).encode())
    prompt: Final = json.loads(request.body).get("prompt", "task")
    prefix: Final = prompt if prompt in ("failed", "missing") else "task"
    return Reply(body=json.dumps({"code": 0, "data": {"task_id": prefix + "-" + uuid4().hex}}).encode())


@pytest.fixture(scope="module")
def rig() -> Iterator[Gateway]:
    with gateway_from_environment() as gateway:
        yield gateway


@pytest.fixture(scope="module")
def wire() -> Iterator[Wire]:
    with wire_server(_respond) as served:
        yield served


@pytest.fixture(autouse=True)
def drained(wire: Wire) -> None:
    wire.drain()


@pytest.fixture(scope="module")
def running_tripo(rig: Gateway, wire: Wire, tmp_path_factory: pytest.TempPathFactory) -> Iterator[OwnedProxy]:
    directory: Final = tmp_path_factory.mktemp("tripo-passthrough")
    config: Final = directory / "proxy.yaml"
    config.write_text(
        "model_list: []\n"
        "general_settings:\n"
        "  master_key: os.environ/LITELLM_MASTER_KEY\n"
        "  database_url: os.environ/DATABASE_URL\n"
        "  store_model_in_db: true\n"
        "  proxy_batch_write_at: 1\n"
        "  coordination_redis:\n"
        "    host: os.environ/REDIS_HOST\n"
        "    port: os.environ/REDIS_PORT\n"
        "router_settings:\n"
        "  redis_host: os.environ/REDIS_HOST\n"
        "  redis_port: os.environ/REDIS_PORT\n"
    )
    with owned_proxy_process(
        rig,
        directory,
        {"TRIPO_API_BASE": f"{wire.url}/upstream", "TRIPO_API_KEY": _API_KEY, "TRIPO_USD_PER_CREDIT": "0.02"},
        config=config,
        workers=2,
    ) as candidate:
        yield candidate


@pytest.fixture(scope="module")
def tripo_proxy(running_tripo: OwnedProxy) -> Gateway:
    return running_tripo.gateway


def test_create_and_poll_preserve_native_payloads_and_use_proxy_credentials(tripo_proxy: Gateway, wire: Wire) -> None:
    body: Final = {"model": "scripted-3d-model", "prompt": "a cat", "texture": True}
    with tripo_proxy.scenario() as scenario:
        key: Final = scenario.key()
        created: Final = tripo_proxy.request(
            "POST",
            "/tripo/v3/generation/text-to-model",
            body,
            key=key,
            headers={"x-api-key": key, "x-pass-authorization": "Bearer caller-override"},
        )
        assert created.status_code == 200, created.text
        task_id: Final = created.json()["data"]["task_id"]
        assert created.json() == {"code": 0, "data": {"task_id": task_id}}
        result: Final = tripo_proxy.request("GET", f"/tripo/v3/tasks/{task_id}", key=key, params={"include": "output"})
        assert result.status_code == 200, result.text
        assert result.json() == {"code": 0, "data": {**_RESULT["data"], "task_id": task_id}}
        requests: Final = wire.drain()
        assert [(item.method, item.target) for item in requests] == [
            ("POST", "/upstream/v3/generation/text-to-model"),
            ("GET", f"/upstream/v3/tasks/{task_id}?include=output"),
        ]
        assert json.loads(requests[0].body) == body
        assert requests[1].body == b""
        for item in requests:
            assert item.headers["authorization"] == f"Bearer {_API_KEY}"
            assert all(key not in value for value in item.headers.values()), item.headers
        assert created.headers["x-litellm-call-id"] != result.headers["x-litellm-call-id"]


def test_upstream_errors_keep_status_body_and_retry_header(tripo_proxy: Gateway, wire: Wire) -> None:
    rejected: Final = tripo_proxy.request("GET", "/tripo/v3/account/rejected")
    assert rejected.status_code == 429, rejected.text
    assert rejected.json() == _ERROR
    assert rejected.headers["retry-after"] == "7"
    business_error: Final = tripo_proxy.request("GET", "/tripo/v3/account/business-error")
    assert business_error.status_code == 200, business_error.text
    assert business_error.json() == _ERROR
    assert [request.target for request in wire.drain()] == [
        "/upstream/v3/account/rejected",
        "/upstream/v3/account/business-error",
    ]


def test_invalid_gateway_key_is_rejected_before_forwarding(tripo_proxy: Gateway, wire: Wire) -> None:
    rejected: Final = tripo_proxy.request("GET", "/tripo/v3/tasks/task-scripted", key="sk-invalid-tripo-caller")
    assert rejected.status_code in (401, 403), rejected.text
    accepted: Final = tripo_proxy.request("GET", "/tripo/v3/account/balance")
    assert accepted.status_code == 200, accepted.text
    assert [(item.method, item.target) for item in wire.drain()] == [("GET", "/upstream/v3/account/balance")]


def test_multipart_upload_preserves_file_and_fields(tripo_proxy: Gateway, wire: Wire) -> None:
    response: Final = tripo_proxy.request_multipart(
        "/tripo/v3/files",
        {"purpose": "reference"},
        {"file": ("reference.png", b"scripted-image-bytes", "image/png")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    assert response.json()["data"]["file_token"].startswith("file_")
    requests: Final = wire.drain()
    assert len(requests) == 1, requests
    assert requests[0].target == "/upstream/v3/files"
    assert requests[0].headers["authorization"] == f"Bearer {_API_KEY}"
    assert requests[0].headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'filename="reference.png"' in requests[0].body
    assert b"scripted-image-bytes" in requests[0].body
    assert b"reference" in requests[0].body


def test_foreign_key_cannot_query_reuse_or_list_a_task(tripo_proxy: Gateway, wire: Wire) -> None:
    with tripo_proxy.scenario() as scenario:
        owner: Final = scenario.key()
        foreign: Final = scenario.key()
        created: Final = tripo_proxy.request(
            "POST", "/tripo/v3/generation/text-to-model", {"prompt": "isolation"}, key=owner
        )
        assert created.status_code == 200, created.text
        task_id: Final = created.json()["data"]["task_id"]
        denied: Final = (
            tripo_proxy.request("GET", f"/tripo/v3/tasks/{task_id}", key=foreign),
            tripo_proxy.request("POST", "/tripo/v3/tasks/list", {"task_ids": [task_id]}, key=foreign),
            tripo_proxy.request("POST", "/tripo/v3/models/texture", {"input": task_id}, key=foreign),
            tripo_proxy.request("GET", "/tripo/v3/account/usage/history", key=foreign),
        )
        assert [response.status_code for response in denied] == [404, 404, 404, 403], [
            response.text for response in denied
        ]
        assert all(request.method != "POST" or "/generation/" in request.target for request in wire.drain())


def test_background_completion_charges_once_without_client_polling(tripo_proxy: Gateway, wire: Wire) -> None:
    with tripo_proxy.scenario() as scenario:
        key: Final = scenario.key(max_budget=0.2)
        created: Final = tripo_proxy.request(
            "POST", "/tripo/v3/generation/text-to-model", {"prompt": "billing"}, key=key
        )
        assert created.status_code == 200, created.text
        task_id: Final = created.json()["data"]["task_id"]
        digest: Final = sha256(key.encode()).hexdigest()
        rows: Final = eventually(
            lambda: tripo_proxy.rows('SELECT spend FROM "LiteLLM_VerificationToken" WHERE token=%s', (digest,)),
            lambda values: bool(values) and float(values[0]["spend"]) >= 0.25,
            seconds=100,
        )
        assert float(rows[0]["spend"]) == pytest.approx(12.5 * 0.02)
        charged: Final = tripo_proxy.rows(
            "SELECT request_id, spend FROM \"LiteLLM_SpendLogs\" WHERE metadata->>'tripo_task_id'=%s", (task_id,)
        )
        assert len(charged) == 1, charged
        assert float(charged[0]["spend"]) == pytest.approx(12.5 * 0.02)
        denied: Final = tripo_proxy.request(
            "POST", "/tripo/v3/generation/text-to-model", {"prompt": "over budget"}, key=key
        )
        assert denied.status_code in (400, 429), denied.text
        assert any(request.method == "GET" and task_id in request.target for request in wire.drain())


def test_persisted_task_survives_worker_replacement_and_replay(
    running_tripo: OwnedProxy, tripo_proxy: Gateway, wire: Wire
) -> None:
    with tripo_proxy.scenario() as scenario:
        key: Final = scenario.key()
        created: Final = tripo_proxy.request(
            "POST", "/tripo/v3/generation/text-to-model", {"prompt": "restart"}, key=key
        )
        assert created.status_code == 200, created.text
        task_id: Final = created.json()["data"]["task_id"]
        before: Final = running_tripo.worker_pids()
        assert len(before) == 2
        replacements: Final = tuple(running_tripo.kill_worker(pid) for pid in before)
        assert len(set(replacements) - before) == 2
        result: Final = tripo_proxy.request("GET", f"/tripo/v3/tasks/{task_id}", key=key)
        assert result.status_code == 200, result.text
        digest: Final = sha256(key.encode()).hexdigest()
        read_spend: Final = lambda: tripo_proxy.rows(
            'SELECT spend FROM "LiteLLM_VerificationToken" WHERE token=%s', (digest,)
        )
        first: Final = eventually(read_spend, lambda rows: bool(rows) and float(rows[0]["spend"]) > 0, seconds=100)
        assert float(first[0]["spend"]) == pytest.approx(12.5 * 0.02)
        from integration._support.database import write_rows

        write_rows(
            "UPDATE \"LiteLLM_ManagedObjectTable\" SET batch_processed=false, status='pending' WHERE unified_object_id LIKE %s",
            (f"%:{task_id}",),
            database_url=tripo_proxy.database_url,
        )
        settled: Final = eventually(
            lambda: tripo_proxy.rows(
                'SELECT batch_processed FROM "LiteLLM_ManagedObjectTable" WHERE unified_object_id LIKE %s',
                (f"%:{task_id}",),
            ),
            lambda rows: bool(rows) and rows[0]["batch_processed"] is True,
            seconds=100,
        )
        assert settled == [{"batch_processed": True}]
        assert read_spend() == first
        assert (
            len(
                tripo_proxy.rows(
                    'SELECT delivery_id FROM "LiteLLM_SpendLogDelivery" WHERE delivery_id LIKE %s',
                    (f"%:{task_id}:cost",),
                )
            )
            == 1
        )


def test_failed_tasks_settle_at_zero_and_missing_credits_remain_unsettled(tripo_proxy: Gateway, wire: Wire) -> None:
    with tripo_proxy.scenario() as scenario:
        key: Final = scenario.key()
        responses: Final = tuple(
            tripo_proxy.request("POST", "/tripo/v3/generation/text-to-model", {"prompt": prompt}, key=key)
            for prompt in ("failed", "missing")
        )
        assert [response.status_code for response in responses] == [200, 200]
        failed_id, missing_id = (response.json()["data"]["task_id"] for response in responses)
        rows: Final = eventually(
            lambda: tripo_proxy.rows(
                'SELECT status, batch_processed FROM "LiteLLM_ManagedObjectTable" WHERE unified_object_id LIKE %s',
                (f"%:{failed_id}",),
            ),
            lambda values: bool(values) and values[0]["batch_processed"] is True,
            seconds=100,
        )
        assert rows == [{"status": "failed", "batch_processed": True}]
        missing: Final = tripo_proxy.rows(
            'SELECT batch_processed FROM "LiteLLM_ManagedObjectTable" WHERE unified_object_id LIKE %s',
            (f"%:{missing_id}",),
        )
        assert missing == [{"batch_processed": False}]
        digest: Final = sha256(key.encode()).hexdigest()
        assert tripo_proxy.rows('SELECT spend FROM "LiteLLM_VerificationToken" WHERE token=%s', (digest,)) == [
            {"spend": 0.0}
        ]
        charges: Final = tripo_proxy.rows(
            'SELECT spend, status FROM "LiteLLM_SpendLogs" WHERE request_id LIKE %s', (f"%:{failed_id}:cost",)
        )
        assert charges == [{"spend": 0.0, "status": "failure"}]
        assert (
            tripo_proxy.rows(
                'SELECT delivery_id FROM "LiteLLM_SpendLogDelivery" WHERE delivery_id LIKE %s',
                (f"%:{missing_id}:cost",),
            )
            == []
        )
        assert any(missing_id in request.target for request in wire.drain())
