"""Submit once, poll authenticated same-origin jobs; never resend on uncertainty."""
import asyncio
import os
import re
from uuid import uuid4
import httpx


async def ask(message, conversation_id, *, request_id=None, response_delivery='text', audio_capable=False,
              http=None, base_url='http://127.0.0.1:18080'):
    owned = http is None
    client = http or httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False)
    headers = {'Authorization':'Bearer '+os.environ['AGENT_API_TOKEN']}
    try:
        response = await client.post(base_url+'/chat', headers=headers, json={
            'message':message,'conversation_id':conversation_id,'request_id':request_id or uuid4().hex,
            'response_delivery':response_delivery,'audio_capable':audio_capable})
        response.raise_for_status()
        path = response.json()['status_url']
        if not re.fullmatch(r'/jobs/[a-f0-9]{32}', path): raise ValueError('invalid_job')
        async with asyncio.timeout(155):
            while True:
                response = await client.get(base_url+path, headers=headers)
                response.raise_for_status()
                value = response.json()
                if value['status'] == 'done': return value['result']
                if value['status'] == 'failed': raise ValueError('agent_unavailable_no_automatic_retry')
                await asyncio.sleep(0.4)
    finally:
        if owned: await client.aclose()
