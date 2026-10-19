import asyncio
import importlib.util
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
spec = importlib.util.spec_from_file_location('v3_bff', Path(__file__).parents[1] / 'examples/sse_v3/bff.py')
bff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bff)


def test_bff_forwards_fragmented_unicode_and_trusted_identity(monkeypatch):
    payload = 'id: 2\nevent: message.delta\ndata: {"text":"中文😀"}\n\n'.encode()
    closed = []
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            for byte in payload: yield bytes([byte])
        async def aclose(self): closed.append(True)
    async def upstream(request):
        assert request.url.params['user_id'] == 'trusted'
        assert request.headers['last-event-id'] == '1'
        return httpx.Response(200, headers={'Content-Type':'text/event-stream','X-SSE-Protocol':'hailiang.sse.v3'}, stream=Bytes())
    original = httpx.AsyncClient
    monkeypatch.setattr(bff.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(upstream)))
    monkeypatch.setenv('BFF_DEMO_COOKIE', 'secret')
    monkeypatch.setenv('BFF_DEMO_USER', 'trusted')
    with TestClient(bff.app) as client:
        client.cookies.set('demo_session','secret')
        response = client.get('/api/v3/sessions/s/harness/runs/run-1/events?user_id=forged',headers={'Last-Event-ID':'1'})
        assert response.content == payload
        assert response.headers['x-sse-protocol'] == 'hailiang.sse.v3'
    assert closed


def test_bff_rejects_missing_session_and_cross_origin(monkeypatch):
    monkeypatch.setenv('BFF_DEMO_COOKIE','secret')
    with TestClient(bff.app) as client:
        assert client.get('/api/v3/sessions/s/harness').status_code == 401
        client.cookies.set('demo_session','secret')
        assert client.post('/api/v3/sessions/s/harness/runs',json={}).status_code == 403
