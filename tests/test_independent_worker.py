"""STA queue lifecycle regressions; pythoncom is always an in-memory fake."""
from concurrent.futures import CancelledError
import sys
import threading
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.native.errors import CadError
from solidworks_mcp_pro.native.session import NativeSession
from solidworks_mcp_pro.native import worker as worker_module


class Apartment:
    COINIT_APARTMENTTHREADED = 2

    def __init__(self):
        self.calls = []
        self.initialization_error = None
        self.thread = None

    def CoInitializeEx(self, mode):
        self.thread = threading.current_thread()
        self.calls.append(("initialize", threading.get_ident(), mode))
        if self.initialization_error:
            raise self.initialization_error

    def CoUninitialize(self):
        self.calls.append(("uninitialize", threading.get_ident()))

    def PumpWaitingMessages(self):
        self.calls.append(("pump", threading.get_ident()))


@pytest.fixture
def apartment(monkeypatch):
    fake = Apartment()
    monkeypatch.setitem(sys.modules, "pythoncom", fake)
    monkeypatch.setattr(worker_module, "os", SimpleNamespace(name="nt"))
    return fake


class OwnedSession:
    def __init__(self):
        self.owner = threading.get_ident()
        self.disposals = []

    def current_owner(self):
        if threading.get_ident() != self.owner:
            raise CadError("WRONG_COM_THREAD", "Session accessed outside its owner.")
        return self.owner

    def dispose(self):
        self.disposals.append(self.current_owner())


def test_factory_job_and_disposal_stay_on_same_initialized_apartment(apartment):
    worker = worker_module.StaWorker(OwnedSession)
    try:
        assert worker.thread_id != threading.get_ident()
        assert worker.session.owner == worker.thread_id
        assert worker.submit(worker.session.current_owner).result(timeout=2) == worker.thread_id
        with pytest.raises(CadError, match="WRONG_COM_THREAD"):
            worker.session.current_owner()
    finally:
        worker.shutdown()
    assert worker.session.disposals == [worker.thread_id]
    assert [call[0] for call in apartment.calls].count("initialize") == 1
    assert [call[0] for call in apartment.calls].count("uninitialize") == 1
    assert {call[1] for call in apartment.calls} == {worker.thread_id}


def test_native_session_is_created_on_worker_and_rejects_direct_caller_use(apartment):
    worker = worker_module.StaWorker(NativeSession)
    try:
        assert worker.submit(worker.session._check_thread).result(timeout=2) is None
        with pytest.raises(CadError, match="WRONG_COM_THREAD"):
            worker.session._check_thread()
    finally:
        worker.shutdown()
    assert worker.session._sw is None


def test_queued_jobs_execute_fifo_and_exceptions_do_not_stop_following_jobs(apartment):
    worker = worker_module.StaWorker(OwnedSession)
    order = []

    def failing():
        order.append(2)
        raise ValueError("native call failure")

    try:
        first = worker.submit(lambda: order.append(1))
        second = worker.submit(failing)
        third = worker.submit(lambda: order.append(3))
        assert first.result(timeout=2) is None
        with pytest.raises(ValueError, match="native call failure"):
            second.result(timeout=2)
        assert third.result(timeout=2) is None
        assert order == [1, 2, 3]
    finally:
        worker.shutdown()


def test_cancelled_queued_job_is_never_called_and_does_not_block_drain(apartment):
    worker = worker_module.StaWorker(OwnedSession, capacity=2)
    started, release = threading.Event(), threading.Event()
    called = []

    def blocking():
        started.set()
        assert release.wait(2)
        return "first completed"

    try:
        first = worker.submit(blocking)
        assert started.wait(2)
        cancelled = worker.submit(lambda: called.append("cancelled"))
        after = worker.submit(lambda: called.append("following"))
        assert cancelled.cancel() is True
        release.set()
        assert first.result(timeout=2) == "first completed"
        with pytest.raises(CancelledError):
            cancelled.result(timeout=2)
        after.result(timeout=2)
        assert called == ["following"]
    finally:
        release.set()
        worker.shutdown()


def test_running_native_call_cannot_be_cancelled_halfway_through(apartment):
    worker = worker_module.StaWorker(OwnedSession)
    started, release = threading.Event(), threading.Event()

    def blocking():
        started.set()
        assert release.wait(2)
        return "mutation finished"

    try:
        future = worker.submit(blocking)
        assert started.wait(2)
        assert future.cancel() is False
        release.set()
        assert future.result(timeout=2) == "mutation finished"
    finally:
        release.set()
        worker.shutdown()


