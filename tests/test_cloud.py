import asyncio
import os
import unittest
from unittest.mock import patch
import httpx
from cloud.api import create_app
from cloud.security import validate_token

TOKEN = 'offline_only_' + 'A9xB7yC5zD3wE1vF8uG6tH4sJ2rK0qM'


class Graph:
    def __init__(self): self.calls = []
    async def ainvoke(self, state):
        self.calls.append(state)
        await asyncio.sleep(0.02)
        return {'route':'generica','reason':'greeting','response':'Hola',
                'provider':'google','backend':'developer','model_used':'test-primary',
                'fallback_used':False,'response_model_used':'test-primary',
                'response_fallback_used':False}


class CloudTests(unittest.IsolatedAsyncioTestCase):
    def test_weak_token_rejected(self):
        for value in ['', 'password', 'a'*48, 'replace-with-a-generated-secret']:
            with self.assertRaises(ValueError): validate_token(value)
        self.assertEqual(validate_token(TOKEN), TOKEN)

    async def test_authenticated_async_turn_and_idempotency(self):
        graph = Graph()
        with patch.dict(os.environ, {'AGENT_API_TOKEN': TOKEN, 'APP_VARIANT':'base'}):
            app = create_app(graph=graph)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            payload = {'message':'Hola', 'request_id':'request1', 'conversation_id':'owner1'}
            self.assertEqual((await client.post('/chat', json=payload)).status_code, 401)
            headers = {'Authorization':'Bearer '+TOKEN}
            response = await client.post('/chat', json=payload, headers=headers)
            self.assertEqual(response.status_code, 202)
            location = response.json()['status_url']
            self.assertEqual((await client.get(location)).status_code, 401)
            repeat = await client.post('/chat', json=payload, headers=headers)
            self.assertEqual(repeat.json()['status_url'], location)
            changed = await client.post('/chat', json={**payload,'message':'Other'}, headers=headers)
            self.assertEqual(changed.status_code, 409)
            await asyncio.sleep(0.08)
            result = await client.get(location, headers=headers)
            self.assertEqual(result.json()['result']['response'], 'Hola')
            self.assertEqual(len(graph.calls), 1)
            self.assertFalse((await client.get('/health')).json().get('token'))
            self.assertEqual((await client.delete('/conversations/owner1')).status_code, 401)

    async def test_safe_errors_do_not_echo_content(self):
        with patch.dict(os.environ, {'AGENT_API_TOKEN': TOKEN, 'APP_VARIANT':'base'}):
            app = create_app(graph=Graph())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post('/chat', json={'message':'SECRET','unexpected':'SECRET'},
                                      headers={'Authorization':'Bearer '+TOKEN})
            self.assertEqual(result.status_code, 422)
            self.assertNotIn('SECRET', result.text)

    def test_public_configuration_requires_origin(self):
        from cloud.security import public_origin
        with patch.dict(os.environ, {'PUBLIC_ORIGIN':'https://example.herokuapp.com'}):
            self.assertEqual(public_origin(), 'https://example.herokuapp.com')
        with patch.dict(os.environ, {'PUBLIC_ORIGIN':'https://example.com/path'}):
            with self.assertRaises(ValueError): public_origin()


if __name__ == '__main__': unittest.main()
