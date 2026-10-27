"""One durable, bounded owner trial. Only resource metadata survives restart."""
import asyncio
import contextlib
import json
import os
import math
import re
import tempfile
import time
from uuid import uuid4


class TrialError(Exception):
    def __init__(self, code="unavailable", status=503, *, stage=None, http_status=None, recoverable=None):
        self.code, self.status = code, status
        self.stage, self.http_status, self.recoverable = stage, http_status, recoverable
        super().__init__(code)


class Rejected(TrialError):
    pass


FAILURE_STAGES = {"create_room", "check_dispatch", "connect_room", "create_pal", "create_conversation", "connect_agent",
                  "runtime_stt", "runtime_tts", "runtime_llm", "runtime_session"}
FAILURE_CODES = {"timeout", "cancelled", "connection_error", "authentication_failed", "rate_limited",
                 "provider_rejected", "provider_error", "invalid_response", "unexpected_dispatch",
                 "unexpected_participant", "internal_error"}


def failure_metadata(error, stage):
    """Never stringify exceptions: provider messages may contain credentials or content."""
    code = "internal_error"
    if isinstance(error, asyncio.CancelledError): code = "cancelled"
    elif isinstance(error, TimeoutError): code = "timeout"
    elif isinstance(error, OSError): code = "connection_error"
    if isinstance(error, TrialError):
        if isinstance(error.code, str) and error.code in FAILURE_CODES: code = error.code
        if isinstance(error.stage, str) and error.stage in FAILURE_STAGES: stage = error.stage
    value = {"stage": stage, "code": code}
    if isinstance(error, TrialError) and type(error.http_status) is int and 400 <= error.http_status <= 599:
        value["http_status"] = error.http_status
    if isinstance(error, TrialError) and type(error.recoverable) is bool:
        value["recoverable"] = error.recoverable
    return value


def valid_failure(value):
    return value is None or (isinstance(value, dict) and {"stage", "code"} <= set(value)
        and not set(value) - {"stage", "code", "http_status", "recoverable"}
        and value["stage"] in FAILURE_STAGES and value["code"] in FAILURE_CODES
        and ("recoverable" not in value or type(value["recoverable"]) is bool)
        and ("http_status" not in value or type(value["http_status"]) is int and 400 <= value["http_status"] <= 599))


