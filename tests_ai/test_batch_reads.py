"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

The harness's setup-time savings: BatchClient fetches tools/list and the
fixture lookups once per batch (copies out, a failed fetch is retried),
leaves every other call alone, and observe_all reads side by side while
keeping the keys' order. Nothing touches the network.
"""
from __future__ import annotations

import threading
import time

import pytest

from harness import fixtures, verifiers
from harness.batch_client import BatchClient, memo


class FakeClient:
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def call(self, name, arguments=None):
        with self.lock:
            self.calls.append(name)
        time.sleep(self.delay)
        if name == "Item.list":
            return {"data": [{"id": "i1", "code": "A"}], "total": 1}
        if name == "Party.list":
            return {"data": [{"id": "p1", "company_id": "c1"}]}
        if name == "AgentSession.list":
            return {"total": 3}
        return {"id": (arguments or {}).get("id"), "stage": "new", "status": "new"}

    def list_tools(self):
        with self.lock:
            self.calls.append("tools/list")
        time.sleep(self.delay)
        return [{"name": "Deal.get"}]


def test_tools_list_is_fetched_once_and_handed_out_as_copies():
    fake = FakeClient()
    client = BatchClient(fake)
    first = client.list_tools()
    first.append({"name": "mutated"})
    assert client.list_tools() == [{"name": "Deal.get"}]
    assert fake.calls == ["tools/list"]


def test_other_calls_always_reach_the_platform():
    fake = FakeClient()
    client = BatchClient(fake)
    client.call("Deal.get", {"id": "d1"})
    client.call("Deal.get", {"id": "d1"})
    assert fake.calls == ["Deal.get", "Deal.get"]


def test_a_failed_fetch_is_not_remembered():
    client = BatchClient(FakeClient())
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("blip")
        return "ok"

    with pytest.raises(RuntimeError):
        client.memo("k", flaky)
    assert client.memo("k", flaky) == "ok"
    assert client.memo("k", flaky) == "ok"
    assert len(attempts) == 2


def test_memo_on_a_plain_client_fetches_every_time():
    fetched = []
    memo(FakeClient(), "k", lambda: fetched.append(1))
    memo(FakeClient(), "k", lambda: fetched.append(1))
    assert len(fetched) == 2


def test_fixture_lookups_are_shared_across_a_batch():
    fake = FakeClient()
    client = BatchClient(fake)
    assert fixtures._all_items(client) == fixtures._all_items(client)
    assert fake.calls.count("Item.list") == 1


def test_observe_all_reads_in_parallel_and_keeps_key_order():
    keys = ["sessions_t6", "deal_state:d1", "lead_state:l1", "deal:d2", "tools_list"]
    fake = FakeClient(delay=0.3)
    t0 = time.monotonic()
    out = verifiers.observe_all(BatchClient(fake), keys)
    assert time.monotonic() - t0 < 0.3 * len(keys) / 2
    assert list(out["observations"]) == keys
    assert out["observations"]["sessions_t6"] == {"total": 3}
    assert out["observations"]["deal_state:d1"]["row"]["id"] == "d1"
