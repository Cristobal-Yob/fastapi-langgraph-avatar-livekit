"""Pinned public SDK composition; fixed local graph, own keys, no implicit creates."""
import asyncio
import contextlib
from datetime import timedelta
import hashlib
import json
import math
import re
import time

import aiohttp
import httpx
from livekit import api, rtc
from livekit.agents import (Agent, AgentSession, APIConnectOptions, APIStatusError, APIConnectionError,
                            APITimeoutError, llm, stt, tts, utils)
from livekit.agents.voice import room_io
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.voice.avatar import DataStreamAudioOutput
from livekit.plugins import elevenlabs

from .service import Rejected, TrialError, failure_metadata

CORE = "http://127.0.0.1:18080/chat"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise TrialError("invalid_response")
    return value


def http_failure(status):
    if status in {401, 403}: return "authentication_failed"
    if status == 429: return "rate_limited"
    if status == 408: return "timeout"
    return "provider_rejected" if 400 <= status < 500 else "provider_error"


def livekit_failure(error, stage):
    if isinstance(error, api.TwirpError):
        status = error.status
        return TrialError(http_failure(status), stage=stage, http_status=status)
    return TrialError("timeout" if isinstance(error, TimeoutError) else "connection_error", stage=stage)


def idle_default_dispatch(value, room):
    """Recognize only LiveKit's inert default definition, not evidence of worker isolation."""
    if type(value) is not api.AgentDispatch:
        return False
    known = api.AgentDispatch()
    known.CopyFrom(value)
    known.DiscardUnknownFields()
    if known.SerializeToString(deterministic=True) != value.SerializeToString(deterministic=True):
        return False
    # Non-default or newly recognized settings must never silently become trusted.
    if {field.name for field, _ in value.ListFields()} - {"id", "room", "state"}:
        return False
    return (bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value.id)) and value.room == room
        and not value.agent_name and not value.metadata and not value.deployment and not value.attributes
        and value.restart_policy == api.JRP_ON_FAILURE and value.HasField("state")
        and not value.state.jobs and value.state.created_at > 0 and value.state.deleted_at == 0
        and {field.name for field, _ in value.state.ListFields()} == {"created_at"})


class CoreLLM(llm.LLM):
    """SDK LLM interface only; all reasoning stays in the original HTTP graph."""
    def __init__(self, gateway):
        super().__init__()
        self.gateway = gateway

    @property
    def model(self): return "original-langgraph-adapter"

    @property
    def provider(self): return "local-original-api"

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        if tools: raise TrialError("tools_not_allowed", 400)
        return CoreStream(self, chat_ctx=chat_ctx, tools=[],
                          conn_options=APIConnectOptions(max_retry=0, timeout=20))


class CoreStream(llm.LLMStream):
    async def _run(self):
        current = next((item for item in reversed(self.chat_ctx.items)
                        if item.type == "message" and item.role == "user"), None)
        if current is None or any(not isinstance(item, str) for item in current.content):
            raise TrialError("invalid_turn", 400)
        text = await self._llm.gateway.core(current.id, current.text_content)
        self._event_ch.send_nowait(llm.ChatChunk(id=current.id,
                                  delta=llm.ChoiceDelta(role="assistant", content=text)))


class GraphAgent(Agent):
    def __init__(self, output_tts):
        super().__init__(instructions="Delegate every answer to the configured local graph.")
        self.output_tts = output_tts

    async def tts_node(self, text, model_settings):
        # The core returns completed JSON; synthesize that one bounded answer once.
        parts, size = [], 0
        async for value in text:
            if not isinstance(value, str): raise TrialError("invalid_reply")
            size += len(value)
            if size > 1000: raise TrialError("invalid_reply")
            parts.append(value)
        async with self.output_tts.synthesize("".join(parts), conn_options=APIConnectOptions(max_retry=0, timeout=15)) as stream:
            async for event in stream:
                yield event.frame


