"""One Gmail send attempt after bounded user OAuth and identity verification."""
import asyncio
import base64
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
import json
import re
import time
import httpx
from cloud.mail_config import SCOPES
import os

TOKEN_URL = "https://oauth2.googleapis.com/token"
IDENTITY_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


@dataclass(frozen=True)
class SendResult:
    status: str
    provider_id: str | None = None
    error_code: str | None = None


class GmailAuthorizationError(Exception):
    """Internal category only; never retain provider errors or token payloads."""
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def normalized_scopes(value):
    scopes = value.split() if isinstance(value, str) else value
    if not isinstance(scopes, (list, tuple, set)) or any(not isinstance(s, str) for s in scopes):
        raise ValueError("gmail_authorization_failed")
    return {"email" if s == "https://www.googleapis.com/auth/userinfo.email" else s for s in scopes}


def load_credentials(settings):
    """Read only for explicit authorization checks or enabled sends; never ADC."""
    try:
        data = json.loads(os.environ["GMAIL_OAUTH_JSON"])
        if (not isinstance(data, dict) or data.get("type") != "authorized_user"
                or data.get("project_id") != settings.project_id
                or data.get("account_email") != settings.sender
                or data.get("client_id") != settings.client_id or not settings.client_id
                or data.get("token_uri") != TOKEN_URL or normalized_scopes(data.get("scopes")) != SCOPES
                or any(not isinstance(data.get(k), str) or not 1 <= len(data[k]) <= 4096
                       for k in ("client_secret", "refresh_token", "account_sub"))):
            raise ValueError("gmail_authorization_failed")
        return data
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("gmail_authorization_failed") from None


def raw_message(draft, sender):
    message = EmailMessage(policy=SMTP)
    message["From"], message["To"], message["Subject"] = sender, draft["to"], draft["subject"]
    message["Date"] = formatdate(usegmt=True)
    message["MIME-Version"] = "1.0"
    message.set_type("text/plain")
    message.set_param("charset", "utf-8")
    message["Content-Transfer-Encoding"] = "base64"
    # set_content would append a newline, changing the approved body.
    message.set_payload(base64.encodebytes(draft["body"].encode("utf-8")).decode("ascii"))
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailTransport:
    def __init__(self, settings, *, client=None, clock=time.time):
        self.settings, self.client, self.clock = settings, client, clock

    async def _authorization_headers(self, client):
        try:
            credentials = load_credentials(self.settings)
        except Exception:
            raise GmailAuthorizationError("binding_mismatch") from None
        try:
            async with asyncio.timeout(10):
                response = await client.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "client_id": credentials["client_id"],
                    "client_secret": credentials["client_secret"], "refresh_token": credentials["refresh_token"]
                }, timeout=4, follow_redirects=False)
                response.raise_for_status()
                token = response.json()
                if not isinstance(token, dict):
                    raise GmailAuthorizationError("authorization_invalid")
                try:
                    scopes = normalized_scopes(token.get("scope"))
                except ValueError:
                    raise GmailAuthorizationError("scope_mismatch") from None
                if scopes != SCOPES:
                    raise GmailAuthorizationError("scope_mismatch")
                if (token.get("token_type", "").lower() != "bearer"
                        or not isinstance(token.get("access_token"), str)
                        or not 1 <= len(token["access_token"]) <= 16384):
                    raise GmailAuthorizationError("authorization_invalid")
                headers = {"Authorization": "Bearer " + token["access_token"]}
                identity = await client.get(IDENTITY_URL, headers=headers, timeout=4, follow_redirects=False)
                identity.raise_for_status()
                info = identity.json()
                if (not isinstance(info, dict) or info.get("email") != self.settings.sender
                        or info.get("sub") != credentials["account_sub"] or info.get("email_verified") is not True):
                    raise GmailAuthorizationError("identity_mismatch")
                return headers
        except GmailAuthorizationError:
            raise
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            category = ("grant_rejected" if status in {400, 401, 403} else
                        "transient_unavailable" if status in {408, 429} or status >= 500 else
                        "authorization_invalid")
            raise GmailAuthorizationError(category) from None
        except (httpx.TimeoutException, httpx.NetworkError, TimeoutError):
            raise GmailAuthorizationError("transient_unavailable") from None
        except Exception:
            raise GmailAuthorizationError("authorization_invalid") from None

    async def check_authorization(self):
        """Explicit auth-only check: at most one refresh and one identity request, no mail."""
        owned = self.client is None
        client = self.client
        try:
            if client is None:
                client = httpx.AsyncClient(timeout=4, follow_redirects=False)
            await self._authorization_headers(client)
            result = {"status": "ready"}
        except GmailAuthorizationError as error:
            result = {"status": "not_ready", "category": error.category}
        except Exception:
            result = {"status": "not_ready", "category": "authorization_invalid"}
        finally:
            if owned and client is not None:
                try:
                    await client.aclose()
                except Exception:
                    result = {"status": "not_ready", "category": "transient_unavailable"}
        return result

    async def send(self, draft):
        if not self.settings.real_send_enabled:
            return SendResult("not_attempted", error_code="send_disabled")
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=4, follow_redirects=False)
        try:
            try:
                headers = await self._authorization_headers(client)
            except Exception:
                return SendResult("not_attempted", error_code="gmail_authorization_failed")
            if not draft["issued_at"] <= self.clock() < draft["expires_at"]:
                return SendResult("not_attempted", error_code="approval_expired")
            try:
                # Exactly one POST. No retry, redirects, or fallback transport.
                response = await client.post(SEND_URL, headers=headers,
                                             json={"raw": raw_message(draft, self.settings.sender)}, timeout=8)
                if 400 <= response.status_code < 500 and response.status_code not in {408, 409, 429}:
                    return SendResult("rejected", error_code="provider_rejected")
                if response.status_code != 200:
                    return SendResult("unknown", error_code="delivery_unknown")
                data = response.json()
                provider_id = data.get("id") if isinstance(data, dict) else None
                if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", provider_id):
                    raise ValueError("invalid_acceptance")
                return SendResult("accepted", provider_id=provider_id)
            except Exception:
                return SendResult("unknown", error_code="delivery_unknown")
        finally:
            if owned:
                await client.aclose()
