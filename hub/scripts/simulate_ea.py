"""Stands in for the MT4 bridge EA during development.

Connects a PUB socket to the hub's SUB port and a PUSH socket to the hub's
PULL port, then publishes a full OPEN -> MODIFY -> CLOSE sequence for one
ticket plus a heartbeat -- mirroring what bridge_ea/TradeCopierBridge.mq4
does on the MT4 side (Phase 2: real order-state diffing, Phase 3: includes
the master's own iATR() value so autonomous slaves can back-calculate SL/TP
as an ATR multiple).
"""

import argparse
import json
import time

import zmq


def publish(pub: zmq.Socket, signal: dict) -> None:
    pub.send_string(json.dumps(signal))
    print(f"published {signal['action']}:", signal)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--pub-port", type=int, default=5555)
    parser.add_argument("--push-port", type=int, default=5557)
    parser.add_argument("--step-delay", type=float, default=0.5, help="seconds between OPEN/MODIFY/CLOSE")
    args = parser.parse_args()

    ctx = zmq.Context.instance()

    pub = ctx.socket(zmq.PUB)
    pub.connect(f"tcp://{args.host}:{args.pub_port}")

    push = ctx.socket(zmq.PUSH)
    push.connect(f"tcp://{args.host}:{args.push_port}")

    # give the SUB socket time to complete its connection before we publish
    # (classic ZMQ "slow joiner" issue)
    time.sleep(1.5)

    ticket = 123456
    base = {
        "ticket": ticket,
        "symbol": "XAUUSD",
        "direction": "BUY",
        "lots": 0.10,
        "price": 2385.50,
        "atr": 10.00,  # master XAUUSD's own iATR() value at signal time
        "account": "TEST-MASTER-1",
    }

    publish(pub, {**base, "action": "OPEN", "sl": 2375.00, "tp": 2405.00, "ts": time.time()})
    time.sleep(args.step_delay)

    publish(pub, {**base, "action": "MODIFY", "sl": 2380.00, "tp": 2405.00, "ts": time.time()})
    time.sleep(args.step_delay)

    publish(pub, {**base, "action": "CLOSE", "sl": 2380.00, "tp": 2405.00, "ts": time.time()})

    heartbeat = {"account": "TEST-MASTER-1", "ts": time.time()}
    push.send_string(json.dumps(heartbeat))
    print("pushed heartbeat:", heartbeat)

    time.sleep(0.2)
    pub.close()
    push.close()


if __name__ == "__main__":
    main()
