from __future__ import annotations

from dataclasses import replace

from freerouter.planning import (
    DEFAULT_RPM,
    POOL_ALIAS,
    canonical_alias,
    deployment_id,
    diff,
    direct_alias,
    is_managed,
    plan_deployments,
)
from freerouter.state import HealthStatus
from tests.conftest import make_health, make_provider, make_state

ENV = {"DEMO_API_KEY": "secret"}


def test_healthy_models_get_the_pool_the_canonical_and_the_direct_alias() -> None:
    # Given
    provider = make_provider()
    state = make_state(make_health(status=HealthStatus.HEALTHY))

    # When
    plan = plan_deployments([provider], state, ENV)

    # Then
    assert {entry.alias for entry in plan} == {
        POOL_ALIAS,
        "fr/demo/alpha",
        "alpha",
    }
    assert all(entry.litellm_model == "openai/alpha" for entry in plan)
    assert all(entry.api_base == "https://demo.example/v1" for entry in plan)
    assert all(entry.api_key_env == "DEMO_API_KEY" for entry in plan)


def test_unverified_and_quarantined_models_stay_out_of_the_shared_pool() -> None:
    # Given
    provider = make_provider()
    state = make_state(
        make_health(model_id="new"),
        make_health(model_id="broken", status=HealthStatus.QUARANTINED),
    )

    # When
    aliases = {entry.alias for entry in plan_deployments([provider], state, ENV)}

    # Then
    assert aliases == {"fr/demo/new", "fr/demo/broken"}
    assert POOL_ALIAS not in aliases


def test_a_provider_missing_a_templated_variable_plans_nothing() -> None:
    # Given
    provider = make_provider(
        api_base="https://demo.example/${DEMO_ACCOUNT_ID}/v1",
        extra_credentials=["DEMO_ACCOUNT_ID"],
    )
    state = make_state(make_health(status=HealthStatus.HEALTHY))

    # When
    plan = plan_deployments([provider], state, ENV)

    # Then
    assert plan == ()


def test_retired_providers_are_never_planned() -> None:
    # Given
    provider = make_provider(status="retired")
    state = make_state(make_health(status=HealthStatus.HEALTHY))

    # When / Then
    assert plan_deployments([provider], state, ENV) == ()


def test_deployment_ids_are_stable_and_recognisable() -> None:
    # Given / When
    first = deployment_id("demo", "alpha", POOL_ALIAS)
    second = deployment_id("demo", "alpha", POOL_ALIAS)
    other = deployment_id("demo", "alpha", "demo-free")

    # Then
    assert first == second
    assert first != other
    assert is_managed(first)
    assert not is_managed("some-user-model")


def test_diff_adds_missing_and_removes_stale_without_touching_user_models() -> None:
    # Given
    provider = make_provider()
    state = make_state(make_health(status=HealthStatus.HEALTHY))
    desired = plan_deployments([provider], state, ENV)
    stale = deployment_id("demo", "removed-model", POOL_ALIAS)
    current = [stale, "user-owned-deployment"]

    # When
    to_add, to_delete = diff(desired, current)

    # Then
    assert len(to_add) == len(desired)
    assert to_delete == (stale,)


def test_diff_is_idempotent_once_applied() -> None:
    # Given
    provider = make_provider()
    state = make_state(make_health(status=HealthStatus.HEALTHY))
    desired = plan_deployments([provider], state, ENV)

    # When
    to_add, to_delete = diff(desired, [entry.deployment_id for entry in desired])

    # Then
    assert to_add == ()
    assert to_delete == ()


def test_payload_marks_the_deployment_free_and_traceable() -> None:
    # Given
    provider = make_provider(limits={"rpm": 20})
    state = make_state(make_health(status=HealthStatus.HEALTHY))
    entry = next(e for e in plan_deployments([provider], state, ENV) if e.alias == POOL_ALIAS)

    # When
    payload = entry.payload()

    # Then
    assert payload["model_name"] == POOL_ALIAS
    assert payload["litellm_params"] == {
        "model": "openai/alpha",
        "api_key": "os.environ/DEMO_API_KEY",
        "api_base": "https://demo.example/v1",
        "rpm": 20,
    }
    assert payload["model_info"] == {
        "id": entry.deployment_id,
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "cache_creation_input_token_cost": 0,
        "cache_read_input_token_cost": 0,
        "freerouter_provider": "demo",
        "freerouter_tier": "free",
    }


