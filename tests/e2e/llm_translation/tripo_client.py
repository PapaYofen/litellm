from __future__ import annotations

import hashlib
import time
import warnings
from dataclasses import dataclass, field
from typing import Final

import pytest
from e2e_http import NoBody, Result, unwrap, without_retries
from e2e_metadata import step
from models import (
    KeyDeleteBody,
    SpendLogsParams,
    TripoGenerationBody,
    TripoGenerationResponse,
    TripoSpendLogs,
    TripoSpendRow,
    TripoTask,
    TripoTaskListBody,
    TripoTaskResponse,
)
from proxy_client import ProxyClient


@dataclass(frozen=True, slots=True)
class TripoClient:
    proxy: ProxyClient

    @step("Create a Tripo generation using {body.model}")
    def create(self, key: str, body: TripoGenerationBody) -> str:
        with without_retries():
            return unwrap(
                self.proxy.transport.post(
                    "/tripo/v3/generation/text-to-model",
                    headers=self.proxy.transport.bearer(key),
                    json=body,
                    response_type=TripoGenerationResponse,
                )
            ).data.task_id

    @step("Read Tripo task {task_id}")
    def task(self, key: str, task_id: str) -> Result[TripoTaskResponse]:
        return self.proxy.transport.get(
            f"/tripo/v3/tasks/{task_id}",
            headers=self.proxy.transport.bearer(key),
            params=NoBody(),
            response_type=TripoTaskResponse,
        )

    @step("Query Tripo task {task_id} through the batch endpoint")
    def list_task(self, key: str, task_id: str) -> Result[NoBody]:
        return self.proxy.transport.post(
            "/tripo/v3/tasks/list",
            headers=self.proxy.transport.bearer(key),
            json=TripoTaskListBody(task_ids=(task_id,)),
            response_type=NoBody,
        )

    @step("Wait for Tripo task {task_id} to complete")
    def poll_task(self, key: str, task_id: str) -> TripoTask:
        deadline: Final = time.monotonic() + self.proxy.poll_timeout
        while time.monotonic() < deadline:
            if (task := self._completed_task(key, task_id)) is not None:
                return task
            time.sleep(self.proxy.poll_interval)
        pytest.fail(f"Tripo task {task_id} did not complete within {self.proxy.poll_timeout} seconds")

    def _completed_task(self, key: str, task_id: str) -> TripoTask | None:
        task: Final = unwrap(self.task(key, task_id)).data
        if task.status == "success":
            assert task.credits_consumed is not None, f"Task {task_id} succeeded without credits_consumed"
            return task
        assert task.status not in {"failed", "cancelled", "banned", "expired"}, (
            f"Task {task_id} ended with status {task.status}"
        )
        return None

    @step("Read the key's Tripo spend rows")
    def spend_rows(self, key: str, task_id: str) -> tuple[TripoSpendRow, ...]:
        digest: Final = hashlib.sha256(key.encode()).hexdigest()
        logs: Final = unwrap(
            self.proxy.transport.get(
                "/spend/logs",
                headers=self.proxy.management_headers(),
                params=SpendLogsParams(api_key=digest),
                response_type=TripoSpendLogs,
            )
        )
        return tuple(row for row in logs.root if row.metadata is not None and row.metadata.tripo_task_id == task_id)

    @step("Wait for Tripo task {task_id} accounting and key isolation")
    def poll_accounting(
        self,
        owner: str,
        foreign: str,
        task_id: str,
        credits: float,
        owner_baseline: float,
        foreign_baseline: float,
    ) -> TripoSpendRow:
        deadline: Final = time.monotonic() + self.proxy.poll_timeout
        while time.monotonic() < deadline:
            if (
                row := self._accounting(owner, foreign, task_id, credits, owner_baseline, foreign_baseline)
            ) is not None:
                return row
            time.sleep(self.proxy.poll_interval)
        pytest.fail(f"Task {task_id} accounting did not settle within {self.proxy.poll_timeout} seconds")

    def _accounting(
        self, owner: str, foreign: str, task_id: str, credits: float, owner_baseline: float, foreign_baseline: float
    ) -> TripoSpendRow | None:
        rows: Final = self.spend_rows(owner, task_id)
        assert len(rows) <= 1, f"Task {task_id} has {len(rows)} final spend rows"
        foreign_spend: Final = self.proxy.key_info(foreign).spend
        assert foreign_spend is not None
        assert abs(foreign_spend - foreign_baseline) < 1e-9, "Foreign key was charged"
        if rows:
            row: Final = rows[0]
            assert row.metadata is not None
            rate: Final = row.metadata.tripo_usd_per_credit
            usage: Final = row.metadata.usage_object
            assert rate is not None and usage is not None, "Final task spend is missing its rate or usage"
            assert usage.credits_consumed == credits, "Logged credits differ from the provider"
            expected: Final = credits * rate
            assert abs(row.spend - expected) < 1e-9, "Final task spend differs from credits times captured rate"
            owner_spend: Final = self.proxy.key_info(owner).spend
            assert owner_spend is not None
            if abs(owner_spend - owner_baseline - expected) < 1e-9:
                return row
        return None

    @step("Delete the Tripo test key")
    def delete_key(self, key: str) -> None:
        _ = unwrap(
            self.proxy.transport.post(
                "/key/delete",
                headers=self.proxy.management_headers(),
                json=KeyDeleteBody(keys=[key]),
                response_type=NoBody,
            )
        )


@dataclass(slots=True)
class TripoOwnerKey:
    client: TripoClient
    key: str = field(repr=False)
    alias: str
    accounting_pending: bool = False

    @step("Submit a generation under test key {self.alias}")
    def generate(self, body: TripoGenerationBody) -> str:
        self.accounting_pending = True
        return self.client.create(self.key, body)

    def mark_accounted(self) -> None:
        self.accounting_pending = False

    @step("Clean up the Tripo owner key {self.alias} after accounting")
    def cleanup(self) -> None:
        if self.accounting_pending:
            warnings.warn(
                f"Owner key {self.alias} retained because task accounting is incomplete. "
                "Reconcile the task and spend as an administrator before deleting it",
                stacklevel=2,
            )
            return
        self.client.delete_key(self.key)
