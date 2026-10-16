"""Inert ASGI lab app. Never imports the girlfriend runtime or loads models."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket


@asynccontextmanager
async def lifespan(app):
    yield
    while Path('hold_shutdown').exists():
        await asyncio.sleep(.05)


app = FastAPI(lifespan=lifespan)


@app.get('/health')
async def health():
    return {'ok': True, 'fixture': True}


@app.post('/slow-upload')
async def slow_upload(request: Request):
    await request.body()
    await asyncio.sleep(1)
    return {'ok': True}


@app.websocket('/ws')
async def ws(socket: WebSocket):
    await socket.accept()
    try:
        await socket.receive_text()
    finally:
        await socket.close()
