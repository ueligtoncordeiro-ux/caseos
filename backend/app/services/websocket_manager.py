import json
import asyncio
from fastapi import WebSocket


class WebSocketManager:
    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._queues: dict[str, asyncio.Queue] = {}

    async def connect(self, sessao_id: str, websocket: WebSocket):
        await websocket.accept()
        self._connections[sessao_id] = websocket
        # Drain any queued messages sent before WS connected
        if sessao_id in self._queues:
            q = self._queues[sessao_id]
            while not q.empty():
                msg = q.get_nowait()
                try:
                    await websocket.send_text(json.dumps(msg, ensure_ascii=False))
                except Exception:
                    break

    def disconnect(self, sessao_id: str):
        self._connections.pop(sessao_id, None)

    async def send(self, sessao_id: str, data: dict):
        ws = self._connections.get(sessao_id)
        if ws:
            try:
                await ws.send_text(json.dumps(data, ensure_ascii=False))
                return
            except Exception:
                self.disconnect(sessao_id)
        # WS not connected yet — queue the message
        if sessao_id not in self._queues:
            self._queues[sessao_id] = asyncio.Queue()
        await self._queues[sessao_id].put(data)

    def clear_queue(self, sessao_id: str):
        self._queues.pop(sessao_id, None)


manager = WebSocketManager()
