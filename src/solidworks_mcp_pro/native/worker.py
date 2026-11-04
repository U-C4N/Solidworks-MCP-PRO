"""A bounded, drainable COM apartment with explicit thread ownership."""
from __future__ import annotations

from concurrent.futures import Future
import os
from queue import Empty, Full, Queue
import threading
from typing import Callable

from .errors import CadError


class StaWorker:
    def __init__(self, factory: Callable, *, capacity: int = 128):
        if type(capacity) is not int or capacity <= 0:
            raise CadError("INVALID_QUEUE_CAPACITY", "Native queue capacity must be a positive integer.")
        self._queue = Queue(maxsize=capacity)
        self._closing = False
        self._state_lock = threading.Lock()
        self._ready = Future()
        self._thread = threading.Thread(target=self._run, args=(factory,), name="PRO native CAD STA", daemon=True)
        self._thread.start()
        self.session = self._ready.result(timeout=20)

    @property
    def thread_id(self):
        return self._thread.ident

    def _run(self, factory):
        pythoncom = None
        initialized = False
        try:
            if os.name == "nt":
                import pythoncom
                pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
                initialized = True
            session = factory()
            self._ready.set_result(session)
        except BaseException as exc:
            self._ready.set_exception(exc)
            if initialized:
                pythoncom.CoUninitialize()
            return
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.03)
                except Empty:
                    if pythoncom is not None:
                        pythoncom.PumpWaitingMessages()
                    continue
                if item is None:
                    self._queue.task_done()
                    break
                future, function = item
                try:
                    if future.set_running_or_notify_cancel():
                        try:
                            future.set_result(function())
                        except BaseException as exc:
                            future.set_exception(exc)
                finally:
                    self._queue.task_done()
                if pythoncom is not None:
                    pythoncom.PumpWaitingMessages()
        finally:
            try:
                session.dispose()
            finally:
                if pythoncom is not None:
                    pythoncom.CoUninitialize()

    def submit(self, function: Callable) -> Future:
        future = Future()
        with self._state_lock:
            if self._closing:
                future.set_exception(CadError("WORKER_CLOSED", "The native worker is shutting down."))
                return future
            try:
                self._queue.put_nowait((future, function))
            except Full:
                future.set_exception(CadError("NATIVE_QUEUE_FULL", "Native work queue is full; inspect running operations."))
        return future

    def shutdown(self):
        if threading.get_ident() == self._thread.ident:
            raise CadError("WORKER_SHUTDOWN_CONTEXT", "Drain and close the native worker from its owning application thread.")
        with self._state_lock:
            if self._closing:
                return
            self._closing = True
        # Shutdown is called only after the application has drained its jobs.
        self._queue.put(None)
        if threading.get_ident() != self._thread.ident:
            self._thread.join()

