import asyncio
import logging
from typing import Awaitable, Callable, Optional, Tuple, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int,
    base_delay: float,
    retryable: Tuple[Type[Exception], ...],
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> T:
    """Calls `fn()`, retrying with exponential backoff (base_delay * 2**n) on
    any of `retryable` exception types. Anything else propagates immediately
    -- e.g. a broker rejecting an order outright isn't something a retry
    fixes. After `retries` attempts have failed, the last exception raises.
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except retryable as exc:
            attempt += 1
            if attempt > retries:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            if on_retry:
                on_retry(attempt, exc)
            logger.warning(
                "retry %d/%d after %s: %s (sleeping %.1fs)",
                attempt, retries, exc.__class__.__name__, exc, delay,
            )
            await asyncio.sleep(delay)
