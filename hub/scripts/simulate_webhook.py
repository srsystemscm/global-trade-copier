"""A fake webhook receiver for development.

Logs every POST body it receives to stdout, so notification delivery
(trade close, disconnect/reconnect, drawdown) can be verified end-to-end.
Point Settings -> Notifications -> Webhook URL at this (e.g.
http://127.0.0.1:8901/webhook) or set it directly:

    curl -X PATCH http://127.0.0.1:8000/config \
        -H "Content-Type: application/json" \
        -d '{"notify_webhook_url": "http://127.0.0.1:8901/webhook"}'
"""

import argparse
import json

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI(title="Fake Webhook Receiver")

received: list = []


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    received.append(body)
    print(f"WEBHOOK #{len(received)}: {json.dumps(body)}", flush=True)
    return {"status": "ok"}


@app.get("/received")
async def get_received():
    return {"received": received}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8901)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
