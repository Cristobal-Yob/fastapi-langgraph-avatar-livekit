"""Single-operator access; no public signup or client-selected ownership."""
import hmac
import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def validate_token(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value)
            or len(set(value)) < 16 or value.lower().startswith(('replace', 'example', 'change'))):
        raise ValueError('AGENT_API_TOKEN must be a freshly generated 32-byte URL-safe secret.')
    return value


def public_origin():
    value = os.environ.get('PUBLIC_ORIGIN', '')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
        raise ValueError('PUBLIC_ORIGIN must be the exact HTTPS application origin, without a trailing slash.')
    return value


def authenticate(request: Request, expected):
    supplied = request.headers.get('authorization', '')
    if not hmac.compare_digest(supplied.encode(), ('Bearer '+expected).encode()):
        raise HTTPException(401, 'operator_authentication_required')


def browser_origin(request: Request):
    if request.headers.get('origin') != public_origin():
        raise HTTPException(403, 'trusted_origin_required')
