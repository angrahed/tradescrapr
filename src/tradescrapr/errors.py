"""Exception taxonomy. The worker's retry/dead-letter policy keys off these types."""


class TradescraprError(Exception):
    """Base class for all tradescrapr errors."""


class ConfigError(TradescraprError):
    """Invalid or missing configuration."""


class TransientError(TradescraprError):
    """Retryable failure: network trouble, 5xx, rate-limit rejection after transport
    retries, or a 200 response carrying an error envelope."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after  # server-suggested wait, seconds


class PermanentError(TradescraprError):
    """Non-retryable failure: bad request, bad credentials, unknown resource.
    The task is dead-lettered immediately."""


class QuotaExhausted(TradescraprError):
    """Daily call quota is spent (locally budgeted or server-reported). Not a task
    failure: the task is released and retried after the quota resets."""


class ShutdownRequested(TradescraprError):
    """A shutdown signal arrived mid-task (e.g. during a long bulk download).
    The task is released back to pending without consuming an attempt."""


class CannotSplit(TradescraprError):
    """A truncated response cannot be split any further (single flow, single
    commodity, single partner). The task is dead-lettered loudly."""
