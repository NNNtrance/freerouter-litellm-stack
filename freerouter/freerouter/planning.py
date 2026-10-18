from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from freerouter.policy import Policy
from freerouter.registry import expand_env, resolve_extra_params

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from freerouter.registry import Provider
    from freerouter.state import HealthState
    from freerouter.transport import JsonValue

POOL_ALIAS: Final = "free-router"
DIRECT_NAMESPACE: Final = "fr"
MANAGED_PREFIX: Final = "fr-"
DEPLOYMENT_ID_CHARS: Final = 16
DEFAULT_RPM: Final = 10
"""Fallback rpm for a provider that does not declare one.

LiteLLM's ``simple-shuffle`` weights candidate deployments by ``rpm`` and gives a
deployment with no ``rpm`` the weight ``0``, so it is never picked — which
silently defeats canonical pooling: the model merges across providers, then every
request still goes to whichever provider happens to declare a limit. Emitting a
conservative default for *every* deployment keeps the weighting fair. Providers
that declare their own real limit keep it, and LiteLLM enforces the default as an
actual rate limit, which also damps 429 storms on free tiers.

Lower it to 1 to effectively pin traffic to the providers that declare a limit.
"""

FAMILY_PREFIXES: Final = (
    "qwen",
    "llama",
    "gpt",
    "phi",
    "gemma",
    "glm",
    "nemotron",
    "deepseek",
    "minimax",
    "mistral",
    "codellama",
    "olmo",
    "smol",
    "exaone",
    "kimi",
)
"""Model-family names whose version suffix is written without a separator.

Providers disagree on spelling the same model: groq serves ``qwen/qwen3.8-27b``
while cerebras serves ``qwen-3.8-27b``, and nvidia serves ``meta/llama2-70b``
where others write ``llama-2-70b``. Without normalising, those become two
separate aliases and the model never pools.

This list is deliberately explicit. A blanket "insert a dash between a letter and
a digit" rule was measured against the live catalog and renamed 45 aliases into
nonsense — ``c4ai-aya-expanse-32b`` became ``c-4ai-...``, ``intern-s1`` became
``intern-s-1``, ``nemotron-3-ultra-550b-a55b`` became ``...-a-55b`` — because it
also splits size and version tokens that must stay intact. Restricted to family
prefixes it renames only the 8 aliases that genuinely need it.
"""


@dataclass(frozen=True, slots=True)
class Deployment:
    deployment_id: str
    alias: str
    provider_id: str
    model_id: str
    litellm_model: str
    tier: str
    api_base: str | None = None
    api_key_env: <REDACTED_API_KEY> | None = None
    rpm: int | None = None
    extra_params: tuple[tuple[str, JsonValue], ...] = ()
    max_input_tokens: <REDACTED_API_KEY> | None = None
    supports_tools: bool | None = None
    order: int | None = None

    def payload(self) -> dict[str, JsonValue]:
        """Render the LiteLLM ``/model/new`` request body for this deployment."""
        params: dict[str, JsonValue] = {"model": self.litellm_model}
        if self.api_key_env:
            <REDACTED_API_KEY>"api_key"] = f"os.environ/{self.api_key_env}"
        if self.api_base:
            params["api_base"] = self.api_base
        if self.rpm:
            params["rpm"] = self.rpm
        if self.order is not None:
            params["order"] = self.order
        params.update(dict(self.extra_params))
        info: dict[str, JsonValue] = {
            "id": self.deployment_id,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
            "cache_creation_input_token_cost": 0,
            "cache_read_input_token_cost": 0,
            "freerouter_provider": self.provider_id,
            "freerouter_tier": self.tier,
        }
        # LiteLLM's enable_pre_call_checks skips a deployment whose max_input_tokens
        # cannot fit the prompt, so a small-window route still serves small prompts.
        if self.max_input_tokens:
            <REDACTED_API_KEY>"max_input_tokens"] = self.max_input_tokens
        if self.supports_tools is not None:
            info["supports_function_calling"] = self.supports_tools
        return {"model_name": self.alias, "litellm_params": params, "model_info": info}


def direct_alias(provider_id: str, model_id: str) -> str:
    """Build the always-addressable alias for one model, for example ``fr/groq/llama-3.1-8b``."""
    return f"{DIRECT_NAMESPACE}/{provider_id}/{model_id}"


def canonical_alias(model_id: str) -> str:
    """Strip provider-specific decoration from a model id to get a cross-platform alias.

    ``openrouter/meta-llama/llama-3.3-70b-instruct:free`` and
    ``nvidia_nim/meta/llama-3.3-70b-instruct`` both mean the same model, so they
    should share one alias and pool behind it instead of appearing as two routes.
    """
    name = model_id.rsplit("/", 1)[-1]
    # ":free" (openrouter) and "-free" (zenmux, llmtr, zen) are pricing markers, not
    # part of the model's identity: glm-4.7-flash-free is glm-4.7-flash.
    if name.lower().endswith((":free", "-free")):
        name = name[:-5]
    # "-it" is Google's instruction-tuned marker: gemini/requesty/sambanova serve
    # gemma-4-31b-it while evren/ollama serve gemma-4-31b, the same weights.
    for suffix in ["-versatile", "-instant", "-instruct", "-it"]:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    for prefix in FAMILY_PREFIXES:
        if name.lower().startswith(prefix) and name[len(prefix) : len(prefix) + 1].isdigit():
            name = f"{name[: len(prefix)]}-{name[len(prefix) :]}"
            break
    # Ollama writes model tags with a colon (gpt-oss:120b, gemma4:31b). LiteLLM
    # rejects a model_group containing one, so those deployments were registered
    # but unroutable. Flattening to a dash also lets them pool with the same model
    # served elsewhere: gpt-oss:120b joins cerebras+groq on gpt-oss-120b.
    name = name.replace(":", "-")
    return name.replace("_", "-").lower()


