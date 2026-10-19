"""Local reference: BFF_UPSTREAM=http://127.0.0.1:8016 BFF_DEMO_COOKIE=... uvicorn examples.sse_v3.bff:app --port 8090.
Replace fixed demo identity with your authenticated session service in production.
"""
import hmac
import json
import os
import re
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response, StreamingResponse

app = FastAPI()
UPSTREAM = os.getenv('BFF_UPSTREAM', 'http://127.0.0.1:8016')
PATH = re.compile(r'sessions/[^/]+/(harness(?:/runs(?:/[^/]+/(?:cancel|record|events))?|/events|/capabilities)?|input-requests/[^/]+/answers)$')

async def raw_stream(response, client):
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()
        await client.aclose()

@app.api_route('/api/v3/{path:path}', methods=['GET', 'POST'])
async def proxy(path: str, request: Request):
    secret = os.getenv('BFF_DEMO_COOKIE', '')
    if not secret or not hmac.compare_digest(request.cookies.get('demo_session', ''), secret):
        raise HTTPException(401, 'AUTHENTICATION_REQUIRED')
    if not PATH.fullmatch(path):
        raise HTTPException(404)
    if request.method == 'POST' and request.headers.get('origin') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'SAME_ORIGIN_REQUIRED')
    user_id, profile_id = os.getenv('BFF_DEMO_USER', 'demo'), os.getenv('BFF_DEMO_PROFILE') or None
    query = dict(request.query_params)
    query.pop('user_id', None)
    query.pop('profile_id', None)
    if request.method == 'GET':
        query.update(user_id=user_id)
        if profile_id: query['profile_id'] = profile_id
    body = None
    if request.method == 'POST':
        body = await request.json()
        target = body.setdefault('context_data', {}) if path.endswith('/harness/runs') else body
        target.update(user_id=user_id, profile_id=profile_id)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), follow_redirects=False)
    headers = {'accept-encoding': 'identity'}
    if request.headers.get('accept'): headers['accept'] = request.headers['accept']
    if request.headers.get('last-event-id'): headers['Last-Event-ID'] = request.headers['last-event-id']
    try:
        upstream = await client.send(client.build_request(request.method, UPSTREAM + '/api/v3/' + path,
            params=query, json=body, headers=headers), stream=True)
    except Exception:
        await client.aclose()
        raise HTTPException(502, 'UPSTREAM_UNAVAILABLE')
    forwarded = {k: v for k, v in upstream.headers.items() if k.lower() in {'content-type','cache-control','x-accel-buffering','x-sse-protocol','retry-after'}}
    if upstream.status_code == 200 and 'text/event-stream' in upstream.headers.get('content-type', ''):
        return StreamingResponse(raw_stream(upstream, client), status_code=200, headers=forwarded)
    try:
        content = await upstream.aread()
        return Response(content, status_code=upstream.status_code, headers=forwarded)
    finally:
        await upstream.aclose()
        await client.aclose()
