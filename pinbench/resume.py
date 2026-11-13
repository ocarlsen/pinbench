"""Crash safety for a long sync run: rate-limit retry, signal handling, exit codes.

Refuses to do: keep burning through a suite after the provider has said "quota exhausted"
(retries are bounded, then the run stops and is resumable), or lose a paid output to a signal
(SIGTERM, SIGHUP and SIGINT persist run.json before the process exits).

Everything tunable about the 429 policy lives in this file. Tests inject `sleep_fn`.
"""

from __future__ import annotations

import signal
import threading
import time

from . import cost as cost_mod

# Attempts per call (the first try included) and the backoff schedule: the delay before retry
# k (k = 1, 2, ...) is RATE_LIMIT_BACKOFF_BASE_S * 2 ** (k - 1), capped at RATE_LIMIT_BACKOFF_MAX_S.
# 15, 30, 60, 120 seconds by default: about four minutes of waiting before a run gives up.
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_BACKOFF_BASE_S = 15.0
RATE_LIMIT_BACKOFF_MAX_S = 300.0

# Exit codes owned by this module (documented in docs/commands.md).
EXIT_QUOTA_STOP = 6
STOP_QUOTA = "quota_or_rate_limit"

# Substrings of a provider error that mean quota or rate limit when no HTTP status was kept.
# Gemini RESOURCE_EXHAUSTED and OpenAI insufficient_quota both arrive with HTTP 429 as well.
_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "insufficient_quota", "rate_limit_error")


class RateLimitExhausted(Exception):
    """A call kept returning 429 / quota exhausted after every retry. `record` is the partial
    judge record (sync judge stage) so the caller can keep what was paid for."""

    def __init__(self, message, record=None):
        super().__init__(message)
        self.record = record


class RunInterrupted(KeyboardInterrupt):
    """Raised from a signal handler when a call must be abandoned now (a second signal, or a
    signal during a batch wait). A KeyboardInterrupt so existing handlers treat it as one."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def is_rate_limited(reply) -> bool:
    if reply is None or reply.error is None:
        return False
    if reply.status_code == 429:
        return True
    return any(marker in reply.error for marker in _QUOTA_MARKERS)


def backoff_s(attempt) -> float:
    return min(RATE_LIMIT_BACKOFF_BASE_S * 2 ** (attempt - 1), RATE_LIMIT_BACKOFF_MAX_S)


def call_with_rate_limit_retry(call, price, estimate_usd, sleep_fn=None):
    """Run `call()` (returns a providers.Reply), retrying only while the reply is a 429 or a
    quota error. Returns (final_reply, prior_cost_usd): `prior_cost_usd` is what the failed
    attempts cost under the ordinary cost rule (`cost.actual_call_cost`: $0 for a bare 4xx,
    billed tokens when the reply carries them, the estimate when the outcome is unknown), so
    the caller can charge it and never loses billed tokens to a retry. The final reply is
    returned as is, rate-limited or not; the caller decides to stop."""
    sleep_fn = sleep_fn or time.sleep
    prior = 0.0
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        reply = call()
        if not is_rate_limited(reply) or attempt == RATE_LIMIT_ATTEMPTS:
            return reply, prior
        cost = cost_mod.actual_call_cost(reply, price, estimate_usd)
        prior += estimate_usd if cost is None else cost
        sleep_fn(backoff_s(attempt))


class SignalGuard:
    """Context manager for SIGTERM, SIGHUP and SIGINT.

    graceful=True (the arm loop): the first signal only sets `requested`; the call in flight
    is allowed to return (its tokens are paid) and the loop stops before the next call. A
    second signal abandons the in-flight call by raising RunInterrupted.
    graceful=False (waiting on a judge batch): the first signal raises RunInterrupted; the
    batch id is already on disk, so nothing is lost.

    Handlers are installed only on the main thread and restored on exit."""

    SIGNALS = tuple(s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None), getattr(signal, "SIGINT", None)) if s is not None)

    def __init__(self, graceful=True, install=True):
        self.graceful = graceful
        self.requested = None  # the first signum seen, or None
        self._install = install
        self._previous = {}

    def handle(self, signum, _frame=None):
        if self.graceful and self.requested is None:
            self.requested = signum
            return
        if self.requested is None:
            self.requested = signum
        raise RunInterrupted(signum)

    def __enter__(self):
        if self._install and threading.current_thread() is threading.main_thread():
            for sig in self.SIGNALS:
                self._previous[sig] = signal.signal(sig, self.handle)
        return self

    def __exit__(self, *exc):
        for sig, previous in self._previous.items():
            signal.signal(sig, previous)
        self._previous = {}
        return False

    @property
    def name(self):
        return signal.Signals(self.requested).name if self.requested is not None else None


def exit_code_for_signal(name) -> int:
    return 128 + int(getattr(signal.Signals, name))
