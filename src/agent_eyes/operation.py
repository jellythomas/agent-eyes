"""Request-scoped operation modes, deadlines, and fail-closed errors."""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar


_T = TypeVar("_T")


class _ProviderTimeout(RuntimeError):
    """Internal wrapper that distinguishes provider TimeoutError from our deadline."""


class OperationMode(Enum):
    """Execution planes are explicit choices, not ordered fallback tiers."""

    FOREGROUND = "foreground"
    SHADOW = "shadow"


class OperationErrorCode(Enum):
    """Stable internal failure categories suitable for compact MCP errors."""

    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    MODE_MISMATCH = "MODE_MISMATCH"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    FOCUS_MISMATCH = "FOCUS_MISMATCH"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    PROVIDER_BUSY = "PROVIDER_BUSY"
    RESULT_TRUNCATED = "RESULT_TRUNCATED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"


class OperationError(RuntimeError):
    """A typed, user-safe automation failure."""

    def __init__(
        self,
        code: OperationErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True, slots=True)
class OperationBudget:
    """One absolute monotonic deadline shared by every layer of a request."""

    deadline: float
    _clock: Callable[[], float]

    @classmethod
    def start(
        cls,
        timeout: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> OperationBudget:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite non-negative number")
        normalized = float(timeout)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError("timeout must be a finite non-negative number")
        return cls(deadline=clock() + normalized, _clock=clock)

    def remaining(self) -> float:
        return max(0.0, self.deadline - self._clock())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def checkpoint(self, operation: str = "operation") -> None:
        if self.expired:
            raise OperationError(
                OperationErrorCode.DEADLINE_EXCEEDED,
                f"{operation} exceeded its deadline",
            )

    async def wait_for(
        self,
        awaitable: Awaitable[_T],
        *,
        operation: str = "operation",
    ) -> _T:
        async def preserve_provider_timeout() -> _T:
            try:
                return await awaitable
            except TimeoutError as exc:
                raise _ProviderTimeout from exc

        try:
            return await asyncio.wait_for(
                preserve_provider_timeout(),
                timeout=self.remaining(),
            )
        except _ProviderTimeout as exc:
            provider_error = exc.__cause__
            if isinstance(provider_error, TimeoutError):
                raise provider_error
            raise
        except asyncio.TimeoutError as exc:
            raise OperationError(
                OperationErrorCode.DEADLINE_EXCEEDED,
                f"{operation} exceeded its deadline",
            ) from exc
