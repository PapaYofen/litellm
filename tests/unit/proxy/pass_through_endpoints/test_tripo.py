import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest
from fastapi import HTTPException, Request, Response
from pydantic import ValidationError
from starlette.types import Message

from litellm.models.tripo import TripoObject, TripoOwner, TripoTaskResult
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.pass_through_endpoints.tripo import (
    authorize_tripo_request,
    build_tripo_ownership_scope,
    register_tripo_response,
)
from litellm.repositories.tripo_repository import TripoRepository


class _ObjectRow:
    def __init__(self, value: TripoObject, encoded: bool) -> None:
        self.file_object = value.model_dump_json() if encoded else value.model_dump(mode="json")


class _ObjectTable:
    def __init__(self, encoded: bool = True) -> None:
        self._encoded = encoded

    async def find_unique(self, where: Mapping[str, object]) -> _ObjectRow | None:
        item: Final = TripoObject(
            object_id="owner-task",
            scope="account",
            owner=TripoOwner(api_key="owner-key"),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            model="tripo/test",
            usd_per_credit=Decimal("0.01"),
        )
        return _ObjectRow(item, self._encoded) if where["unified_object_id"] == item.storage_id else None


class _Writer:
    def __init__(self, encoded: bool = True) -> None:
        self.litellm_managedobjecttable = _ObjectTable(encoded)

    async def query_raw(self, query: str, *args: object) -> Sequence[Mapping[str, object]]:
        return ()


class _Client:
    def __init__(self, encoded: bool = True) -> None:
        self.writer_db = _Writer(encoded)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,status", [("owner-key", 200), ("foreign-key", 404)])
async def test_task_queries_require_the_creating_key(key: str, status: int) -> None:
    request: Final = Request({"type": "http", "method": "GET", "headers": []})
    repository: Final = TripoRepository(_Client())
    if status == 404:
        with pytest.raises(HTTPException) as error:
            await authorize_tripo_request(
                "v3/tasks/owner-task", request, UserAPIKeyAuth(api_key=key), repository, "account"
            )
        assert error.value.status_code == status
        return
    owner, body = await authorize_tripo_request(
        "v3/tasks/owner-task", request, UserAPIKeyAuth(api_key=key), repository, "account"
    )
    assert (owner.api_key, body) == (key, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["v3/tasks/../account/balance", "v3/tasks/%2e%2e", "v3/tasks//owner-task"])
async def test_noncanonical_paths_cannot_bypass_ownership(endpoint: str) -> None:
    request: Final = Request({"type": "http", "method": "GET", "headers": []})
    with pytest.raises(HTTPException) as error:
        await authorize_tripo_request(
            endpoint,
            request,
            UserAPIKeyAuth(api_key="owner-key"),
            TripoRepository(_Client()),
            "account",
        )
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("field,status", [("prompt", 200), ("model", 200), ("file_token", 404), ("input", 404)])
async def test_only_resource_fields_require_ownership(field: str, status: int) -> None:
    payload: Final = {field: "file_cabinet"}

    async def receive() -> Message:
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

    request: Final = Request(
        {"type": "http", "method": "POST", "headers": [], "path": "/tripo/v3/generation/text-to-model"},
        receive=receive,
    )
    if status == 404:
        with pytest.raises(HTTPException) as rejected:
            await authorize_tripo_request(
                "v3/generation/text-to-model",
                request,
                UserAPIKeyAuth(api_key="owner-key"),
                TripoRepository(_Client()),
                "account",
            )
        assert rejected.value.status_code == 404
        return
    _, body = await authorize_tripo_request(
        "v3/generation/text-to-model",
        request,
        UserAPIKeyAuth(api_key="owner-key"),
        TripoRepository(_Client()),
        "account",
    )
    assert body == payload


@pytest.mark.parametrize("base", ["https://UPSTREAM.EXAMPLE:443", "https://upstream.example/"])
def test_scope_normalizes_equivalent_base_urls(base: str) -> None:
    assert build_tripo_ownership_scope(base, "key") == build_tripo_ownership_scope("https://upstream.example", "key")
    assert build_tripo_ownership_scope(base, "key") != build_tripo_ownership_scope(base, "another-key")
    assert build_tripo_ownership_scope(base, "key") != build_tripo_ownership_scope(base.rstrip("/") + "/other", "key")


@pytest.mark.parametrize("credits", [-1, "NaN", "Infinity"])
def test_invalid_provider_charges_are_rejected(credits: str | int) -> None:
    with pytest.raises(ValidationError):
        TripoTaskResult.model_validate({"task_id": "task", "status": "success", "credits_consumed": credits})


def test_missing_credits_are_distinct_from_a_free_task() -> None:
    missing: Final = TripoTaskResult(task_id="task", status="success")
    free: Final = TripoTaskResult(task_id="task", status="success", credits_consumed=Decimal(0))
    assert missing.credits_consumed is None
    assert free.credits_consumed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [False, True])
async def test_prisma_json_representation_preserves_task_ownership(encoded: bool) -> None:
    item: Final = TripoObject(
        object_id="owner-task",
        scope="account",
        owner=TripoOwner(api_key="owner-key"),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        model="tripo/test",
        usd_per_credit=Decimal("0.01"),
    )
    assert await TripoRepository(_Client(encoded)).get_tripo_object("account", "owner-task") == item


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not JSON", b'{"code":0}', b'{"code":0,"data":{}}'])
async def test_success_without_a_registered_resource_returns_an_upstream_error(payload: bytes) -> None:
    with pytest.raises(HTTPException) as rejected:
        await register_tripo_response(
            Response(content=payload),
            "v3/generation/text-to-model",
            {},
            TripoOwner(api_key="owner-key"),
            TripoRepository(_Client()),
            "account",
            Decimal("0.02"),
        )
    assert rejected.value.status_code == 502
