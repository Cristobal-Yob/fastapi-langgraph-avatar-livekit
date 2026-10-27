import asyncio
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from avatar.web import create_app

TOKEN = 'offline_only_A9xB7yC5zD3wE1vF8uG6tH4sJ2rK0qM'


class FakeTrial:
    config=SimpleNamespace(owner_key=TOKEN,enabled=True,dispatch_isolation_confirmed=True)
    spent=False
    started=False
    async def start(self): self.started=True;return self.status()
    async def stop(self): return self.status()
    def status(self): return {'status':'prepared'}


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_avatar_start_acknowledges_before_sdk_preparation(self):
        origin='https://synthetic.example.invalid';trial=FakeTrial()
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':origin}):app=create_app(trial)
        gate=asyncio.Event()
        async def prepare(value): await gate.wait(); await value.start()
        with patch('avatar.web.prepare_start',prepare,create=True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=origin) as http:
                csrf=(await http.get('/api/bootstrap')).json()['csrf']
                headers={'Origin':origin,'X-CSRF-Token':csrf}
                self.assertEqual((await http.post('/api/unlock',json={'key':TOKEN},headers=headers)).status_code,204)
                result=await asyncio.wait_for(http.post('/api/start',json={'confirmed':True},headers=headers),2)
                self.assertEqual(result.status_code,202)
                self.assertFalse(trial.started)
                gate.set()
                await asyncio.sleep(.02)
                self.assertTrue(trial.started)

    async def test_stop_during_sdk_preparation_revokes_before_paid_start(self):
        origin='https://synthetic.example.invalid';trial=FakeTrial()
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':origin}):app=create_app(trial)
        gate=asyncio.Event()
        async def prepare(value): await gate.wait(); await value.start()
        with patch('avatar.web.prepare_start',prepare):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=origin) as http:
                csrf=(await http.get('/api/bootstrap')).json()['csrf']
                headers={'Origin':origin,'X-CSRF-Token':csrf}
                await http.post('/api/unlock',json={'key':TOKEN},headers=headers)
                await http.post('/api/start',json={'confirmed':True},headers=headers)
                self.assertEqual((await http.post('/api/stop',json={},headers=headers)).status_code,202)
                gate.set()
                await asyncio.sleep(.02)
                self.assertFalse(trial.started)