def test_direct_alias_keeps_the_platform_model_id_addressable() -> None:
    assert direct_alias("openrouter", "z-ai/glm-5.2:free") == "fr/openrouter/z-ai/glm-5.2:free"


def test_every_deployment_declares_an_rpm_so_pooling_stays_fair() -> None:
    """LiteLLM weights by rpm and scores a missing rpm as 0, so a provider that
    omits the limit would never be picked and the merge would be a no-op."""
    # Given: one provider that declares a limit, one that does not
    declared = make_provider(id="declared", limits={"rpm": 20})
    undeclared = make_provider(id="undeclared")
    state = make_state(
        make_health(provider="declared", status=HealthStatus.HEALTHY),
        make_health(provider="undeclared", status=HealthStatus.HEALTHY),
    )

    # When
    plan = plan_deployments([declared, undeclared], state, ENV)

    # Then: no deployment may be left without a weight
    assert all(entry.rpm for entry in plan)
    assert {e.rpm for e in plan if e.provider_id == "declared"} == {20}
    assert {e.rpm for e in plan if e.provider_id == "undeclared"} == {DEFAULT_RPM}


def test_a_declared_rpm_reaches_the_litellm_payload() -> None:
    # Given / When
    entry = next(
        e
        for e in plan_deployments(
            [make_provider()], make_state(make_health(status=HealthStatus.HEALTHY)), ENV
        )
        if e.alias == POOL_ALIAS
    )

    # Then
    params = entry.payload()["litellm_params"]
    assert isinstance(params, dict)
    assert params["rpm"] == DEFAULT_RPM


def test_canonical_alias_flattens_an_ollama_style_tag() -> None:
    """LiteLLM refuses a model_group containing ':', so ollama's gpt-oss:120b would
    be registered but unroutable -- and would miss the pool it belongs in."""
    assert canonical_alias("gpt-oss:120b") == "gpt-oss-120b"
    assert canonical_alias("gpt-oss:20b") == "gpt-oss-20b"
    assert canonical_alias("gemma4:31b") == "gemma-4-31b"
    assert canonical_alias("nemotron-3-nano:30b") == "nemotron-3-nano-30b"
    assert canonical_alias("mistral-large-3:675b") == "mistral-large-3-675b"
    assert canonical_alias("deepseek-v4-pro:0813") == "deepseek-v4-pro-0813"


def test_canonical_alias_strips_namespace_free_tag_and_variant_suffix() -> None:
    assert canonical_alias("meta-llama/llama-3.3-70b-instruct:free") == "llama-3.3-70b"
    assert canonical_alias("openai/gpt-oss-120b") == "gpt-oss-120b"
    assert canonical_alias("llama-3.1-8b-versatile") == "llama-3.1-8b"
    assert canonical_alias("llama-3.1-8b-instant") == "llama-3.1-8b"
    assert canonical_alias("Some_Vendor/Big_Model:Free") == "big-model"


def test_canonical_alias_normalises_a_family_version_written_without_a_dash() -> None:
    """groq spells it qwen3.8-27b and cerebras qwen-3.8-27b; both must land on one alias."""
    assert canonical_alias("qwen/qwen3.8-27b") == "qwen-3.8-27b"
    assert canonical_alias("qwen-3.8-27b") == "qwen-3.8-27b"
    assert canonical_alias("meta/llama2-70b") == "llama-2-70b"
    assert canonical_alias("nvidia/llama3-chatqa-1.5-70b") == "llama-3-chatqa-1.5-70b"
    assert canonical_alias("Qwen/Qwen3.5-397B-A17B") == "qwen-3.5-397b-a17b"