class Gateway:
    def __init__(self, settings, *, http=None, livekit_api=None, room=None, clock=time.time):
        self.config, self.clock = settings, clock
        self.http = http or httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=8,
                                             transport=httpx.AsyncHTTPTransport(retries=0))
        self.lk, self.room = livekit_api, room
        self.session = self.stt = self.tts = self.aio = None
        self.trial = None
        self.listeners = []
        self.session_listeners = []
        self.microphones = []
        self.input_epoch = 0

    def identities(self, record):
        return {"owner": "owner-" + record["id"], "agent": "connector-" + record["id"],
                "avatar": "face-" + record["id"]}

    def token(self, record, role):
        identities = self.identities(record)
        grant = api.VideoGrants(room_join=True, room=record["room"], can_subscribe=True,
                                can_publish=True, can_publish_data=True,
                                can_publish_sources=["microphone"] if role != "avatar" else ["microphone", "camera"])
        token = (api.AccessToken(self.config.livekit_api_key, self.config.livekit_api_secret)
                 .with_identity(identities[role]).with_name(identities[role]).with_grants(grant)
                 .with_ttl(timedelta(seconds=max(1, min(100, int(record["expires"] - self.clock()))))))
        if role != "owner": token = token.with_kind("agent")
        if role == "avatar": token = token.with_attributes({"lk.publish_on_behalf": identities["agent"]})
        return token.to_jwt()

    async def create_room(self, record):
        remaining = record["expires"] - self.clock()
        if remaining <= 0: raise TrialError("session_stopped", 409)
        self.ensure_api()
        try:
            result = await self.lk.room.create_room(api.CreateRoomRequest(name=record["room"],
                            empty_timeout=min(100, self.config.duration, math.ceil(remaining)),
                            departure_timeout=5, max_participants=3))
        except (api.TwirpError, aiohttp.ClientError, TimeoutError) as error:
            raise livekit_failure(error, "create_room") from None
        return identifier(result.sid)

    def ensure_api(self):
        if self.lk is None:
            self.lk = api.LiveKitAPI(url=self.config.livekit_url, api_key=self.config.livekit_api_key,
                                    api_secret=self.config.livekit_api_secret,
                                    timeout=aiohttp.ClientTimeout(total=5), failover=False)

    async def check_dispatch(self, record):
        try:
            dispatches = await self.lk.agent_dispatch.list_dispatch(record["room"])
        except (api.TwirpError, aiohttp.ClientError, TimeoutError) as error:
            raise livekit_failure(error, "check_dispatch") from None
        if (type(dispatches) is not list or len(dispatches) > 1
                or dispatches and not idle_default_dispatch(dispatches[0], record["room"])):
            raise Rejected("unexpected_dispatch", 409, stage="check_dispatch")

    async def connect_room(self, record):
        await self.check_dispatch(record)
        if self.room is None: self.room = rtc.Room()
        try:
            await self.room.connect(self.config.livekit_url, self.token(record, "agent"),
                                    options=rtc.RoomOptions(auto_subscribe=False))
        except (rtc.ConnectError, aiohttp.ClientError, TimeoutError) as error:
            raise livekit_failure(error, "connect_room") from None
        self.watch_participants()
        await self.check_dispatch(record)
        if any(p.identity not in set(self.identities(record).values()) for p in self.room.remote_participants.values()):
            raise Rejected("unexpected_participant", 409)

    async def tavus(self, method, path, body=None, *, empty=False):
        try:
            async with asyncio.timeout(7):
                async with self.http.stream(method, "https://tavusapi.com/v2" + path, json=body,
                                            headers={"x-api-key": self.config.tavus_key}, timeout=7) as response:
                    status = response.status_code
                    if status in {400, 401, 403, 404, 408, 409, 422, 429}:
                        raise Rejected(http_failure(status), http_status=status)
                    if not 200 <= status < 300:
                        raise TrialError(http_failure(status), http_status=status)
                    if empty: return {}
                    size, chunks = 0, []
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 32768: raise TrialError("invalid_response")
                        chunks.append(chunk)
                    value = json.loads(b"".join(chunks))
                    if not isinstance(value, dict): raise TrialError("invalid_response")
                    return value
        except httpx.TimeoutException:
            raise TrialError("timeout") from None
        except httpx.TransportError:
            raise TrialError("connection_error") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TrialError("invalid_response") from None

    async def create_pal(self, record):
        value = await self.tavus("POST", "/pals", {"pal_name": record["room"],
                    "default_face_id": self.config.face_id, "pipeline_mode": "echo",
                    "layers": {"transport": {"transport_type": "livekit"}}})
        return identifier(value.get("pal_id"))

    async def create_conversation(self, record):
        value = await self.tavus("POST", "/conversations", {"pal_id": record["pal_id"],
                    "face_id": self.config.face_id, "conversation_name": record["room"],
                    "properties": {"livekit_ws_url": self.config.livekit_url,
                        "livekit_room_token": self.token(record, "avatar"),
                        "max_call_duration": max(1, min(100, int(record["expires"] - self.clock()))),
                        "participant_absent_timeout": 30, "participant_left_timeout": 0,
                        "enable_recording": False, "auto_start_recording": False, "enable_closed_captions": False}})
        return identifier(value.get("conversation_id"))

    def join(self, record):
        return {"url": self.config.livekit_url, "token": self.token(record, "owner"),
                "owner_identity": self.identities(record)["owner"], "avatar_identity": self.identities(record)["avatar"],
                "agent_identity": self.identities(record)["agent"]}

    async def core(self, turn_id, text):
        trial = self.trial
        if (not trial or trial.stopping or trial.remaining() <= 0 or not isinstance(text, str)
                or not 1 <= len(text.strip()) <= 2000 or not isinstance(turn_id, str)):
            raise TrialError("invalid_turn", 409)
        if trial.inflight or turn_id in trial.seen or trial.core_attempts >= 8:
            raise TrialError("duplicate_or_busy", 409)
        trial.inflight = True
        trial.seen.add(turn_id)
        trial.core_attempts += 1
        task = asyncio.current_task()
        trial.tasks.add(task)
        deadline = max(0.01, min(30, trial.remaining()))
        try:
            async with asyncio.timeout(deadline):
                from cloud.client import ask
                result = await ask(text.strip(), "avatar-lk-"+trial.record["id"],
                    request_id="avlk-"+trial.record["id"]+"-"+hashlib.sha256(turn_id.encode()).hexdigest()[:16],
                    audio_capable=True, response_delivery="audio")
                value = result.get("response")
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
                    raise TrialError("invalid_response", stage="runtime_llm")
                if trial.stopping or trial.remaining() <= 0: raise TrialError("session_stopped", 409)
                trial.completed_turns += 1
                return value.strip()
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise TrialError("timeout", stage="runtime_llm") from None
        except httpx.TransportError:
            raise TrialError("connection_error", stage="runtime_llm") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TrialError("invalid_response", stage="runtime_llm") from None
        except TrialError:
            raise
        except Exception:
            raise TrialError("internal_error", stage="runtime_llm") from None
        finally:
            trial.inflight = False
            trial.tasks.discard(task)

    def watch_participants(self):
        trial = self.trial
        record, identities = trial.record, self.identities(trial.record)
        allowed = set(identities.values())
        def abort():
            if not trial.stopping:
                asyncio.create_task(trial.stop())
        def participant_joined(participant):
            if participant.identity not in allowed: abort()
        def participant_left(participant):
            if participant.identity in {identities["owner"], identities["avatar"]}: abort()
        for event, handler in (("participant_connected", participant_joined), ("participant_disconnected", participant_left),
                               ("disconnected", lambda *_: abort())):
            self.room.on(event, handler)
            self.listeners.append((event, handler))

    def watch_microphone(self):
        trial, room = self.trial, self.room
        owner = self.identities(trial.record)["owner"]
        self.input_epoch += 1
        generation = self.input_epoch
        def published(publication, participant):
            if (trial.stopping or generation != self.input_epoch or room is not self.room or participant.identity != owner
                    or publication.kind != rtc.TrackKind.KIND_AUDIO
                    or publication.source != rtc.TrackSource.SOURCE_MICROPHONE):
                return
            if not any(value is publication for value in self.microphones):
                self.microphones.append(publication)
                publication.set_subscribed(True)
        def joined(participant):
            for publication in participant.track_publications.values(): published(publication, participant)
        for event, handler in (("track_published", published), ("participant_connected", joined)):
            room.on(event, handler)
            self.listeners.append((event, handler))
        for participant in room.remote_participants.values(): joined(participant)

    async def connect(self, trial):
        self.trial = trial
        record, identities = trial.record, self.identities(trial.record)
        allowed = set(identities.values())
        if any(p.identity not in allowed for p in self.room.remote_participants.values()):
            raise Rejected("unexpected_participant", 409)
        await self.check_dispatch(record)
        await utils.wait_for_participant(room=self.room, identity=identities["avatar"])
        await utils.wait_for_track_publication(room=self.room, identity=identities["avatar"], kind=rtc.TrackKind.KIND_VIDEO)
        self.aio = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.stt = elevenlabs.STT(api_key=self.config.elevenlabs_key, model=self.config.stt_model,
                                 enable_logging=False, server_vad={}, language_code="es", http_session=self.aio)
        self.tts = elevenlabs.TTS(api_key=self.config.elevenlabs_key, voice_id=self.config.voice_id,
                                 model=self.config.tts_model, encoding="pcm_24000", enable_logging=False,
                                 http_session=self.aio)
        no_retry = APIConnectOptions(max_retry=0, timeout=15)
        model = CoreLLM(self)
        self.session = AgentSession(stt=self.stt, llm=model, tts=self.tts, turn_detection="stt",
                       preemptive_generation=False, use_tts_aligned_transcript=False,
                       conn_options=SessionConnectOptions(stt_conn_options=no_retry, llm_conn_options=no_retry,
                                                         tts_conn_options=no_retry, max_unrecoverable_errors=1))
        self.session.output.replace_audio_tail(DataStreamAudioOutput(room=self.room,
                   destination_identity=identities["avatar"], sample_rate=24000, wait_remote_track=rtc.TrackKind.KIND_VIDEO))
        def failed(event):
            if trial.stopping: return
            error, source = getattr(event, "error", None), getattr(event, "source", None)
            stage = "runtime_session"
            for provider, error_type, name in ((self.stt, stt.STTError, "runtime_stt"),
                    (self.tts, tts.TTSError, "runtime_tts"), (model, llm.LLMError, "runtime_llm")):
                if source is provider and isinstance(error, error_type):
                    if error.recoverable is True: return  # The SDK owns recovery, not the local Stop action.
                    stage = name
                    break
            cause = getattr(error, "error", None)
            status = cause.status_code if isinstance(cause, APIStatusError) else None
            if type(status) is not int or not 400 <= status <= 599: status = None
            code = (http_failure(status) if status else "timeout" if isinstance(cause, APITimeoutError)
                    else "connection_error" if isinstance(cause, APIConnectionError) else "internal_error")
            if not trial.record.get("failure"):
                detail = failure_metadata(TrialError(code, stage=stage, http_status=status, recoverable=False), stage)
                if stage == "runtime_llm" and isinstance(cause, TrialError):
                    detail = failure_metadata(cause, stage)
                    detail.update(stage=stage, recoverable=False)
                trial.record["failure"] = detail
                trial.save_cleanup()
            asyncio.create_task(trial.stop())
        def closed(event):
            if trial.stopping: return
            if getattr(event, "error", None) is not None:
                failed(event)
            else:
                asyncio.create_task(trial.stop())
        for event, handler in (("error", failed), ("close", closed)):
            self.session.on(event, handler)
            self.session_listeners.append((event, handler))
        self.watch_microphone()
        await self.session.start(agent=GraphAgent(self.tts),
            room=self.room, record=False, session_host=False, capture_run=False,
            room_options=room_io.RoomOptions(participant_identity=identities["owner"], audio_input=True,
                participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD],
                video_input=False, text_input=True, text_output=False, audio_output=False,
                close_on_disconnect=False, delete_room_on_close=False))

    async def close_session(self):
        self.input_epoch += 1
        if self.room:
            for event, handler in self.listeners: self.room.off(event, handler)
        self.listeners.clear()
        for publication in self.microphones:
            with contextlib.suppress(Exception): publication.set_subscribed(False)
        self.microphones.clear()
        if self.session:
            for event, handler in self.session_listeners: self.session.off(event, handler)
            self.session_listeners.clear()
            await self.session.aclose()
        if self.room and self.room.isconnected(): await self.room.disconnect()
        if self.stt: await self.stt.aclose()
        if self.tts: await self.tts.aclose()
        if self.aio: await self.aio.close()

    async def end_conversation(self, record):
        await self.tavus("POST", "/conversations/" + identifier(record["conversation_id"]) + "/end", {}, empty=True)

    async def delete_pal(self, record):
        await self.tavus("DELETE", "/pals/" + identifier(record["pal_id"]), empty=True)

    async def delete_room(self, record):
        self.ensure_api()
        await self.lk.room.delete_room(api.DeleteRoomRequest(room=record["room"]))

    async def aclose(self):
        await self.close_session()
        if self.lk: await self.lk.aclose()
        await self.http.aclose()
