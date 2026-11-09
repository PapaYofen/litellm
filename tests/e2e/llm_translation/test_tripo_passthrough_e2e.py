import os
from collections.abc import Iterator
from typing import Final

import pytest
from e2e_config import unique_marker
from e2e_http import UnknownApiError, unwrap
from e2e_metadata import Domain, Route, Subject, meta
from lifecycle import ResourceManager
from models import KeyGenerateBody, TripoGenerationBody
from proxy_client import ProxyClient
from tripo_client import TripoClient, TripoOwnerKey

TRIPO_MODEL: Final = os.environ.get("TRIPO_MODEL", "")


@pytest.fixture
def tripo_resources(proxy: ProxyClient) -> Iterator[ResourceManager]:
    manager: Final = ResourceManager(client=proxy, strict_cleanup=True)
    yield manager
    manager.teardown()


@pytest.mark.e2e
@pytest.mark.tripo
@pytest.mark.provider_live
@pytest.mark.flaky(reruns=0)
@meta(Subject(domain=Domain.PASSTHROUGH, route=Route.PASSTHROUGH, models=(TRIPO_MODEL,)))
def test_tripo_task_is_owned_and_charged_at_captured_rate(proxy: ProxyClient, tripo_resources: ResourceManager) -> None:
    assert TRIPO_MODEL.strip(), "Set TRIPO_MODEL to the supported Tripo model to test"
    assert proxy.poll_timeout > 0 and proxy.poll_interval > 0, "E2E polling durations must be positive"
    client: Final = TripoClient(proxy)
    marker: Final = unique_marker()
    owner_alias: Final = f"tripo-test-owner-{marker}"
    owner: Final = TripoOwnerKey(client, proxy.generate_key(KeyGenerateBody(key_alias=owner_alias)), owner_alias)
    tripo_resources.defer(owner.cleanup)
    foreign_alias: Final = f"tripo-test-foreign-{marker}"
    foreign: Final = proxy.generate_key(KeyGenerateBody(key_alias=foreign_alias))
    tripo_resources.defer(lambda: client.delete_key(foreign))
    owner_baseline: Final = proxy.key_info(owner.key).spend
    foreign_baseline: Final = proxy.key_info(foreign).spend
    assert owner_baseline is not None and foreign_baseline is not None

    task_id: Final = owner.generate(
        TripoGenerationBody(model=TRIPO_MODEL, prompt=os.environ.get("TRIPO_PROMPT", "a simple low-poly wooden chair"))
    )
    _ = unwrap(client.task(owner.key, task_id))
    for rejected in (client.task(foreign, task_id), client.list_task(foreign, task_id)):
        assert isinstance(rejected, UnknownApiError) and rejected.status_code == 404, "Foreign key can access the task"
    completed: Final = client.poll_task(owner.key, task_id)
    assert completed.credits_consumed is not None
    row: Final = client.poll_accounting(
        owner.key, foreign, task_id, completed.credits_consumed, owner_baseline, foreign_baseline
    )
    owner.mark_accounted()
    assert row.metadata is not None
