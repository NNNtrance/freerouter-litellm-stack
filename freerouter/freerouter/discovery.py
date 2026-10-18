from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from freerouter.policy import MIN_CONTEXT, hard_rejection, rank_key
from freerouter.registry import (
    AuthMode,
    DiscoveryMode,
    ProviderStatus,
    credentials_for,
    expand_env,
    matches_any,
    select_models,
)
from freerouter.transport import (
    as_items,
    as_price,
    as_strings,
    as_text,
    dig,
    request,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from freerouter.registry import Discovery, Provider
    from freerouter.transport import JsonValue

TEXT_MODALITY = "text"
CONTEXT_PATHS = (
    "context_length",
    "context_window.tokens",
    "context_window",
    "max_context_length",
    "max_input_tokens",
    "inputTokenLimit",
    "top_provider.context_length",
    "context_size",
)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    provider_id: str
    models: tuple[str, ...] = ()
    dropped_by_cap: tuple[str, ...] = ()
    dropped_by_gate: tuple[str, ...] = ()
    context: tuple[tuple[str, int], ...] = ()
    skipped: str | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        """Report whether the provider produced a routable model list."""
        return self.skipped is None and self.error is None


def _is_zero_price(value: JsonValue, value_path: str) -> bool | None:
    """Decide whether a price field is zero, returning None when it is unreadable."""
    if isinstance(value, list):
        if not value:
            return None
        rates = [
            as_price(dig(item, value_path)) if isinstance(item, dict) else as_price(item)
            for item in value
        ]
        if any(rate is None for rate in rates):
            return None
        return all(rate == 0 for rate in rates)
    price = as_price(value)
    return None if price is None else price == 0


def _is_free(item: JsonValue, discovery: Discovery) -> bool:
    """Apply the zero-price rule to one catalog entry."""
    if discovery.prompt_price_path is None or discovery.completion_price_path is None:
        return False
    prompt = _is_zero_price(dig(item, discovery.prompt_price_path), discovery.price_value_path)
    completion = _is_zero_price(
        dig(item, discovery.completion_price_path), discovery.price_value_path
    )
    return prompt is True and completion is True


def _is_text_model(item: JsonValue, discovery: Discovery) -> bool:
    """Apply the modality rules so media generators never enter a chat-completion pool.

    A zero prompt and completion price does not make a model free when it also
    bills per generated second or image, so any model that can emit a denied
    output modality is rejected even when its token prices are zero.
    """
    if discovery.input_modalities_path is None or discovery.output_modalities_path is None:
        return True
    outputs = as_strings(dig(item, discovery.output_modalities_path))
    if any(modality in outputs for modality in discovery.deny_output_modalities):
        return False
    if not discovery.require_text_io:
        return True
    inputs = as_strings(dig(item, discovery.input_modalities_path))
    return TEXT_MODALITY in inputs and TEXT_MODALITY in outputs


def _context(item: JsonValue, discovery: Discovery) -> int | None:
    """Read the context window a catalog entry advertises, if any."""
    for path in (discovery.context_path,) if discovery.context_path else CONTEXT_PATHS:
        value = dig(item, path)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _required(item: JsonValue, discovery: Discovery) -> bool:
    """Apply the ``require`` field rules (all must match)."""
    for path, accepted in discovery.require.items():
        value = dig(item, path)
        if str(value).lower() not in {str(option).lower() for option in accepted}:
            return False
    return True


def _model_ids(payload: JsonValue, discovery: Discovery) -> tuple[tuple[str, int | None], ...]:
    """Extract the candidate model ids, with their advertised context, from a catalog."""
    collected: list[tuple[str, int | None]] = []
    for item in as_items(dig(payload, discovery.items_path)):
        model_id = as_text(dig(item, discovery.id_path))
        if model_id is None:
            continue
        if discovery.id_strip_prefix and model_id.startswith(discovery.id_strip_prefix):
            model_id = model_id[len(discovery.id_strip_prefix) :]
        if discovery.mode is DiscoveryMode.PRICED_CATALOG and not _is_free(item, discovery):
            continue
        if not _is_text_model(item, discovery):
            continue
        if not _required(item, discovery):
            continue
        collected.append((model_id, _context(item, discovery)))
    return tuple(collected)


def _catalog_request(
    discovery: Discovery,
    secrets: <REDACTED_API_KEY>, str],
    credential: <REDACTED_API_KEY> | None,
) -> tuple[str, dict[str, str]] | None:
    """Build the catalog URL and headers, or None when the URL cannot be resolved."""
    if discovery.url is None:
        return None
    url = expand_env(discovery.url, secrets)
    if url is None:
        return None
    key = secrets.get(credential or "", "")
    headers: dict[str, str] = {}
    if discovery.auth is AuthMode.BEARER and key:
        headers["Authorization"] = f"Bearer {key}"
    if discovery.auth is AuthMode.QUERY_KEY and key:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}key={key}"
    return url, headers


