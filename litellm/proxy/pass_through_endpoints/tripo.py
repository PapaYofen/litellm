import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Final
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from litellm._logging import verbose_proxy_logger
from litellm.models.tripo import TripoObject, TripoObjectStore, TripoOwner, TripoTaskResult
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.pass_through_endpoints.pass_through_endpoints import HttpPassThroughEndpointHelpers
from litellm.secret_managers.main import get_secret_str

_BODY: Final = TypeAdapter(dict[str, JsonValue])
_IDS: Final = TypeAdapter(tuple[str, ...])
_RESOURCE_ID: Final = re.compile(r"[a-zA-Z0-9_-]{1,200}\Z")
_GENERATION: Final = frozenset(
    {
        "text-to-model",
        "image-to-model",
        "multiview-to-model",
        "image-to-splat",
        "text-to-image",
        "image-to-image",
        "image-to-multiview",
        "edit-multiview",
    }
)
_PROCESSING: Final = frozenset(
    {
        "models/texture",
        "models/convert",
        "models/import",
        "mesh/segment",
        "mesh/smartsegment",
        "mesh/complete",
        "mesh/decimate",
        "mesh/uv",
        "animations/rig-check",
        "animations/rig",
        "animations/retarget",
    }
)


class _NativeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: int
    data: Mapping[str, JsonValue] | None = None


def get_tripo_api_base() -> httpx.URL:
    return httpx.URL(get_secret_str("TRIPO_API_BASE") or "https://openapi.tripo3d.ai")


def build_tripo_ownership_scope(base_url: str, api_key: str) -> str:
    normalized: Final = str(httpx.URL(base_url)).rstrip("/")
    return hashlib.sha256(f"{normalized}\n{api_key}".encode()).hexdigest()


def get_tripo_usd_per_credit() -> Decimal:
    try:
        rate: Final = Decimal(get_secret_str("TRIPO_USD_PER_CREDIT") or "0.01")
    except InvalidOperation as error:
        raise HTTPException(503, "TRIPO_USD_PER_CREDIT must be a finite positive number") from error
    if not rate.is_finite() or rate <= 0:
        raise HTTPException(503, "TRIPO_USD_PER_CREDIT must be a finite positive number")
    return rate


def tripo_owner(auth: UserAPIKeyAuth) -> TripoOwner:
    if not auth.api_key:
        raise HTTPException(403, "Tripo requires an authenticated gateway key")
    return TripoOwner(
        api_key=auth.api_key,
        user_id=auth.user_id,
        team_id=auth.team_id,
        org_id=auth.org_id,
        project_id=auth.project_id,
        end_user_id=auth.end_user_id,
    )


async def _require_owner(store: TripoObjectStore, scope: str, object_id: str, owner: TripoOwner) -> None:
    if not _RESOURCE_ID.fullmatch(object_id):
        raise HTTPException(400, "Invalid Tripo resource identifier")
    item: Final = await store.get_tripo_object(scope, object_id)
    if item is None or item.owner.api_key != owner.api_key:
        raise HTTPException(404, "Tripo resource not found")


async def _check_references(
    value: JsonValue, store: TripoObjectStore, scope: str, owner: TripoOwner, field: str = ""
) -> None:
    if isinstance(value, dict):
        if "object" in value:
            raise HTTPException(400, "Use a gateway-uploaded file_token or a public URL for Tripo inputs")
        for name, child in value.items():
            await _check_references(child, store, scope, owner, name)
    elif isinstance(value, list):
        for child in value:
            await _check_references(child, store, scope, owner, field)
    elif isinstance(value, str) and value:
        reference: Final = field in ("task_id", "original_model_task_id", "file_token") or (
            field in ("input", "image", "front", "left", "back", "right")
            and urlsplit(value).scheme not in ("https", "http")
        )
        if reference:
            await _require_owner(store, scope, value, owner)


