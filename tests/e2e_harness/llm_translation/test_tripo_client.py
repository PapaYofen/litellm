from builtins import ExceptionGroup
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import SimpleQueue
from threading import Thread
from typing import Final

import pytest
from e2e_http import without_retries
from lifecycle import ResourceManager
from llm_translation.tripo_client import TripoClient, TripoOwnerKey
from models import TripoGenerationBody
from proxy_client import build_proxy_client


@contextmanager
def _client(replies: tuple[tuple[int, bytes], ...]) -> Generator[tuple[TripoClient, SimpleQueue[str]]]:
    pending: Final[SimpleQueue[tuple[int, bytes]]] = SimpleQueue()
    received: Final[SimpleQueue[str]] = SimpleQueue()
    for reply in replies:
        pending.put(reply)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            received.put(self.path)
            status, body = pending.get_nowait() if not pending.empty() else (500, b"{}")
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

    server: Final = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread: Final = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    url: Final = f"http://127.0.0.1:{server.server_port}"
    proxy: Final = build_proxy_client(base_url=url, control_plane_base_url=url, replica_urls=(url,), master_key="test")
    try:
        yield TripoClient(replace(proxy, poll_timeout=0.1, poll_interval=0.001)), received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_generation_is_not_retried_and_pending_owner_is_retained() -> None:
    with _client(((503, b"{}"),)) as (client, received):
        owner: Final = TripoOwnerKey(client, "owner", "test-owner")
        resources: Final = ResourceManager(client=client.proxy, strict_cleanup=True)
        resources.defer(owner.cleanup)
        with pytest.raises(AssertionError):
            _ = owner.generate(TripoGenerationBody(model="test-model", prompt="chair"))
        with pytest.warns(UserWarning, match="test-owner retained"):
            resources.teardown()
        assert received.qsize() == 1
        assert received.get_nowait() == "/tripo/v3/generation/text-to-model"


def test_completed_owner_is_deleted_and_cleanup_failure_is_reported() -> None:
    with _client(((200, b'{"code":0,"data":{"task_id":"task"}}'), (403, b"{}"))) as (client, received):
        owner: Final = TripoOwnerKey(client, "owner", "test-owner")
        resources: Final = ResourceManager(client=client.proxy, strict_cleanup=True)
        resources.defer(owner.cleanup)
        assert owner.generate(TripoGenerationBody(model="test-model", prompt="chair")) == "task"
        owner.mark_accounted()
        with pytest.raises(ExceptionGroup, match="Resource cleanup failed"), without_retries():
            resources.teardown()
        assert tuple(received.get_nowait() for _ in range(received.qsize())) == (
            "/tripo/v3/generation/text-to-model",
            "/key/delete",
        )


@pytest.mark.parametrize("credits, spend", ((0, 0.0), (20, 0.2)))
def test_accounting_uses_captured_rate_and_accepts_json_string_metadata(credits: int, spend: float) -> None:
    metadata: Final = (
        '{\\"tripo_task_id\\":\\"task\\",\\"tripo_usd_per_credit\\":0.01,'
        f'\\"usage_object\\":{{\\"credits_consumed\\":{credits}}}}}'
    )
    logs: Final = f'[{{"spend":0,"metadata":{{"usage_object":{{}}}}}},{{"spend":{spend},"metadata":"{metadata}"}}]'
    replies: Final = (
        (200, logs.encode()),
        (200, b'{"info":{"spend":3}}'),
        (200, f'{{"info":{{"spend":{2 + spend}}}}}'.encode()),
    )
    with _client(replies) as (client, received):
        row: Final = client.poll_accounting("owner", "foreign", "task", credits, 2, 3)
        assert row.spend == spend
        assert received.qsize() == 3


@pytest.mark.parametrize(
    "row_spend, foreign_spend, message",
    ((0.4, 3.0, "Final task spend differs"), (0.2, 3.2, "Foreign key was charged")),
)
def test_accounting_rejects_wrong_cost_or_foreign_key_charge(
    row_spend: float, foreign_spend: float, message: str
) -> None:
    logs: Final = (
        f'[{{"spend":{row_spend},"metadata":{{"tripo_task_id":"task","tripo_usd_per_credit":0.01,'
        '"usage_object":{"credits_consumed":20}}}]'
    )
    with _client(((200, logs.encode()), (200, f'{{"info":{{"spend":{foreign_spend}}}}}'.encode()))) as (client, _):
        with pytest.raises(AssertionError, match=message):
            _ = client.poll_accounting("owner", "foreign", "task", 20, 2, 3)


def test_poll_task_rejects_provider_failure_and_success_without_usage() -> None:
    for body in (b'{"code":0,"data":{"status":"failed"}}', b'{"code":0,"data":{"status":"success"}}'):
        with _client(((200, body),)) as (client, received):
            with pytest.raises(AssertionError):
                _ = client.poll_task("owner", "task")
            assert received.qsize() == 1
