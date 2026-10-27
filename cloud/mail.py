"""One explicit Gmail effect after graph routing and response generation."""
import asyncio
import hashlib
import hmac
import json
import os
import time


def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'))


class MailRuntime:
    def __init__(self, *, mode='direct', allowed=False):
        self.mode, self.allowed, self.store = mode, allowed, None

    @property
    def enabled(self): return self.allowed and os.environ.get('EMAIL_ENABLE_REAL_SEND', 'false') == 'true'

    def close(self):
        if self.store: self.store.close()

    async def dispatch(self, *, recipient, subject, body, conversation_id, action_key, order_digest, authorized):
        if not self.enabled: return {'status':'disabled','delivery':'not_attempted'}
        if not authorized: return {'status':'authorization_required','delivery':'not_attempted'}
        try:
            from cloud.store import DurableStore
            from cloud.mail_config import single_address
            single_address(recipient)
            if self.store is None:
                self.store = DurableStore('gmail-host:'+os.environ['STATE_NAMESPACE'], singleton=True)
            data = {'to':recipient,'subject':subject,'body':body,'issued_at':time.time(),
                    'expires_at':time.time()+180,'action_key':action_key,'order_digest':order_digest}
            # Exclude time from the identity: a restart cannot turn an old action into a new one.
            digest = hashlib.sha256(canonical({k:v for k,v in data.items() if not k.endswith('_at')}).encode()).hexdigest()
            status = await asyncio.to_thread(self.store.claim, action_key, digest)
            if status == 'conflict': return {'status':'action_conflict','delivery':'not_attempted'}
            if status != 'new': return {'status':status if status in {'accepted','rejected'} else 'unknown',
                                        'delivery':'not_repeated'}
            await asyncio.to_thread(self.store.alive)
            if self.mode == 'mcp':
                from cloud.mcp_client import send
                outcome = await send(data)
            else:
                outcome = await direct_send(data)
            status = outcome['status']
            if status in {'accepted','rejected','not_attempted'}:
                await asyncio.to_thread(self.store.finish, action_key, status)
            return outcome
        except Exception:
            # A reservation may exist. Never turn uncertainty into automatic replay.
            return {'status':'unknown' if self.store else 'configuration_required','delivery':'not_attempted'}


async def direct_send(data):
    from cloud.gmail import GmailTransport
    from cloud.mail_config import Settings
    result = await GmailTransport(Settings()).send(data)
    return {'status':result.status,'delivery':'accepted_by_gmail' if result.status == 'accepted' else 'not_confirmed'}
