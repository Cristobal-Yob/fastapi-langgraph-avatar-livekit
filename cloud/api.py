"""Authenticated async jobs: HTTP returns before router/responder/fallback work."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import time
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.memory import ConversationStore
from cloud.security import authenticate, validate_token

Identifier = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_-]{1,64}$')]


class ChatInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    request_id: Identifier
    conversation_id: Identifier | None = None
    response_delivery: Literal['text', 'audio'] = 'text'
    audio_capable: bool = False


def create_app(*, graph=None, mail=None):
    token = validate_token(os.environ.get('AGENT_API_TOKEN', ''))
    memory = ConversationStore()
    jobs, requests, tasks = {}, {}, set()
    variant = os.environ.get('APP_VARIANT', 'base')
    expected_origin = os.environ.get('PUBLIC_ORIGIN','')
    if mail is None:
        from cloud.mail import MailRuntime
        mail = MailRuntime(mode='mcp' if variant == 'gmail-mcp' else 'direct', allowed=variant in {'gmail','gmail-mcp'})

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in tuple(tasks): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        mail.close()

    app = FastAPI(title='Autonomous LangGraph teaching API', docs_url=None,
                  redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({'detail':'invalid_request'}, status_code=422)

    @app.middleware('http')
    async def boundary(request, next_call):
        public = request.url.path == '/health' or (variant == 'frontend' and request.method == 'GET' and request.url.path == '/')
        if not public:
            try: authenticate(request, token)
            except HTTPException as error:
                return JSONResponse({'detail':error.detail}, status_code=error.status_code)
            if request.headers.get('origin') and request.headers['origin'] != expected_origin:
                return JSONResponse({'detail':'trusted_origin_required'}, status_code=403)
            try: size = int(request.headers.get('content-length', '0'))
            except ValueError: size = 16385
            if size > 16384:
                return JSONResponse({'detail':'request_too_large'}, status_code=413)
            if request.method in {'POST','PUT','PATCH'}:
                # Bound chunked bodies as well, before Pydantic can retain their contents.
                chunks, total = [], 0
                try:
                    async with asyncio.timeout(5):
                        async for chunk in request.stream():
                            total += len(chunk)
                            if total > 16384: return JSONResponse({'detail':'request_too_large'}, status_code=413)
                            chunks.append(chunk)
                except TimeoutError: return JSONResponse({'detail':'request_timeout'}, status_code=408)
                request._body = b''.join(chunks)
        response = await next_call(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        return response

    @app.get('/health')
    async def health(): return {'status':'ok'}

    async def execute(job, payload, conversation_id):
        nonlocal graph
        try:
            async with asyncio.timeout(150):
                async with memory.use(conversation_id) as conversation:
                    if graph is None:
                        from app.provider import create_graph
                        graph = create_graph()
                    with tracing_context(enabled=False):
                        result = await graph.ainvoke({'message':payload.message,
                            'history':list(conversation.turns),'conversation_id':conversation_id,
                            'request_id':payload.request_id,'email_pending':conversation.pending_email,
                            'email_now':time.time(),'email_authorized':True,'email_dispatch':mail.dispatch,
                            'response_delivery':payload.response_delivery,'audio_capable':payload.audio_capable})
                    keys = ('route','reason','response','provider','backend','model_used','fallback_used',
                            'response_model_used','response_fallback_used','email')
                    answer = {key:result[key] for key in keys if key in result}
                    answer['conversation_id'] = conversation_id
                    conversation.pending_email = result.get('email_pending')
                    conversation.turns.append({'user':payload.message,'assistant':result['response']})
                    job.update(status='done', result=answer)
        except asyncio.CancelledError:
            job.update(status='failed', error='interrupted_no_automatic_retry')
            raise
        except Exception:
            job.update(status='failed', error='agent_unavailable_no_automatic_retry')
        finally:
            job['finished'] = time.monotonic()

    @app.post('/chat', status_code=202)
    async def chat(payload: ChatInput):
        if payload.response_delivery == 'audio' and not payload.audio_capable:
            raise HTTPException(422, 'audio_capability_required')
        # Fixed-capacity teaching process; never evict request identities and accidentally replay.
        digest = hashlib.sha256(json.dumps(payload.model_dump(), sort_keys=True).encode()).hexdigest()
        if payload.request_id in requests:
            old_digest, job_id = requests[payload.request_id]
            if old_digest != digest: raise HTTPException(409, 'request_payload_conflict')
            return {'status_url':'/jobs/'+job_id}
        if len(jobs) >= 128 or len(tasks) >= 8:
            raise HTTPException(503, 'capacity_reached')
        job_id = uuid4().hex
        conversation_id = payload.conversation_id or 'req-'+hashlib.sha256(payload.request_id.encode()).hexdigest()[:48]
        jobs[job_id] = {'status':'running'}
        requests[payload.request_id] = (digest, job_id)
        task = asyncio.create_task(execute(jobs[job_id], payload, conversation_id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {'status_url':'/jobs/'+job_id}

    @app.get('/jobs/{job_id}')
    async def status(job_id: str):
        if job_id not in jobs: raise HTTPException(404, 'job_not_found_do_not_replay_effects')
        return {k:v for k,v in jobs[job_id].items() if k != 'finished'}

    @app.delete('/conversations/{conversation_id}', status_code=204)
    async def reset(conversation_id: Identifier):
        try: removed = memory.reset(conversation_id)
        except Exception: raise HTTPException(409, 'conversation_busy') from None
        if not removed: raise HTTPException(404, 'conversation_not_found')
        return Response(status_code=204)

    return app


def application(): return create_app()