class Trial:
    def __init__(self, settings, *, gateway=None, clock=time.time):
        self.config, self.gateway, self.clock = settings, gateway, clock
        self.record = {"status": "prepared"}
        self.store = None
        value = None
        if os.environ.get('DATABASE_URL') or settings.enabled:
            from cloud.store import DurableStore
            self.store = DurableStore('avatar-trial:' + os.environ['STATE_NAMESPACE'], singleton=True)
            value = self.store.read('trial')
        self.spent = value is not None
        self.stopping, self.closed = False, False
        self.start_task = None
        self.stop_lock = asyncio.Lock()
        self.completed_turns = self.core_attempts = 0
        self.inflight = False
        self.seen = set()
        self.tasks = set()
        self.corrupt = False
        if self.spent:
            try:
                allowed = {"status", "id", "room", "expires", "room_id", "pal_id", "conversation_id",
                           "pending_create", "uncertain", "conversation_ended", "pal_deleted", "room_deleted", "failure"}
                if not isinstance(value, dict) or set(value) - allowed:
                    raise ValueError
                if (not re.fullmatch(r"[a-f0-9]{32}", value.get("id", ""))
                        or value.get("room") != "avatar-lk-" + value["id"]
                        or type(value.get("expires")) not in {int, float} or not math.isfinite(value["expires"])
                        or value.get("status") not in {"starting", "ready", "active", "stopping", "ended", "failed", "reconcile_required"}):
                    raise ValueError
                for name in ("room_id", "pal_id", "conversation_id"):
                    if name in value and (not isinstance(value[name], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value[name])):
                        raise ValueError
                for name in ("uncertain", "conversation_ended", "pal_deleted", "room_deleted"):
                    if name in value and type(value[name]) is not bool: raise ValueError
                if value.get("pending_create") not in {None, "room", "pal", "conversation"}: raise ValueError
                if not valid_failure(value.get("failure")): raise ValueError
                self.record = value
                if value.get("pending_create"):
                    self.record["uncertain"] = True
                if value.get("status") not in {"ended", "failed"}:
                    self.record["status"] = "reconcile_required"
            except Exception:
                self.corrupt = True
                self.record = {"status": "reconcile_required", "uncertain": True}

    def save(self, *, reserve=False):
        if self.store is None: raise TrialError('durable_database_required')
        self.store.alive()
        self.store.write('trial', self.record, reserve=reserve)

    def save_cleanup(self):
        """Bookkeeping must never prevent cleanup of resources whose IDs are known."""
        try:
            self.save()
            return True
        except Exception:
            return False

    def remaining(self):
        return max(0, self.record.get("expires", 0) - self.clock())

    def status(self):
        return {"status": self.record["status"], "enabled": self.config.enabled, "trial_consumed": self.spent,
                "dispatch_isolation_confirmed": self.config.dispatch_isolation_confirmed,
                "remaining_seconds": max(0, int(self.remaining())), "completed_turns": self.completed_turns,
                "core_attempts": self.core_attempts,
                "failure": self.record.get("failure"), "diagnostic_available": "failure" in self.record,
                "cleanup_confirmed": self.record["status"] == "ended" and not self.record.get("uncertain"),
                "join_available": bool(self.record.get("room_id")) and not self.stopping and self.remaining() > 0
                    and self.record["status"] in {"starting", "ready", "active"}}

    async def start(self):
        if not self.config.enabled: raise TrialError("real_sessions_disabled", 403)
        if not self.config.dispatch_isolation_confirmed: raise TrialError("dispatch_isolation_required", 403)
        if self.spent or self.stopping or self.closed: raise TrialError("trial_already_reserved", 409)
        # SDK imports and local clients are preparation, not a paid resource attempt.
        if self.gateway is None:
            try:
                from .sdk import Gateway
                self.gateway = Gateway(self.config)
            except Exception:
                raise TrialError("local_initialization_failed") from None
        self.spent = True
        sid = uuid4().hex
        self.record = {"status": "starting", "id": sid, "room": "avatar-lk-" + sid,
                       "expires": self.clock() + self.config.duration, "uncertain": False, "failure": None}
        try:
            self.save(reserve=True)
        except Exception:
            self.record["status"] = "reconcile_required"
            raise TrialError("registry_unavailable") from None
        self.gateway.trial = self
        self.start_task = asyncio.create_task(self.launch())
        return self.status()

    async def create(self, stage, field, method):
        self.record["pending_create"] = stage
        self.save()
        try:
            async with asyncio.timeout(max(0.01, min(8, self.remaining()))):
                value = await method(self.record)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError
        except Rejected:
            self.record.pop("pending_create", None)
            raise
        except BaseException:
            self.record["uncertain"] = True
            raise
        # A known remote result is not an unknown create merely because disk I/O fails.
        self.record[field] = value
        self.record.pop("pending_create", None)
        self.save()

    async def launch(self):
        stage = "create_room"
        try:
            await self.create("room", "room_id", self.gateway.create_room)
            stage = "connect_room"
            async with asyncio.timeout(max(0.01, min(8, self.remaining()))):
                await self.gateway.connect_room(self.record)
            stage = "create_pal"
            await self.create("pal", "pal_id", self.gateway.create_pal)
            stage = "create_conversation"
            await self.create("conversation", "conversation_id", self.gateway.create_conversation)
            stage = "connect_agent"
            async with asyncio.timeout(max(0.01, self.remaining())):
                await self.gateway.connect(self)
            if not self.stopping and not self.record.get("failure"):
                self.record["status"] = "ready"
                self.save()
        except (Exception, asyncio.CancelledError) as error:
            if not self.record.get("failure"):
                self.record["failure"] = failure_metadata(error, stage)
            self.record["status"] = "reconcile_required" if self.record.get("uncertain") else "failed"
            self.save_cleanup()
            if not self.stopping:
                await self.stop()

    def join(self):
        if not self.status()["join_available"] or self.record["status"] in {"ended", "failed", "reconcile_required"}:
            raise TrialError("join_unavailable", 409)
        return self.gateway.join(self.record)

    async def stop(self):
        if not self.spent:
            return self.status()
        self.stopping = True
        async with self.stop_lock:
            task = self.start_task
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError): await task
            for pending in tuple(self.tasks):
                if pending is not asyncio.current_task(): pending.cancel()
            if not self.spent or self.corrupt:
                return self.status()
            if self.record["status"] == "ended": return self.status()
            if self.gateway is None:
                from .sdk import Gateway
                self.gateway = Gateway(self.config)
            self.record["status"] = "stopping"
            failed = not self.save_cleanup()
            for method, required, confirmed in (("close_session", None, None),
                    ("end_conversation", "conversation_id", "conversation_ended"),
                    ("delete_pal", "pal_id", "pal_deleted"), ("delete_room", "room_id", "room_deleted")):
                if required and (not self.record.get(required) or self.record.get(confirmed)): continue
                try:
                    async with asyncio.timeout(4):
                        if required: await getattr(self.gateway, method)(self.record)
                        else: await getattr(self.gateway, method)()
                    if confirmed: self.record[confirmed] = True
                except Exception:
                    failed = True
                if not self.save_cleanup(): failed = True
            self.record["status"] = "reconcile_required" if failed or self.record.get("uncertain") else "ended"
            if not self.save_cleanup(): self.record["status"] = "reconcile_required"
            return self.status()

    async def expire(self):
        pending_cleanup = any(self.record.get(required) and not self.record.get(confirmed)
            for required, confirmed in (("conversation_id", "conversation_ended"), ("pal_id", "pal_deleted"), ("room_id", "room_deleted")))
        if self.spent and not self.stopping and self.remaining() <= 0 and (pending_cleanup
                or self.record["status"] not in {"ended", "failed", "reconcile_required"}):
            await self.stop()

    async def watchdog(self):
        while not self.closed:
            await self.expire()
            await asyncio.sleep(0.25)

    async def close(self):
        if not self.closed:
            await self.stop()
            if self.gateway:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(4): await self.gateway.aclose()
            self.closed = True
            if self.store: self.store.close()