def deployment_id(provider_id: str, model_id: str, alias: str, fingerprint: str = "") -> str:
    """Derive a stable id from the deployment's whole desired configuration.

    The fingerprint has to cover every field that ends up in ``litellm_params``, not
    just the model's identity. Otherwise editing a provider's request parameters
    leaves the id unchanged, the reconciler sees no difference, and the live
    deployment silently keeps serving the old configuration.
    """
    material = f"{provider_id}|{model_id}|{alias}|{fingerprint}"
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"{MANAGED_PREFIX}{digest[:DEPLOYMENT_ID_CHARS]}"


def is_managed(candidate: str) -> bool:
    """Report whether a LiteLLM deployment id belongs to FreeRouter."""
    return candidate.startswith(MANAGED_PREFIX)


def _build(
    provider: Provider,
    model_id: str,
    alias: str,
    api_base: str | None,
    capability: tuple[int | None, bool | None, int | None] = (None, None, None),
) -> Deployment:
    """Assemble one deployment record for a provider, model and alias.

    ``capability`` is ``(max_input_tokens, supports_tools, order)``: the first two go
    into model_info, ``order`` is the role tier (LiteLLM tries lower orders first).
    """
    max_input_tokens, supports_tools, order = capability
    litellm_model = f"{provider.litellm_prefix}/{model_id}"
    extra = tuple(sorted(resolve_extra_params(provider, model_id).items()))
    # The value that actually reaches ``litellm_params``, not the provider's raw
    # declaration. Hashing the raw value instead would let a change to
    # DEFAULT_RPM leave every id unchanged, and the reconciler would then never
    # re-post the deployments that need the new limit.
    rpm = provider.limits.rpm or DEFAULT_RPM
    fingerprint = "|".join(
        [
            litellm_model,
            api_base or "",
            provider.credential or "",
            str(rpm),
            repr(extra),
            # model_info is part of the row too: a learned context ceiling that never
            # reached LiteLLM would leave pre-call checks reading the old number.
            str(max_input_tokens or ""),
            str(supports_tools),
            str(order),
        ]
    )
    return Deployment(
        deployment_id=deployment_id(provider.provider_id, model_id, alias, fingerprint),
        alias=alias,
        provider_id=provider.provider_id,
        model_id=model_id,
        litellm_model=litellm_model,
        tier=provider.tier.value,
        api_base=api_base,
        api_key_env=<REDACTED_API_KEY>,
        rpm=rpm,
        extra_params=extra,
        max_input_tokens=<REDACTED_API_KEY>,
        supports_tools=supports_tools,
        order=order,
    )


def plan_provider(
    provider: Provider,
    state: HealthState,
    env: Mapping[str, str],
    policy: Policy | None = None,
) -> tuple[Deployment, ...]:
    """Plan every deployment FreeRouter wants for one provider.

    A usable, healthy deployment joins ``free-router``, its canonical alias and any
    role whose member list names that canonical. A rejected one (no tool calling,
    too little context, excluded) keeps only its ``fr/`` row: clients never see it,
    but the next probe can still reach it, which is how a rejection heals itself.
    """
    policy = policy or Policy()
    api_base = expand_env(provider.api_base, env) if provider.api_base else None
    if provider.api_base and api_base is None:
        return ()

    deployments: list[Deployment] = []
    for health in sorted(state.models.values(), key=lambda item: item.model_id):
        if health.provider != provider.provider_id:
            continue
        canonical = policy.canonical(canonical_alias(health.model_id))
        context = policy.effective_context(health, provider, canonical)
        override = policy.override_for(health, canonical)
        tools = override.tools if override.tools is not None else health.tools
        aliases: list[tuple[str, int | None]] = [
            (direct_alias(provider.provider_id, health.model_id), None)
        ]
        if health.poolable and policy.rejection(health, provider, canonical) is None:
            aliases.append((POOL_ALIAS, None))
            if canonical not in policy.exclude:
                aliases.append((canonical, None))
                aliases.extend(policy.roles_for(canonical, context))
        deployments.extend(
            _build(provider, health.model_id, alias, api_base, (context, tools, order))
            for alias, order in aliases
        )
    return tuple(deployments)


def plan_deployments(
    providers: Sequence[Provider],
    state: HealthState,
    env: Mapping[str, str],
    policy: Policy | None = None,
) -> tuple[Deployment, ...]:
    """Plan the full desired deployment set across every routable provider."""
    planned: list[Deployment] = []
    for provider in providers:
        if provider.routable:
            planned.extend(plan_provider(provider, state, env, policy))
    return tuple(planned)


def diff(
    desired: Iterable[Deployment],
    current: Iterable[str],
) -> tuple[tuple[Deployment, ...], tuple[str, ...]]:
    """Compute which deployments to add and which managed ids to delete."""
    wanted = {deployment.deployment_id: deployment for deployment in desired}
    live = {candidate for candidate in current if is_managed(candidate)}
    to_add = tuple(wanted[key] for key in sorted(wanted.keys() - live))
    to_delete = tuple(sorted(live - wanted.keys()))
    return to_add, to_delete
