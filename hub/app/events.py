import asyncio
import time
from typing import Any, Dict, List


class EventBus:
    """Simple asyncio pub-sub: each subscriber gets its own queue and every
    emitted event is fanned out to all of them. Used to push live
    OPEN/MODIFY/CLOSE (and slave-execution) events to WebSocket clients.
    """

    def __init__(self) -> None:
        self._subscribers: List["asyncio.Queue[Dict[str, Any]]"] = []

    def subscribe(self) -> "asyncio.Queue[Dict[str, Any]]":
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def emit(self, event: Dict[str, Any]) -> None:
        event.setdefault("emitted_at", time.time())
        for queue in list(self._subscribers):
            await queue.put(event)
