"""Model-catalog regression checks; provider availability remains a live fact."""

import pytest
from pydantic import ValidationError

from lattice.contracts import ProviderRoute
from lattice.execution import DEFAULT_ADAPTERS, SubscriptionRouteUnavailable, validate_execution_policy


# Independently transcribed from provider catalogs documented in providers/README.md.
CURRENT_MODELS = {
    "anthropic": ("claude-opus-5-5", "claude-sonnet-5", "claude-fable-5-1", "claude-haiku-4-5-20251001", "haiku", "fable"),
    "openai": ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"),
    "google": (
        "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
        "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
        "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
        "gemini-3.1-pro-high", "gemini-3.1-pro-low",
    ),
    "xai": ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5"),
    "meta": ("muse-spark-1.3", "muse-spark-1.3-contributor", "muse-spark-1.2", "muse-spark-1.2-contributor"),
}


@pytest.mark.regression
@pytest.mark.parametrize("provider,model", [(provider, model) for provider, models in CURRENT_MODELS.items() for model in models])
def test_catalog_admits_current_models_without_adding_execution_authority(provider, model):
    base = next(profile for profile in DEFAULT_ADAPTERS if profile.provider_code == provider)
    selected = base.select_model(model)
    effort = model.rsplit("-", 1)[-1] if provider == "google" else "high"
    route = ProviderRoute(provider=provider, harness=base.harness, model=model, effort=effort)
    assert selected.model_code == model
    assert effort in selected.supported_efforts
    assert route.provider_profile_id == selected.profile_id
    assert selected.isolation == base.isolation
    assert selected.qualified_profiles == base.qualified_profiles
    assert selected.definition()["auth_surface"] == "consumer_subscription_cli"
    policy = validate_execution_policy(selected)
    assert policy["tools"] == []
    assert policy["network_policy"] == "disabled"
    with pytest.raises(SubscriptionRouteUnavailable, match="compiled-fresh cannot receive"):
        validate_execution_policy(selected, tools=["shell"])


@pytest.mark.regression
def test_catalog_refresh_does_not_admit_unknown_ids_or_cross_provider_models():
    for base in DEFAULT_ADAPTERS:
        with pytest.raises(SubscriptionRouteUnavailable, match="does not admit model"):
            base.select_model("unlisted-future-model")
    with pytest.raises(ValidationError, match="does not admit model"):
        ProviderRoute(provider="openai", harness="codex", model="claude-opus-5-5")
    with pytest.raises(ValidationError, match="qualified only through"):
        ProviderRoute(provider="anthropic", harness="codex", model="claude-opus-5-5")


@pytest.mark.regression
def test_kimi_ids_are_current_but_route_remains_unqualified():
    kimi = next(profile for profile in DEFAULT_ADAPTERS if profile.provider_code == "moonshot")
    assert kimi.model_code == "k3"
    assert kimi.supported_models == ("k3", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed")
    for model in kimi.supported_models:
        with pytest.raises(SubscriptionRouteUnavailable, match="no official subscription-authenticated local CLI has been qualified"):
            validate_execution_policy(kimi.select_model(model))