def test_capacity_rejects_overflow_instead_of_losing_or_running_work(apartment):
    worker = worker_module.StaWorker(OwnedSession, capacity=1)
    started, release = threading.Event(), threading.Event()
    called = []

    def blocking():
        started.set()
        assert release.wait(2)

    try:
        first = worker.submit(blocking)
        assert started.wait(2)
        pending = worker.submit(lambda: called.append("accepted"))
        rejected = worker.submit(lambda: called.append("overflow"))
        with pytest.raises(CadError, match="NATIVE_QUEUE_FULL"):
            rejected.result(timeout=2)
        release.set()
        first.result(timeout=2)
        pending.result(timeout=2)
        assert called == ["accepted"]
    finally:
        release.set()
        worker.shutdown()


def test_shutdown_drains_accepted_jobs_and_refuses_new_jobs(apartment, monkeypatch):
    worker = worker_module.StaWorker(OwnedSession, capacity=3)
    started, release, closing = threading.Event(), threading.Event(), threading.Event()
    order = []

    def blocking():
        started.set()
        assert release.wait(2)
        order.append(1)

    original_put = worker._queue.put

    def signal_shutdown(item, *args, **kwargs):
        if item is None:
            closing.set()
        return original_put(item, *args, **kwargs)

    monkeypatch.setattr(worker._queue, "put", signal_shutdown)
    closer = threading.Thread(target=worker.shutdown, daemon=True)
    try:
        first = worker.submit(blocking)
        assert started.wait(2)
        second = worker.submit(lambda: order.append(2))
        third = worker.submit(lambda: order.append(3))
        closer.start()
        assert closing.wait(2)
        rejected = worker.submit(lambda: order.append(4))
        with pytest.raises(CadError, match="WORKER_CLOSED"):
            rejected.result(timeout=2)
        release.set()
        for future in (first, second, third):
            future.result(timeout=2)
        closer.join(timeout=2)
        assert not closer.is_alive()
        assert order == [1, 2, 3]
        assert worker.session.disposals == [worker.thread_id]
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(timeout=2)
        worker.shutdown()


def test_repeated_shutdown_disposes_once_and_never_reopens_queue(apartment):
    worker = worker_module.StaWorker(OwnedSession)
    worker.shutdown()
    worker.shutdown()
    assert worker.session.disposals == [worker.thread_id]
    with pytest.raises(CadError, match="WORKER_CLOSED"):
        worker.submit(lambda: "late").result(timeout=2)


def test_factory_failure_releases_successfully_initialized_com_apartment(apartment):
    def broken_factory():
        raise ValueError("session construction failed")

    with pytest.raises(ValueError, match="session construction failed"):
        worker_module.StaWorker(broken_factory)
    apartment.thread.join(timeout=2)
    assert not apartment.thread.is_alive()
    initialized = [call for call in apartment.calls if call[0] == "initialize"]
    uninitialized = [call for call in apartment.calls if call[0] == "uninitialize"]
    assert len(initialized) == len(uninitialized) == 1
    assert initialized[0][1] == uninitialized[0][1]


def test_failed_com_initialization_is_not_uninitialized_again(apartment):
    apartment.initialization_error = RuntimeError("apartment initialization failed")
    factory_calls = []
    with pytest.raises(RuntimeError, match="apartment initialization failed"):
        worker_module.StaWorker(lambda: factory_calls.append(True))
    apartment.thread.join(timeout=2)
    assert not apartment.thread.is_alive()
    assert factory_calls == []
    assert [call[0] for call in apartment.calls] == ["initialize"]


def test_shutdown_cannot_deadlock_by_enqueuing_sentinel_from_worker(apartment):
    worker = worker_module.StaWorker(OwnedSession, capacity=1)
    try:
        with pytest.raises(CadError, match="WORKER_SHUTDOWN_CONTEXT"):
            worker.submit(worker.shutdown).result(timeout=2)
        assert worker.submit(lambda: "still accepting").result(timeout=2) == "still accepting"
    finally:
        worker.shutdown()


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_capacity_must_be_positive_to_keep_queue_bounded(apartment, capacity):
    created = None
    try:
        with pytest.raises((ValueError, CadError)):
            created = worker_module.StaWorker(OwnedSession, capacity=capacity)
    finally:
        if created is not None:
            created.shutdown()
