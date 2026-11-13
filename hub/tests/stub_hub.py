"""A local stand-in for the Operations hub's ingest route.

Real HTTP over a real socket on a loopback port, because the properties
worth testing are the ones a mocked transport cannot have: a connection
that is refused, a server that answers too slowly, a 200 whose body is
rubbish. Never the live hub -- there is no copier route and no key yet, and
a test that needed one would be a test nobody could run.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class StubHub:
    """Records the reports it is POSTed and acks what it is told to.

    `ack` chooses the policy:
      "all"   -- ack every event in the report (a healthy hub)
      "none"  -- ack nothing (a hub that stores and never confirms)
      a list  -- ack exactly these event_ids, whatever was sent
      a callable(report) -> list of ids
    """

    def __init__(
        self,
        ack: Any = "all",
        status: int = 200,
        delay: float = 0.0,
        body: Optional[str] = None,
    ) -> None:
        self.ack = ack
        self.status = status
        self.delay = delay
        self.body = body
        self.reports: List[Dict[str, Any]] = []
        self.auth: List[Optional[str]] = []
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------- lifecycle

    def __enter__(self) -> "StubHub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802  (http.server's own naming)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if stub.delay:
                    time.sleep(stub.delay)
                try:
                    report = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    report = {"_unparseable": raw[:200].decode("latin-1")}
                with stub._lock:
                    stub.reports.append(report)
                    stub.auth.append(self.headers.get("Authorization"))

                payload = (
                    stub.body if stub.body is not None
                    else json.dumps({
                        "ok": True,
                        "schema_version": "1.0",
                        "server_time_utc": "2026-11-12T20:05:06Z",
                        "acked_event_ids": stub._acks(report),
                    })
                )
                encoded = payload.encode("utf-8")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass  # the suite's output is not a web server log

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------- helpers

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def _acks(self, report: Dict[str, Any]) -> List[str]:
        events = report.get("events") or []
        sent = [e.get("event_id") for e in events if isinstance(e, dict)]
        if callable(self.ack):
            return list(self.ack(report))
        if self.ack == "all":
            return [i for i in sent if i]
        if self.ack == "none":
            return []
        return list(self.ack)

    def event_ids(self) -> List[str]:
        """Every event id this hub was sent, in order, duplicates included."""
        out = []
        with self._lock:
            for report in self.reports:
                for event in report.get("events") or []:
                    if isinstance(event, dict) and event.get("event_id"):
                        out.append(event["event_id"])
        return out

    def events_of_type(self, spec_type: str) -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            for report in self.reports:
                for event in report.get("events") or []:
                    if isinstance(event, dict) and event.get("type") == spec_type:
                        out.append(event)
        return out


def closed_port() -> int:
    """A loopback port with nothing listening, for a refused connection."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