def test_canonical_alias_leaves_size_and_version_tokens_alone() -> None:
    """A blanket letter->digit rule would mangle these; the family list must not."""
    assert canonical_alias("ibm/granite-3.0-3b-a800m-instruct") == "granite-3.0-3b-a800m"
    assert canonical_alias("c4ai-aya-expanse-32b") == "c4ai-aya-expanse-32b"
    assert canonical_alias("deepseek-r1-distill-qwen-32b") == "deepseek-r1-distill-qwen-32b"
    assert canonical_alias("mistralai/codestral-22b-instruct-v0.1") == (
        "codestral-22b-instruct-v0.1"
    )
    assert canonical_alias("nvidia/nemotron-3-ultra-550b-a55b") == "nemotron-3-ultra-550b-a55b"
    assert canonical_alias("MiniMax/MiniMax-M3") == "minimax-m3"


def test_the_same_model_on_two_platforms_lands_on_one_canonical_alias() -> None:
    """同一模型在多个平台上线时，canonical alias 把它们收进同一个池。"""
    openrouter = make_provider(
        id="openrouter",
        discovery={"mode": "static", "models": ["meta-llama/llama-3.3-70b-instruct:free"]},
    )
    nvidia = make_provider(
        id="nvidia",
        discovery={"mode": "static", "models": ["meta/llama-3.3-70b-instruct"]},
    )
    state = make_state(
        make_health(
            provider="openrouter",
            model_id="meta-llama/llama-3.3-70b-instruct:free",
            status=HealthStatus.HEALTHY,
        ),
        make_health(
            provider="nvidia",
            model_id="meta/llama-3.3-70b-instruct",
            status=HealthStatus.HEALTHY,
        ),
    )

    plan = plan_deployments([openrouter, nvidia], state, ENV)

    canonical = [e for e in plan if e.alias == "llama-3.3-70b"]
    assert len(canonical) == 2
    assert {e.provider_id for e in canonical} == {"openrouter", "nvidia"}


def test_changing_request_parameters_forces_the_deployment_to_be_replaced() -> None:
    """改了平台请求参数，线上部署必须跟着换，不能悄悄留着旧配置。"""
    # Given
    state = make_state(make_health(status=HealthStatus.HEALTHY))
    before = plan_deployments([make_provider()], state, ENV)
    after = plan_deployments([make_provider(extra_params={"enable_thinking": False})], state, ENV)

    # When
    to_add, to_delete = diff(after, [entry.deployment_id for entry in before])

    # Then
    assert len(to_add) == len(after)
    assert len(to_delete) == len(before)


def test_changing_the_fallback_rpm_forces_the_deployment_to_be_replaced() -> None:
    """The fallback rpm lands in litellm_params, so it has to move the id too —
    otherwise the reconciler never re-posts and the old limit stays live."""
    # Given
    state = make_state(make_health(status=HealthStatus.HEALTHY))
    before = plan_deployments([make_provider()], state, ENV)

    # When: a provider with no declared limit, fingerprinted at a different default
    after = [
        replace(
            entry,
            deployment_id=deployment_id(
                entry.provider_id,
                entry.model_id,
                entry.alias,
                str(DEFAULT_RPM + 1),
            ),
        )
        for entry in before
    ]

    # Then
    to_add, to_delete = diff(after, [entry.deployment_id for entry in before])
    assert len(to_add) == len(after)
    assert len(to_delete) == len(before)


def test_per_model_rules_override_the_provider_default() -> None:
    # Given a provider whose thinking-only variant must not receive the flag
    provider = make_provider(
        extra_params={"enable_thinking": False},
        extra_params_overrides=[{"match": ["*-thinking"], "params": {"enable_thinking": None}}],
        discovery={"mode": "static", "models": ["base", "big-thinking"]},
    )
    state = make_state(
        make_health(model_id="base", status=HealthStatus.HEALTHY),
        make_health(model_id="big-thinking", status=HealthStatus.HEALTHY),
    )

    # When
    plan = {e.model_id: dict(e.extra_params) for e in plan_deployments([provider], state, ENV)}

    # Then
    assert plan["base"] == {"enable_thinking": False}
    assert plan["big-thinking"] == {}


def test_request_parameters_reach_the_litellm_payload() -> None:
    # Given
    provider = make_provider(extra_params={"enable_thinking": False})
    state = make_state(make_health(status=HealthStatus.HEALTHY))

    # When
    payload = next(iter(plan_deployments([provider], state, ENV))).payload()

    # Then
    params = payload["litellm_params"]
    assert isinstance(params, dict)
    assert params["enable_thinking"] is False
