import logging

from fastapi import APIRouter, HTTPException, Query, Request

from app.adapters.base import AdapterError
from app.adapters.schwab import SchwabAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schwab", tags=["schwab"])


def _schwab_adapters(request: Request) -> dict:
    """slave_id -> SchwabAdapter, for whichever Schwab slaves are configured."""
    return {
        engine.slave_id: engine.adapter
        for engine in request.app.state.engines
        if isinstance(engine.adapter, SchwabAdapter)
    }


@router.get("/authorize")
async def authorize(request: Request, slave_id: str = Query(...)):
    """Step 1 of the one-time OAuth setup: open the URL this returns in a
    browser, log in to Schwab, and approve. Schwab then redirects to
    `schwab_redirect_uri` with a `code` query param -- pass that to
    /schwab/callback.
    """
    adapters = _schwab_adapters(request)
    adapter = adapters.get(slave_id)
    if adapter is None:
        raise HTTPException(404, f"no Schwab slave with id={slave_id!r}")
    return {"authorize_url": adapter.auth.build_authorize_url()}


@router.get("/callback")
async def callback(request: Request, slave_id: str = Query(...), code: str = Query(...)):
    adapters = _schwab_adapters(request)
    adapter = adapters.get(slave_id)
    if adapter is None:
        raise HTTPException(404, f"no Schwab slave with id={slave_id!r}")
    try:
        await adapter.auth.exchange_code(code)
    except AdapterError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok", "message": "Schwab authorization complete; refresh token stored."}
