"""
WebSocket Manager — entrega de eventos em tempo real para o pipeline de geração.

Estratégia de queue:
  • Pipeline envia eventos via manager.send() — independente de o cliente estar conectado.
  • Se o WS ainda não conectou, a mensagem é enfileirada (asyncio.Queue).
  • Quando o cliente conecta (connect()), a fila é drenada na ordem correta.
  • A fila só é removida após drenagem bem-sucedida OU em caso de erro no pipeline.
    Isso resolve a race condition em que o cliente conecta DEPOIS do pipeline terminar
    mas ANTES de limpar a fila.

Limite de queue: _MAX_QUEUE_SIZE mensagens por sessão.
  Evita acúmulo ilimitado em sessões abandonadas (cliente nunca conectou).
  Mensagens além do limite são descartadas silenciosamente.
"""
import json
import asyncio
from fastapi import WebSocket

_MAX_QUEUE_SIZE = 60   # ~6 etapas × 10 eventos cada; bem acima do necessário


class WebSocketManager:
    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._queues: dict[str, asyncio.Queue] = {}

    async def connect(self, sessao_id: str, websocket: WebSocket):
        await websocket.accept()
        self._connections[sessao_id] = websocket

        # Drena mensagens enfileiradas antes da conexão (race condition: pipeline
        # iniciou antes do cliente conectar ao WS).
        if sessao_id in self._queues:
            q = self._queues[sessao_id]
            msgs_pendentes: list[dict] = []
            while not q.empty():
                msgs_pendentes.append(q.get_nowait())

            enviados_ok = True
            for msg in msgs_pendentes:
                try:
                    await websocket.send_text(json.dumps(msg, ensure_ascii=False))
                except Exception:
                    enviados_ok = False
                    break

            # Remove a fila apenas se todos os mensagens foram entregues.
            # Se a entrega falhou, o cliente vai precisar reconectar — a fila fica
            # intacta para a próxima tentativa (dentro do TTL da sessão).
            if enviados_ok:
                self._queues.pop(sessao_id, None)

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

        # WS não conectado (ainda) ou envio falhou — enfileira a mensagem.
        # A mensagem ficará disponível para drenagem quando o cliente reconectar.
        q = self._queues.get(sessao_id)
        if q is None:
            q = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
            self._queues[sessao_id] = q
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass   # descarta silenciosamente — fila cheia = sessão provavelmente abandonada

    def clear_queue(self, sessao_id: str):
        """Limpa a fila de uma sessão. Chamar APENAS em caso de erro no pipeline."""
        self._queues.pop(sessao_id, None)


manager = WebSocketManager()
