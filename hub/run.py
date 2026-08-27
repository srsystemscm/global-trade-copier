"""Entrypoint for running the hub.

pyzmq's asyncio integration needs add_reader()/add_writer() support, which
Windows' ProactorEventLoop does not provide -- recv() on a ZMQ socket just
hangs forever with no error. Setting the event loop *policy* is not enough:
uvicorn's own loop factory (uvicorn/loops/asyncio.py) hardcodes
asyncio.ProactorEventLoop on win32 whenever it's not running in subprocess
(multi-worker/reload) mode, overriding whatever policy is active. So instead
of `uvicorn.run(...)`, we build the Selector event loop ourselves and drive
uvicorn's Server.serve() coroutine on it directly.
"""

import asyncio
import sys

import uvicorn

from app.config import settings


def main() -> None:
    config = uvicorn.Config("app.main:app", host="0.0.0.0", port=settings.http_port)
    server = uvicorn.Server(config)

    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