async def authorize_tripo_request(
    endpoint: str,
    request: Request,
    auth: UserAPIKeyAuth,
    store: TripoObjectStore,
    scope: str,
) -> tuple[TripoOwner, Mapping[str, JsonValue]]:
    owner: Final = tripo_owner(auth)
    if "%" in endpoint or "\\" in endpoint or any(part in ("", ".", "..") for part in endpoint.split("/")):
        raise HTTPException(400, "Invalid Tripo path")
    if request.method == "GET" and endpoint.startswith("v3/tasks/"):
        await _require_owner(store, scope, endpoint.removeprefix("v3/tasks/"), owner)
        return owner, {}
    if endpoint.startswith("v3/account/") and request.method == "GET":
        if auth.user_role != LitellmUserRoles.PROXY_ADMIN:
            raise HTTPException(403, "Tripo account endpoints require a proxy administrator")
        return owner, {}
    if request.method != "POST":
        raise HTTPException(403, "This Tripo endpoint is not enabled by the gateway")
    if endpoint == "v3/files":
        return owner, {}
    try:
        body: Final = _BODY.validate_json(await request.body())
    except ValidationError as error:
        raise HTTPException(400, "Tripo expects a JSON object") from error
    if endpoint == "v3/tasks/list":
        try:
            ids: Final = _IDS.validate_python(body.get("task_ids"))
        except ValidationError as error:
            raise HTTPException(400, "task_ids must be an array of strings") from error
        if not 1 <= len(ids) <= 100:
            raise HTTPException(400, "task_ids must contain 1 to 100 identifiers")
        for task_id in ids:
            await _require_owner(store, scope, task_id, owner)
        return owner, body
    allowed: Final = (
        endpoint.removeprefix("v3/generation/") in _GENERATION
        if endpoint.startswith("v3/generation/")
        else endpoint.removeprefix("v3/") in _PROCESSING
    )
    if not allowed:
        raise HTTPException(403, "This Tripo endpoint is not enabled by the gateway")
    if await store.tripo_budget_exhausted(owner):
        raise HTTPException(429, "Tripo task budget exceeded")
    await _check_references(dict(body), store, scope, owner)
    return owner, body


async def register_tripo_response(
    response: Response,
    endpoint: str,
    body: Mapping[str, JsonValue],
    owner: TripoOwner,
    store: TripoObjectStore,
    scope: str,
    rate: Decimal,
) -> None:
    if response.status_code >= 300 or endpoint.startswith(("v3/tasks/", "v3/account/")):
        return
    try:
        parsed: Final = _NativeResponse.model_validate_json(bytes(response.body))
    except ValidationError as error:
        raise HTTPException(502, "Tripo returned an invalid creation response") from error
    if parsed.code != 0:
        return
    if parsed.data is None:
        raise HTTPException(502, "Tripo did not return resource data")
    identifier: Final = parsed.data.get("file_token" if endpoint == "v3/files" else "task_id")
    if not isinstance(identifier, str) or not _RESOURCE_ID.fullmatch(identifier):
        raise HTTPException(502, "Tripo did not return a valid resource identifier")
    model: Final = body.get("model")
    await store.save_tripo_object(
        TripoObject(
            object_id=identifier,
            scope=scope,
            owner=owner,
            created_at=datetime.now(timezone.utc),
            model=f"tripo/{model if isinstance(model, str) else endpoint}",
            is_task=endpoint != "v3/files",
            usd_per_credit=rate,
        )
    )


async def poll_tripo_tasks(store: TripoObjectStore) -> None:
    api_key: Final = get_secret_str("TRIPO_API_KEY")
    if not api_key or not api_key.strip():
        return
    base: Final = str(get_tripo_api_base())
    scope: Final = build_tripo_ownership_scope(base, api_key.strip())
    tasks: Final = await store.claim_tripo_tasks(scope, datetime.now(timezone.utc))
    if not tasks:
        return
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for item in tasks:
            if not await _poll_tripo_task(client, store, item, base, api_key.strip()):
                return


async def _poll_tripo_task(
    client: httpx.AsyncClient, store: TripoObjectStore, item: TripoObject, base: str, api_key: str
) -> bool:
    try:
        url: Final = httpx.URL(base)
        target: Final = url.copy_with(
            path=HttpPassThroughEndpointHelpers.join_base_and_endpoint_path(url, f"v3/tasks/{item.object_id}")
        )
        response: Final = await client.get(str(target), headers={"Authorization": f"Bearer {api_key}"})
        if response.status_code in (429, 503):
            return False
        response.raise_for_status()
        parsed: Final = _NativeResponse.model_validate_json(response.content)
        if parsed.code != 0 or parsed.data is None:
            return True
        result: Final = TripoTaskResult.model_validate(parsed.data)
        if result.task_id != item.object_id:
            return True
        await store.settle_tripo_task(item, result, datetime.now(timezone.utc))
    except (httpx.HTTPError, ValidationError):
        verbose_proxy_logger.warning(
            "Tripo task poll failed for %s; the persisted task will be retried", item.storage_id
        )
    return True
