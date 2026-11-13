"""Copier -> Operations hub reporter, schema 1.0.

The contract is `docs/hub-reporter.md`; the Operations hub's ingest route,
storage, API and page are built against it in that project. Read the
document before changing anything here, because a field that moves on one
side only is a field nobody can rely on.

The shape, in one paragraph: the copier pushes, the hub never calls back.
A report carries a *snapshot* -- what is true now, never queued, never
acked, replaced by the next one -- and *events* -- what happened, queued to
this copier's own SQLite database and held until the hub acks them by
`event_id`, so neither a copier restart nor a hub outage loses or
double-counts one.

The rule that outranks everything else in here: reporting must never slow
or block copying. The reporter is off by default, does nothing at all
without a key, keeps every network call in its own task behind a short
timeout, and fails quietly with a logged warning. A dead or slow hub must
not cost one copy.
"""
from app.hub_reporter.config import HubReporterConfig
from app.hub_reporter.config import load as load_config
from app.hub_reporter.reporter import HubReporter

__all__ = ["HubReporter", "HubReporterConfig", "load_config"]
