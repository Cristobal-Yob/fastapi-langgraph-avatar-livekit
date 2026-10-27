"""Loopback owner controls only; no public callback, docs or provider secrets."""
import asyncio
import hmac
import json
from pathlib import Path
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .service import TrialError


async def prepare_start(trial):
    import importlib
    # Importing the large SDK must not delay Heroku's first response byte.
    await asyncio.to_thread(importlib.import_module, 'avatar.sdk')
    await trial.start()


def equal(left, right):
    return isinstance(left, str) and len(left) <= 512 and hmac.compare_digest(left.encode(), right.encode())


async def body(request):
    chunks, size = [], 0
    async with asyncio.timeout(5):
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536: raise TrialError("request_too_large", 413)
            chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except Exception:
        raise TrialError("invalid_request", 400) from None


def create_app(trial):
    from cloud.security import public_origin
    from urllib.parse import urlsplit
    origin = public_origin()
    trusted_host = urlsplit(origin).netloc
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    csrf, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    failures = 0
    start_pending = False
    pending_start_task = None

    async def safe_error(request, error):
        return JSONResponse({"error": error.code if isinstance(error, TrialError) else "unavailable"},
                            status_code=error.status if isinstance(error, TrialError) else 503,
                            headers={"Cache-Control": "no-store"})
    app.add_exception_handler(TrialError, safe_error)
    app.add_exception_handler(Exception, safe_error)

    @app.middleware("http")
    async def boundary(request, next_call):
        host = request.headers.get("host")
        document_entry = (request.method == "GET" and request.url.path == "/"
                          and request.headers.get("sec-fetch-site") == "cross-site"
                          and request.headers.get("sec-fetch-mode") == "navigate"
                          and request.headers.get("sec-fetch-dest") == "document")
        if (host != trusted_host
                or (request.headers.get("sec-fetch-site") not in {None, "none", "same-origin"} and not document_entry)
                or request.headers.get("origin") not in {None, origin}
                or request.method != "GET" and (request.headers.get("origin") != origin
                    or not equal(request.headers.get("x-csrf-token"), csrf))):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        response = await next_call(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Permissions-Policy": 'camera=(), microphone=(self), display-capture=()',
            "Content-Security-Policy": "default-src 'none'; script-src 'self' https://unpkg.com; style-src 'self'; connect-src 'self' wss://*.livekit.cloud https://*.livekit.cloud; worker-src 'self' blob:; media-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
        return response

    def owner(request):
        if not equal(request.cookies.get("avatar_livekit_owner"), cookie):
            raise TrialError("owner_authentication_required", 403)

    @app.get("/")
    async def index(): return FileResponse(Path(__file__).with_name("index.html"))

    @app.get("/app.js")
    async def script(): return FileResponse(Path(__file__).with_name("app.js"), media_type="text/javascript")

    @app.get("/style.css")
    async def style(): return FileResponse(Path(__file__).with_name("style.css"), media_type="text/css")

    @app.get("/health")
    async def health(): return {"status": "ok"}

    @app.get("/api/bootstrap")
    async def bootstrap(): return {"csrf": csrf}

    @app.post("/api/unlock")
    async def unlock(request: Request):
        nonlocal failures
        data = await body(request)
        if failures >= 5 or not isinstance(data, dict) or set(data) != {"key"} or not equal(data.get("key"), trial.config.owner_key):
            failures = min(5, failures + 1)
            raise TrialError("owner_authentication_required", 403)
        response = Response(status_code=204)
        response.set_cookie("avatar_livekit_owner", cookie, httponly=True, secure=True, samesite="strict", max_age=7200)
        return response

    @app.get("/api/status")
    async def status(request: Request):
        owner(request)
        return {**trial.status(), 'start_pending':start_pending}

    @app.post("/api/start")
    async def start(request: Request):
        nonlocal start_pending, pending_start_task
        owner(request)
        if await body(request) != {"confirmed": True}: raise TrialError("invalid_request", 400)
        if not trial.config.enabled: raise TrialError('real_sessions_disabled', 403)
        if not trial.config.dispatch_isolation_confirmed: raise TrialError('dispatch_isolation_required', 403)
        if start_pending or trial.spent: raise TrialError('trial_already_reserved', 409)
        start_pending = True
        async def launch():
            nonlocal start_pending
            try:
                await prepare_start(trial)
            except Exception:
                if hasattr(trial, 'record'):
                    trial.record['status'] = 'failed'
                    trial.save_cleanup()
            finally:
                start_pending = False
        pending_start_task = asyncio.create_task(launch())
        return JSONResponse({**trial.status(), 'start_pending':True}, status_code=202)

    @app.post("/api/join")
    async def join(request: Request):
        owner(request)
        if await body(request) != {}: raise TrialError("invalid_request", 400)
        return trial.join()

    @app.post("/api/stop")
    async def stop(request: Request):
        owner(request)
        if await body(request) != {}: raise TrialError("invalid_request", 400)
        if pending_start_task and not pending_start_task.done(): pending_start_task.cancel()
        asyncio.create_task(trial.stop())
        return JSONResponse(trial.status(), status_code=202)

    return app
