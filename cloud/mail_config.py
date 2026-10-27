import os
import re
SCOPES = {"openid", "email", "https://www.googleapis.com/auth/gmail.send"}

def single_address(value: str) -> str:
    """Deliberately support one ASCII mailbox, not display names or address lists."""
    if not isinstance(value, str) or len(value) > 254 or value.count("@") != 1:
        raise ValueError("invalid_address")
    local, domain = value.split("@")
    if (not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", local)
            or local.startswith(".") or local.endswith(".") or ".." in local
            or "." not in domain or len(domain) > 253
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in domain.split("."))):
        raise ValueError("invalid_address")
    return value



class Settings:
    def __init__(self):
        self.sender = single_address(os.environ['GMAIL_SENDER'])
        self.project_id = os.environ['GMAIL_PROJECT_ID']
        self.client_id = os.environ['GMAIL_CLIENT_ID']
        self.real_send_enabled = os.environ.get('EMAIL_ENABLE_REAL_SEND', 'false') == 'true'
