"""Stands in for a slave-side bridge EA during development.

Binds a REP socket and answers OPEN/MODIFY/CLOSE/PING/POSITIONS from the
hub's MT4Adapter, mirroring what bridge_ea/SlaveBridge.mq4 (1.04) and
bridge_ea/SlaveBridge.mq5 (1.01) do on a real terminal. Tracks a small
in-memory book so it can hand back tickets, carry SL/TP, and reproduce the
two behaviours the hub actually depends on:

- POSITIONS reports every open position, including each leg's SL/TP, which
  is what lets a MODIFY be verified without reading the terminal by hand.
- MODIFY and CLOSE on an already-closed ticket answer
  {"status":"ok","already_closed":true} rather than an error. Both EAs have
  done this since mq4 1.02, and the difference matters: an error there
  leaves the Trade Registry row stuck at OPEN forever.

Keeping this in step with the EAs is the whole point of the script, and it
has drifted before: it accepted whitespace in the JSON that the real MQL4
parser silently did not, which hid a bug until it reached real terminals.
"""

import argparse
import itertools
import json

import zmq

next_ticket = itertools.count(900001)
open_orders: dict[int, dict] = {}
closed_tickets: set[int] = set()


def _already_closed(ticket: int) -> dict:
    return {"status": "ok", "already_closed": True, "slave_ticket": ticket}


def handle(cmd: dict) -> dict:
    action = cmd.get("cmd")

    if action == "PING":
        return {"status": "ok"}

    if action == "OPEN":
        ticket = next(next_ticket)
        open_orders[ticket] = {
            "symbol": cmd["symbol"],
            "direction": cmd["direction"],
            "lots": cmd["lots"],
            "price": cmd.get("price") or 0.0,
            "sl": cmd.get("sl"),
            "tp": cmd.get("tp"),
        }
        print(
            f"OPEN  master_ticket={cmd['master_ticket']} -> slave_ticket={ticket} "
            f"{cmd['symbol']} {cmd['direction']} {cmd['lots']} sl={cmd['sl']} tp={cmd['tp']}"
        )
        return {"status": "ok", "slave_ticket": ticket, "price": cmd.get("price", 0.0)}

    if action == "MODIFY":
        ticket = cmd["ticket"]
        if ticket in closed_tickets:
            print(f"MODIFY slave_ticket={ticket} ignored (already closed)")
            return _already_closed(ticket)
        if ticket not in open_orders:
            return {"status": "error", "message": f"ticket {ticket} not found"}
        open_orders[ticket]["sl"] = cmd.get("sl")
        open_orders[ticket]["tp"] = cmd.get("tp")
        print(f"MODIFY slave_ticket={ticket} sl={cmd.get('sl')} tp={cmd.get('tp')}")
        return {"status": "ok"}

    if action == "CLOSE":
        ticket = cmd["ticket"]
        if ticket in closed_tickets:
            print(f"CLOSE slave_ticket={ticket} ignored (already closed)")
            return _already_closed(ticket)
        if ticket not in open_orders:
            return {"status": "error", "message": f"ticket {ticket} not found"}
        del open_orders[ticket]
        closed_tickets.add(ticket)
        print(f"CLOSE slave_ticket={ticket}")
        return {"status": "ok"}

    if action == "POSITIONS":
        positions = []
        for ticket, o in open_orders.items():
            # Signed quantity, and 0-means-unset levels formatted the way the
            # EAs format them: they emit numbers, never JSON null.
            positions.append({
                "ticket": ticket,
                "symbol": o["symbol"],
                "quantity": o["lots"] if o["direction"] == "BUY" else -o["lots"],
                "average_price": o["price"],
                "sl": o["sl"] or 0.0,
                "tp": o["tp"] or 0.0,
                "unrealized_pnl": 0.0,
            })
        return {"status": "ok", "positions": positions}

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
