from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class TripoOwner(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key: str
    user_id: str | None = None
    team_id: str | None = None
    org_id: str | None = None
    project_id: str | None = None
    end_user_id: str | None = None


class TripoObject(BaseModel):
    model_config = ConfigDict(frozen=True)

    object_id: str
    scope: str
    owner: TripoOwner
    created_at: datetime
    model: str
    is_task: bool = True
    usd_per_credit: Decimal = Field(gt=0, allow_inf_nan=False)

    @property
    def storage_id(self) -> str:
        return f"tripo:{self.scope}:{self.object_id}"


class TripoTaskResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    status: str
    credits_consumed: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class TripoObjectStore(Protocol):
    async def tripo_budget_exhausted(self, owner: TripoOwner) -> bool: ...

    async def save_tripo_object(self, item: TripoObject) -> None: ...

    async def get_tripo_object(self, scope: str, object_id: str) -> TripoObject | None: ...

    async def claim_tripo_tasks(self, scope: str, now: datetime, limit: int = 20) -> tuple[TripoObject, ...]: ...

    async def settle_tripo_task(self, item: TripoObject, result: TripoTaskResult, now: datetime) -> None: ...
