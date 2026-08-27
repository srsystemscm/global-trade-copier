import asyncio

import pytest

from app.retry import retry_async


class Flaky(Exception):
    pass


class NotRetryable(Exception):
    pass


def test_succeeds_without_retry():
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    result = asyncio.run(retry_async(fn, retries=3, base_delay=0.001, retryable=(Flaky,)))
    assert result == "ok"
    assert len(calls) == 1


def test_retries_then_succeeds():
    attempts = {"n": 0}

    async def fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Flaky("transient")
        return "recovered"

    result = asyncio.run(retry_async(fn, retries=5, base_delay=0.001, retryable=(Flaky,)))
    assert result == "recovered"
    assert attempts["n"] == 3


def test_gives_up_after_max_retries():
    attempts = {"n": 0}

    async def fn():
        attempts["n"] += 1
        raise Flaky("always fails")

    with pytest.raises(Flaky):
        asyncio.run(retry_async(fn, retries=2, base_delay=0.001, retryable=(Flaky,)))
    assert attempts["n"] == 3  # initial attempt + 2 retries


def test_non_retryable_exception_propagates_immediately():
    attempts = {"n": 0}

    async def fn():
        attempts["n"] += 1
        raise NotRetryable("no point retrying this")

    with pytest.raises(NotRetryable):
        asyncio.run(retry_async(fn, retries=5, base_delay=0.001, retryable=(Flaky,)))
    assert attempts["n"] == 1


def test_on_retry_callback_invoked_per_attempt():
    seen = []

    async def fn():
        if len(seen) < 2:
            raise Flaky("retry me")
        return "done"

    result = asyncio.run(
        retry_async(
            fn, retries=3, base_delay=0.001, retryable=(Flaky,),
            on_retry=lambda attempt, exc: seen.append(attempt),
        )
    )
    assert result == "done"
    assert seen == [1, 2]
