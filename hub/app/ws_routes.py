import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await websocket.accept()
    events = websocket.app.state.events
    queue = events.subscribe()
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        events.unsubscribe(queue)