def _skip_reason(
    provider: Provider,
    secrets: <REDACTED_API_KEY>, str],
    *,
    require_credentials: <REDACTED_API_KEY>,
) -> str | None:
    """Return why the provider must be skipped this cycle, or None to proceed."""
    if provider.status is ProviderStatus.RETIRED:
        return "retired"
    if not provider.routable:
        return provider.status.value
    if require_credentials and provider.credential and not secrets:
        <REDACTED_API_KEY> "missing " + ", ".join([provider.credential, *provider.extra_credentials])
    return None


def _finish(
    provider: Provider,
    entries: tuple[tuple[str, int | None], ...],
) -> DiscoveryResult:
    """Apply allow/deny, the usability pre-filter, the ranked cap, then the extra models.

    The pre-filter runs *before* the cap so a provider's slots go to models a client
    can actually use: rejected-by-name models and models whose catalog advertises
    less than the minimum context never compete for them.
    """
    context = {model_id: value for model_id, value in entries if value is not None}
    allow, deny = provider.discovery.allow, provider.discovery.deny
    gated: list[str] = []
    candidates: list[str] = []
    for model_id, _ in entries:
        # Gate only what the provider rules would route at all, so "dropped by the
        # gate" never counts paid catalog entries the allow list excludes anyway.
        if (allow and not matches_any(model_id, allow)) or matches_any(model_id, deny):
            continue
        too_small = model_id in context and context[model_id] < MIN_CONTEXT
        if hard_rejection(model_id) is not None or too_small:
            gated.append(model_id)
        else:
            candidates.append(model_id)
    kept, dropped = select_models(
        candidates,
        allow=provider.discovery.allow,
        deny=provider.discovery.deny,
        limit=provider.max_models,
        rank=rank_key,
    )
    models = tuple(sorted(set(kept) | set(provider.discovery.extra_models)))
    return DiscoveryResult(
        provider_id=provider.provider_id,
        models=models,
        dropped_by_cap=dropped,
        dropped_by_gate=tuple(sorted(set(gated))),
        context=tuple(sorted((m, c) for m, c in context.items() if m in models)),
    )


def _discover_static(provider: Provider) -> DiscoveryResult:
    """Resolve a hand-maintained model list without contacting the platform."""
    return _finish(provider, tuple((model_id, None) for model_id in provider.discovery.models))


def _discover_catalog(provider: Provider, secrets: <REDACTED_API_KEY>, str]) -> DiscoveryResult:
    """Resolve the model list from the platform's live catalog endpoint."""
    resolved = _catalog_request(provider.discovery, secrets, provider.credential)
    if resolved is None:
        return DiscoveryResult(
            provider_id=provider.provider_id, error="catalog URL could not be resolved"
        )

    url, headers = resolved
    response = request(url, headers=headers)
    if not response.ok:
        return DiscoveryResult(
            provider_id=provider.provider_id,
            error=f"catalog HTTP {response.status}: {response.text[:160]}",
        )

    result = _finish(provider, _model_ids(response.json(), provider.discovery))
    # All-gated is a real answer (the provider has nothing usable), not a catalog fault.
    if not result.models and not result.dropped_by_gate:
        return DiscoveryResult(
            provider_id=provider.provider_id,
            dropped_by_cap=result.dropped_by_cap,
            dropped_by_gate=result.dropped_by_gate,
            error="catalog returned no models matching the free-model rules",
        )
    return result


def discover(
    provider: Provider,
    env: Mapping[str, str] | None = None,
    *,
    require_credentials: <REDACTED_API_KEY> = True,
) -> DiscoveryResult:
    """Resolve the current free-model list for one provider."""
    secrets = <REDACTED_API_KEY>, env)
    reason = _skip_reason(provider, secrets, require_credentials=<REDACTED_API_KEY>
    if reason is not None:
        return DiscoveryResult(provider_id=provider.provider_id, skipped=reason)
    if provider.discovery.mode is DiscoveryMode.STATIC:
        return _discover_static(provider)
    return _discover_catalog(provider, secrets)
