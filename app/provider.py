import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from langchain_core.exceptions import ModelAPIError, ModelRateLimitError
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph import RouteDecision, RoutingResult, GeneratedReply, build_graph

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_project_env():
    load_dotenv(ENV_FILE, override=False)


load_project_env()


class ConfigurationError(ValueError):
    pass


def configuration():
    mode = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").strip().lower()
    if mode not in ("true", "false"):
        raise ConfigurationError("GOOGLE_GENAI_USE_VERTEXAI must be true or false.")
    primary = os.getenv("MODELO", "gemini-3.5-flash-lite").strip()
    fallback = os.getenv("FALLBACK_MODEL", "gemini-3.5-flash").strip()
    if not primary or not fallback or max(len(primary), len(fallback)) > 160 or primary == fallback:
        raise ConfigurationError("MODELO and FALLBACK_MODEL must be distinct, nonblank model names (max 160 characters).")
    if mode == "true":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip()
        if not project or not location:
            raise ConfigurationError("Set GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION for Vertex ADC.")
        # Explicit project/location and api_key=None keep ambient API keys out of Vertex ADC mode.
        kwargs = dict(vertexai=True, project=project, location=location, api_key=None)
        backend = "vertex"
    else:
        key = os.getenv("GOOGLE_API_KEY", "").strip()
        if not key:
            raise ConfigurationError("Set GOOGLE_API_KEY in the project .env file.")
        kwargs = dict(vertexai=False, api_key=key)
        backend = "developer"
    return primary, fallback, backend, kwargs


def is_configured() -> bool:
    try:
        configuration()
    except ConfigurationError:
        return False
    return True


TRANSIENT_ERRORS = (ModelRateLimitError, ModelAPIError, httpx.TimeoutException, httpx.NetworkError)


class FallbackRouter:
    def __init__(self, primary, fallback, primary_model, fallback_model, backend):
        self.primary, self.fallback = primary, fallback
        self.primary_model, self.fallback_model = primary_model, fallback_model
        self.backend = backend

    async def ainvoke(self, messages):
        used_model, fallback_used = self.primary_model, False
        try:
            decision = await self.primary.ainvoke(messages)
        except TRANSIENT_ERRORS as exc:
            # Unknown or permanent server codes fail closed; no alternate call.
            if isinstance(exc, ModelAPIError) and getattr(exc, "code", None) not in (500, 502, 503, 504):
                raise
            decision = await self.fallback.ainvoke(messages)
            used_model, fallback_used = self.fallback_model, True
        return RoutingResult(**decision.model_dump(), provider="google", backend=self.backend,
                             model_used=used_model, fallback_used=fallback_used)


class FallbackResponder(FallbackRouter):
    async def ainvoke(self, messages):
        used_model, fallback_used = self.primary_model, False
        try:
            result = await self.primary.ainvoke(messages)
        except TRANSIENT_ERRORS as exc:
            if isinstance(exc, ModelAPIError) and getattr(exc, "code", None) not in (500, 502, 503, 504):
                raise
            result = await self.fallback.ainvoke(messages)
            used_model, fallback_used = self.fallback_model, True
        if getattr(result, "tool_calls", None):
            raise ValueError("Response generation may not execute tools.")
        text = result.content
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict) and part.get("type") == "text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Invalid response content.")
        return GeneratedReply(response=text.strip(), response_model_used=used_model,
                              response_fallback_used=fallback_used)


def create_graph():
    primary, fallback, backend, kwargs = configuration()

    def model(name):
        return ChatGoogleGenerativeAI(
            model=name, **kwargs, temperature=0, timeout=30, max_retries=0,
        )

    primary_chat, fallback_chat = model(primary), model(fallback)
    return build_graph(FallbackRouter(
        primary_chat.with_structured_output(RouteDecision), fallback_chat.with_structured_output(RouteDecision),
        primary, fallback, backend,
    ), FallbackResponder(primary_chat, fallback_chat, primary, fallback, backend))
