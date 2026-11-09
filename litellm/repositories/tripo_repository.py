from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from itertools import chain
from typing import Final

from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.litellm_core_utils.safe_json_dumps import safe_dumps
from litellm.models.tripo import TripoObject, TripoOwner, TripoTaskResult
from litellm.repositories.table_repositories import ManagedObjectRepository, SpendLogsRepository


class _QueryDatabase(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    query_raw: Callable[..., Awaitable[object]]


class _WriterClient(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    writer_db: _QueryDatabase


_QUERY_ROWS: Final = TypeAdapter(tuple[Mapping[str, object], ...])


def _tripo_object(value: object) -> TripoObject:
    return TripoObject.model_validate_json(value) if isinstance(value, str) else TripoObject.model_validate(value)


class TripoRepository:
    def __init__(self, prisma_client: object) -> None:
        self._objects = ManagedObjectRepository(prisma_client, use_writer=True)
        self._spend_logs = SpendLogsRepository(prisma_client)
        self._database = _WriterClient.model_validate(prisma_client).writer_db

    async def tripo_budget_exhausted(self, owner: TripoOwner) -> bool:
        rows: Final = await self._database.query_raw(
            "SELECT 1 FROM ("
            'SELECT spend, max_budget FROM "LiteLLM_VerificationToken" WHERE token=$1 '
            'UNION ALL SELECT spend, max_budget FROM "LiteLLM_TeamTable" WHERE team_id=$2 '
            'UNION ALL SELECT spend, max_budget FROM "LiteLLM_UserTable" WHERE user_id=$3 '
            'UNION ALL SELECT o.spend, b.max_budget FROM "LiteLLM_OrganizationTable" o '
            'JOIN "LiteLLM_BudgetTable" b ON b.budget_id=o.budget_id WHERE o.organization_id=$4 '
            'UNION ALL SELECT p.spend, b.max_budget FROM "LiteLLM_ProjectTable" p '
            'JOIN "LiteLLM_BudgetTable" b ON b.budget_id=p.budget_id WHERE p.project_id=$5 '
            ") AS budgets WHERE max_budget IS NOT NULL AND spend >= max_budget LIMIT 1",
            owner.api_key,
            owner.team_id,
            owner.user_id,
            owner.org_id,
            owner.project_id,
        )
        return bool(rows)

    async def save_tripo_object(self, item: TripoObject) -> None:
        await self._objects.table.create_many(
            data=[
                {
                    "unified_object_id": item.storage_id,
                    "model_object_id": item.storage_id,
                    "file_object": item.model_dump_json(),
                    "file_purpose": "tripo_task" if item.is_task else "tripo_file",
                    "status": "pending",
                    "api_key": item.owner.api_key,
                    "created_by": item.owner.user_id,
                    "team_id": item.owner.team_id,
                    "org_id": item.owner.org_id,
                }
            ],
            skip_duplicates=True,
        )

    async def get_tripo_object(self, scope: str, object_id: str) -> TripoObject | None:
        row: Final = await self._objects.table.find_unique(where={"unified_object_id": f"tripo:{scope}:{object_id}"})
        return None if row is None else _tripo_object(row.file_object)

    async def claim_tripo_tasks(self, scope: str, now: datetime, limit: int = 20) -> tuple[TripoObject, ...]:
        rows: Final = await self._objects.table.find_many(
            where={
                "file_purpose": "tripo_task",
                "batch_processed": False,
                "unified_object_id": {"startswith": f"tripo:{scope}:"},
                "OR": [{"status": "pending"}, {"updated_at": {"lt": now - timedelta(minutes=5)}}],
            },
            order={"updated_at": "asc"},
            take=limit,
        )
        claimed: Final = tuple([await self._claim_tripo_task(row.unified_object_id, row.updated_at) for row in rows])
        return tuple(item for item in claimed if item is not None)

    async def _claim_tripo_task(self, storage_id: str, updated_at: datetime) -> TripoObject | None:
        changed: Final = await self._objects.table.update_many(
            where={"unified_object_id": storage_id, "updated_at": updated_at, "batch_processed": False},
            data={"status": "polling"},
        )
        if not changed:
            return None
        row: Final = await self._objects.table.find_unique(where={"unified_object_id": storage_id})
        return None if row is None else _tripo_object(row.file_object)

    async def _tripo_accounting_windows(self, owner: TripoOwner) -> tuple[Mapping[str, object], ...]:
        from litellm.proxy.spend_tracking.budget_reservation import fixed_budget_window

        scopes: Final = _QUERY_ROWS.validate_python(
            await self._database.query_raw(
                "SELECT 'key' AS entity_type, token AS entity_id, budget_limits FROM \"LiteLLM_VerificationToken\" WHERE token=$1 "
                "UNION ALL SELECT 'team', team_id, budget_limits FROM \"LiteLLM_TeamTable\" WHERE team_id=$2",
                owner.api_key,
                owner.team_id,
            )
        )
        rows: Final = TypeAdapter(tuple[object, ...])
        return tuple(
            chain.from_iterable(
                tuple(
                    {
                        "entity_type": scope["entity_type"],
                        "entity_id": scope["entity_id"],
                        "window_duration": fixed[0],
                        "window_start": fixed[1].isoformat(),
                    }
                    for window in rows.validate_python(scope["budget_limits"])
                    if (fixed := fixed_budget_window(window)) is not None
                )
                for scope in scopes
                if scope["budget_limits"] is not None
            )
        )

    async def settle_tripo_task(self, item: TripoObject, result: TripoTaskResult, now: datetime) -> None:
        terminal: Final = result.status in ("success", "failed", "cancelled", "banned", "expired")
        if not terminal or (result.status == "success" and result.credits_consumed is None):
            await self._objects.table.update_many(
                where={"unified_object_id": item.storage_id}, data={"status": "pending"}
            )
            return
        credits: Final = result.credits_consumed if result.status == "success" else 0
        accounting: Final = {
            "status": "success" if result.status == "success" else "failure",
            "usage_object": {"credits_consumed": float(credits or 0)},
            "tripo_task_id": item.object_id,
            "tripo_task_status": result.status,
            "tripo_usd_per_credit": str(item.usd_per_credit),
            "accounting_windows": await self._tripo_accounting_windows(item.owner),
            "accounting_entities": {
                "api_key": item.owner.api_key,
                "user": item.owner.user_id,
                "team_id": item.owner.team_id,
                "organization_id": item.owner.org_id,
                "project_id": item.owner.project_id,
                "end_user": item.owner.end_user_id,
            },
        }
        row: Final = {
            "request_id": f"{item.storage_id}:cost",
            "call_type": "pass_through",
            "api_key": item.owner.api_key,
            "spend": float((credits or 0) * item.usd_per_credit),
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "startTime": item.created_at.isoformat(),
            "endTime": now.isoformat(),
            "model": item.model,
            "model_group": item.model,
            "custom_llm_provider": "tripo",
            "user": item.owner.user_id,
            "team_id": item.owner.team_id,
            "organization_id": item.owner.org_id,
            "end_user": item.owner.end_user_id,
            "status": accounting["status"],
            "metadata": accounting,
        }
        await self._spend_logs.insert_with_deliveries(
            [{**row, "metadata": safe_dumps(accounting), "accounting_payload": row}]
        )
        await self._objects.table.update_many(
            where={"unified_object_id": item.storage_id},
            data={"status": result.status, "batch_processed": True},
        )
