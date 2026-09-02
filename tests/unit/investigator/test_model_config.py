"""Unit tests for the default investigator model configuration.

The legacy ``LOCAL_OLAMMA_API_KEY`` must enable the Pydantic AI Ollama
investigator automatically, with the cloud default pinned to exactly
``gpt-oss:120b-cloud``. When no explicit ``OLLAMA_BASE_URL`` is set, the
cloud default is the custom native ``/api/chat`` adapter; an explicit
``OLLAMA_BASE_URL`` keeps Pydantic AI's bundled OpenAI-compatible
``OllamaModel`` with the local ``gpt-oss:120b`` tag (``-cloud`` is a
hosted-only tag; still the same Ollama gpt-oss 120b model, never another
vendor/model). The key must never be rendered anywhere. Provider construction
is faked only — no real model calls happen in these tests.
"""

import pytest
from pydantic_ai.models.ollama import OllamaModel

from sta.config import OLLAMA_CLOUD_BASE_URL, OLLAMA_CLOUD_NATIVE_URL, Settings, get_settings
from sta.investigator.agent import (
    OLLAMA_LOCAL_MODEL_NAME,
    OLLAMA_MODEL_NAME,
    CallbackInvestigator,
    InvestigatorNotConfiguredError,
    PydanticAiInvestigator,
    create_investigator,
)
from sta.investigator.ollama_cloud_native_model import OllamaCloudNativeModel

SECRET = "sta-test-api-key-123"

_MODEL_ENV_VARS = (
    "LOCAL_OLAMMA_API_KEY",
    "OLLAMA_API_KEY",
    "OLLAMA_BASE_URL",
    "STA_INVESTIGATOR_MODEL",
)


@pytest.fixture
def model_env(tmp_path, monkeypatch):
    """Isolated model-configuration environment: no ``.env`` file, no model
    variables set, and a cleared ``get_settings`` cache (restored after)."""
    monkeypatch.chdir(tmp_path)
    for name in _MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Settings: legacy key normalization and secret handling
# ---------------------------------------------------------------------------


def test_legacy_local_olamma_key_is_normalized_to_internal_secret(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", SECRET)
    settings = get_settings()
    assert settings.ollama_api_key == SECRET
    assert settings.ollama_base_url is None


def test_current_ollama_api_key_wins_over_legacy_alias(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", "legacy-key")
    model_env.setenv("OLLAMA_API_KEY", SECRET)
    assert get_settings().ollama_api_key == SECRET


def test_explicit_local_base_url_is_configured(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", SECRET)
    model_env.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    settings = get_settings()
    assert settings.ollama_base_url == "http://localhost:11434/v1"


def test_api_key_is_secret_never_rendered():
    settings = Settings(ollama_api_key=SECRET)
    rendered = f"{settings!r} {settings!s}"
    assert SECRET not in rendered
    summary = settings.safe_summary()
    assert SECRET not in str(summary)
    assert summary["OLLAMA_API_KEY"] == "***"
    assert summary["OLLAMA_BASE_URL"] == OLLAMA_CLOUD_NATIVE_URL


def test_safe_summary_reports_configured_local_base_url():
    summary = Settings(
        ollama_api_key="k", ollama_base_url="http://localhost:11434/v1"
    ).safe_summary()
    assert summary["OLLAMA_BASE_URL"] == "http://localhost:11434/v1"
    assert summary["OLLAMA_API_KEY"] == "***"


def test_safe_summary_omits_ollama_entries_without_a_key():
    summary = Settings().safe_summary()
    assert "OLLAMA_API_KEY" not in summary
    assert "OLLAMA_BASE_URL" not in summary


# ---------------------------------------------------------------------------
# create_investigator: default factory wiring (fake provider construction only)
# ---------------------------------------------------------------------------


def test_legacy_env_key_enables_native_cloud_investigator_by_default(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", SECRET)
    investigator = create_investigator()
    assert isinstance(investigator, PydanticAiInvestigator)
    model = investigator._model
    assert isinstance(model, OllamaCloudNativeModel)
    assert model.model_name == OLLAMA_MODEL_NAME == "gpt-oss:120b-cloud"
    assert model.base_url.rstrip("/") == OLLAMA_CLOUD_NATIVE_URL
    assert model._api_key == SECRET


def test_explicit_local_base_url_uses_local_gpt_oss_tag(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", SECRET)
    model_env.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    investigator = create_investigator()
    model = investigator._model
    assert isinstance(model, OllamaModel)
    # The local daemon keeps Ollama's local tag: "-cloud" is hosted-only.
    # Still Ollama gpt-oss 120b — never another vendor or model.
    assert model.model_name == OLLAMA_LOCAL_MODEL_NAME == "gpt-oss:120b"
    assert model.provider.base_url.rstrip("/") == "http://localhost:11434/v1"


def test_no_fallback_model_when_no_key_is_configured(model_env):
    with pytest.raises(InvestigatorNotConfiguredError):
        create_investigator()


def test_explicit_model_and_callback_still_take_precedence(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", SECRET)
    from pydantic_ai.providers.ollama import OllamaProvider

    explicit = OllamaModel(
        OLLAMA_MODEL_NAME,
        provider=OllamaProvider(base_url=OLLAMA_CLOUD_BASE_URL, api_key="explicit"),
    )
    assert create_investigator(model=explicit)._model is explicit

    callback = lambda session: None  # noqa: E731
    assert isinstance(create_investigator(callback=callback), CallbackInvestigator)


def test_sta_investigator_model_override_still_works(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", SECRET)
    model_env.setenv("STA_INVESTIGATOR_MODEL", "ollama:gpt-oss:120b-cloud")
    model_env.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    investigator = create_investigator()
    assert isinstance(investigator, PydanticAiInvestigator)
    # Even the explicit override path stays on the pinned gpt-oss model.
    assert investigator._agent.model.model_name == "gpt-oss:120b-cloud"


def test_api_key_is_never_exposed_by_the_configured_model(model_env):
    model_env.setenv("LOCAL_OLAMMA_API_KEY", SECRET)
    investigator = create_investigator()
    model = investigator._model
    rendered = "".join(
        f"{value!s}{value!r}"
        for value in (investigator, model)
    )
    assert SECRET not in rendered
    # The custom model stores the key in a private attribute; ensure it is not
    # reachable through public repr/str.
    assert "_api_key" not in repr(model)