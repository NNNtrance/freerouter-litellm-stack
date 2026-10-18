from __future__ import annotations

from typing import TYPE_CHECKING, Final

from freerouter.planning import direct_alias
from freerouter.state import HealthStatus, ProbeOutcome
from freerouter.transport import (
    HTTP_FORBIDDEN,
    HTTP_NOT_FOUND,
    HTTP_PAYMENT_REQUIRED,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
    as_items,
    as_text,
    dig,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from datetime import datetime, timedelta

    from freerouter.gateway import Gateway
    from freerouter.registry import Provider
    from freerouter.state import HealthState
    from freerouter.transport import Response

MAX_DETAIL: Final = 200

STATUS_OUTCOMES: Final[Mapping[int, ProbeOutcome]] = {
    HTTP_UNAUTHORIZED: ProbeOutcome.AUTH,
    HTTP_PAYMENT_REQUIRED: ProbeOutcome.EXHAUSTED,
    HTTP_FORBIDDEN: ProbeOutcome.AUTH,
    HTTP_NOT_FOUND: ProbeOutcome.MISSING,
    HTTP_TOO_MANY_REQUESTS: ProbeOutcome.THROTTLED,
}

KEYWORD_OUTCOMES: Final[tuple[tuple[ProbeOutcome, tuple[str, ...]], ...]] = (
    (
        # First: "exceeded" and "limit" in these messages would otherwise read as
        # quota or throttling, and a too-big prompt says nothing about health.
        ProbeOutcome.TOO_LARGE,
        (
            "context length",
            "context_length",
            "maximum context",
            "context window",
            "too many tokens",
            "request too large",
            "reduce the length",
            "prompt is too long",
            "input is too long",
            "tokens per minute",
        ),
    ),
    (
        ProbeOutcome.MISSING,
        (
            "model_not_found",
            "not found",
            "does not exist",
            "no such model",
            "unknown model",
            "decommissioned",
            "has been deprecated",
            "no longer available",
            "已下线",
            "不存在",
        ),
    ),
    (
        ProbeOutcome.EXHAUSTED,
        (
            "insufficient",
            "quota",
            "out of credit",
            "balance",
            "exceeded your current",
            "余额",
            "额度",
        ),
    ),
    (
        ProbeOutcome.THROTTLED,
        ("rate limit", "rate_limit", "too many requests", "请求过于频繁", "限流"),
    ),
    (
        ProbeOutcome.AUTH,
        ("invalid api key", "unauthorized", "authentication", "invalid token", "鉴权"),
    ),
)


def _keyword_outcome(text: str) -> ProbeOutcome | None:
    """Infer an outcome from the error text when the status code is not decisive."""
    lowered = text.lower()
    for outcome, needles in KEYWORD_OUTCOMES:
        if any(needle in lowered for needle in needles):
            return outcome
    return None


ERROR_PATHS: Final = ("error.message", "message", "detail", "msg")


def _detail(response: Response) -> str:
    """Pull the upstream error message out of the JSON body when there is one.

    Reading the decoded JSON rather than the raw bytes keeps non-ASCII messages
    intact even when the platform returns them as escaped unicode sequences.
    """
    payload = response.json()
    for path in ERROR_PATHS:
        message = as_text(dig(payload, path))
        if message:
            return " ".join(message.split())[:MAX_DETAIL]
    return " ".join(response.text.split())[:MAX_DETAIL]


ALLOWANCE_SPENT: Final = (
    "free allowance",
    "check-in required",
    "next rolling",
    "period's free",
    "allowance has been",
)
"""Messages that say a free quota is used up or locked. They arrive as 429 (tokenharbor)
or 402 (apinex) and must read as EXHAUSTED, not as a short throttle: the route stays
dead for hours or days, and retrying it in 30 minutes just hands users errors."""


def outcome_for(status: int, detail: str) -> ProbeOutcome:
    """Classify one upstream failure from its status code and message.

    Shared by the active probe and by passive observation of real traffic, so a
    model is judged the same way whether FreeRouter tested it or a user did.
    """
    if any(needle in detail.lower() for needle in ALLOWANCE_SPENT):
        return ProbeOutcome.EXHAUSTED
    return STATUS_OUTCOMES.get(status) or _keyword_outcome(detail) or ProbeOutcome.TRANSIENT


TOOL_REJECTION: Final = ("tool", "function call", "function_call", "functions")
HTTP_BAD_REQUEST: Final = 400


def classify(response: Response) -> tuple[ProbeOutcome, str]:
    """Turn one probe response into an outcome and a short human-readable reason."""
    detail = _detail(response)
    if response.ok:
        if as_items(dig(response.json(), "choices")):
            return ProbeOutcome.OK, "ok"
        return ProbeOutcome.TRANSIENT, f"200 但没有返回 choices: {detail}"
    outcome = outcome_for(response.status, detail)
    if (
        outcome is ProbeOutcome.TRANSIENT
        and response.status in {HTTP_BAD_REQUEST, 422}
        and any(needle in detail.lower() for needle in TOOL_REJECTION)
    ):
        outcome = ProbeOutcome.NO_TOOLS
    return outcome, f"HTTP {response.status}: {detail}"


def called_a_tool(response: Response) -> bool:
    """Report whether a successful answer contains a tool call."""
    for choice in as_items(dig(response.json(), "choices")):
        if as_items(dig(choice, "message.tool_calls")):
            return True
    return False


def probe_one(
    gateway: Gateway, provider: Provider, model_id: str
) -> tuple[ProbeOutcome, str, bool | None]:
    """Send one real tool-calling completion through the proxy and classify the result.

    Every health probe is also the capability probe: a client that cannot get a
    tool call out of a route cannot use it, so testing both costs one request.
    The third value is whether a tool was called (None when the request failed).
    """
    alias = direct_alias(provider.provider_id, model_id)
    response = gateway.chat_tools(alias)
    outcome, detail = classify(response)
    return outcome, detail, called_a_tool(response) if outcome is ProbeOutcome.OK else None


def probe_targets(
    state: HealthState,
    provider: Provider,
    now: datetime,
    *,
    recheck_after: timedelta,
    verified_by_traffic: Collection[str] = (),
) -> tuple[str, ...]:
    """Choose which of a provider's models to probe this cycle, newest and riskiest first.

    Real traffic is the cheapest health signal there is: a model that answered a
    user request needs no synthetic probe. Only three cases still need one — a model
    nobody has verified yet, a quarantined model whose backoff has expired, and a
    healthy model that has gone quiet long enough to have become stale. The
    per-provider cap then bounds even that, so verification never becomes the
    reason a free quota runs out.
    """
    if not provider.probe.enabled:
        return ()

    unknown: list[str] = []
    due: list[str] = []
    untested: list[str] = []
    stale: list[str] = []
    for health in state.models.values():
        if health.provider != provider.provider_id or health.key in verified_by_traffic:
            continue
        # A model inside its backoff window is skipped whatever its status. This
        # matters most for UNKNOWN: a throttled model keeps that status, so
        # without this check it never left the head of the unknown queue and
        # consumed the whole per-cycle budget on its own, every cycle.
        deferred = health.retry_after is not None and health.retry_after > now
        # A learned rejection (no tools) is not re-probed until it is forgotten.
        rejected = health.tools is False
        if deferred or rejected:
            continue
        if health.status is HealthStatus.UNKNOWN:
            unknown.append(health.model_id)
        elif health.status is HealthStatus.QUARANTINED:
            due.append(health.model_id)
        elif health.tools is None:
            # Healthy but never tool-tested: the capability gate needs one probe.
            untested.append(health.model_id)
        elif health.last_ok is None or now - health.last_ok >= recheck_after:
            stale.append(health.model_id)

    ordered = [*sorted(unknown), *sorted(due), *sorted(untested), *sorted(stale)]
    return tuple(ordered[: provider.probe.max_per_cycle])
