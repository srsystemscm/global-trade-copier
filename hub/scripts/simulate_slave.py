"""Stands in for a slave-side MT4 bridge EA during development.

Binds a REP socket and answers OPEN/MODIFY/CLOSE/PING commands from the
hub's MT4Adapter, mirroring what bridge_ea/SlaveBridge.mq4 does on a real
MT4 terminal. Tracks a tiny in-memory ticket book so it can hand back
sensible tickets and reject MODIFY/CLOSE on tickets it doesn't know about.
"""

import argparse
import itertools
import json

import zmq

next_ticket = itertools.count(900001)
open_orders: dict[int, dict] = {}


def handle(cmd: dict) -> dict:
    action = cmd.get("cmd")

    if action == "PING":
        return {"status": "ok"}

    if action == "OPEN":
        ticket = next(next_ticket)
        open_orders[ticket] = dict(cmd)
        print(
            f"OPEN  master_ticket={cmd['master_ticket']} -> slave_ticket={ticket} "
            f"{cmd['symbol']} {cmd['direction']} {cmd['lots']} sl={cmd['sl']} tp={cmd['tp']}"
        )
        return {"status": "ok", "slave_ticket": ticket, "price": cmd.get("price", 0.0)}

    if action == "MODIFY":
        ticket = cmd["ticket"]
        if ticket not in open_orders:
            return {"status": "error", "message": f"unknown ticket {ticket}"}
        open_orders[ticket]["sl"] = cmd.get("sl")
        open_orders[ticket]["tp"] = cmd.get("tp")
        print(f"MODIFY slave_ticket={ticket} sl={cmd.get('sl')} tp={cmd.get('tp')}")
        return {"status": "ok"}

    if action == "CLOSE":
        ticket = cmd["ticket"]
        if ticket not in open_orders:
            return {"status": "error", "message": f"unknown ticket {ticket}"}
        del open_orders[ticket]
        print(f"CLOSE slave_ticket={ticket}")
        return {"status": "ok"}

    return {"status": "error", "message": f"unknown cmd {action!r}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5560)
    args = parser.parse_args()

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REP)
    sock.bind(f"tcp://*:{args.port}")
    print(f"simulate_slave listening on tcp://*:{args.port}")

    while True:
        raw = sock.recv()
        cmd = json.loads(raw.decode("utf-8"))
        reply = handle(cmd)
        sock.send_string(json.dumps(reply))


if __name__ == "__main__":
    main()
