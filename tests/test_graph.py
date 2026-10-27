import unittest
from unittest.mock import AsyncMock
from app.graph import RoutingResult, GeneratedReply, build_graph
from app.provider import FallbackRouter, FallbackResponder
import httpx


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_graph_runs_router_then_responder(self):
        router, responder = AsyncMock(), AsyncMock()
        router.ainvoke.return_value = RoutingResult(route='generica',reason='greeting',provider='google',
            backend='developer',model_used='primary',fallback_used=False)
        responder.ainvoke.return_value = GeneratedReply(response='Hola',response_model_used='primary',response_fallback_used=False)
        result = await build_graph(router,responder).ainvoke({'message':'Hola','history':[]})
        self.assertEqual(result['response'],'Hola')
        router.ainvoke.assert_awaited_once()
        responder.ainvoke.assert_awaited_once()

    async def test_router_and_responder_keep_transient_fallback(self):
        from app.graph import RouteDecision
        from langchain_core.messages import AIMessage
        for cls, reply in [(FallbackRouter,RouteDecision(route='generica',reason='fallback')),
                           (FallbackResponder,AIMessage(content='Fallback answer'))]:
            primary, secondary = AsyncMock(), AsyncMock()
            primary.ainvoke.side_effect = httpx.ReadTimeout('offline synthetic')
            secondary.ainvoke.return_value = reply
            result = await cls(primary,secondary,'primary','fallback','developer').ainvoke([])
            self.assertTrue(result.fallback_used if cls is FallbackRouter else result.response_fallback_used)
            secondary.ainvoke.assert_awaited_once()
