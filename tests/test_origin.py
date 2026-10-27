"""RED-first contract: authenticated browsers must still match the configured origin."""
import os
import unittest
from unittest.mock import patch, AsyncMock
import httpx
from cloud.api import create_app

TOKEN = 'offline_only_A9xB7yC5zD3wE1vF8uG6tH4sJ2rK0qM'


class OriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_origin_authenticated_post_is_rejected(self):
        with patch.dict(os.environ,{'AGENT_API_TOKEN':TOKEN,'APP_VARIANT':'frontend',
                                    'PUBLIC_ORIGIN':'https://owner.example.invalid'}):
            app = create_app(graph=AsyncMock())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://owner.example.invalid') as http:
                response = await http.post('/chat',headers={'Authorization':'Bearer '+TOKEN,
                    'Origin':'https://untrusted.example.invalid'},json={'message':'test','request_id':'synthetic'})
                self.assertEqual(response.status_code,403)
