"""Provider adapters: request shape, response parsing, selection, and failure.

HTTP is stubbed at `llm._post_json`, so these run offline and assert on the
exact payload each provider would receive.
"""

from __future__ import annotations

import json

import pytest

from backend import llm

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}
MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from a known, credential-free environment."""
    for name in (
        "DISABLE_LLM", "LLM_PROVIDER",
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_BASE_URL",
        "GROQ_API_KEY", "GROQ_MODEL", "GROQ_BASE_URL",
        "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_BASE_URL",
        "OLLAMA_HOST", "OLLAMA_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


class Recorder:
    """Stands in for `_post_json`, capturing the outgoing request."""

    def __init__(self, response: dict | Exception):
        self.response = response
        self.url: str | None = None
        self.payload: dict | None = None
        self.headers: dict | None = None

    def __call__(self, url, payload, headers, timeout=60):
        self.url, self.payload, self.headers = url, payload, headers
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# ------------------------------------------------------------- extract_json
@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Sure! {"a": 1} hope that helps', {"a": 1}),
        ('{"a": "}"}', {"a": "}"}),                 # brace inside a string
        ('{"a": {"b": 2}} trailing', {"a": {"b": 2}}),
        ('{"a": "say \\"hi\\""}', {"a": 'say "hi"'}),  # escaped quotes
    ],
)
def test_extract_json_recovers_objects(raw, expected):
    assert json.loads(llm.extract_json(raw)) == expected


@pytest.mark.parametrize("raw", ["", "no json here", "[1,2,3]", "{unbalanced"])
def test_extract_json_gives_up_cleanly(raw):
    result = llm.extract_json(raw)
    assert result is None or not result.startswith("{") or "unbalanced" in raw


# ------------------------------------------------------------------- Gemini
def test_gemini_request_shape(monkeypatch):
    recorder = Recorder({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    monkeypatch.setattr(llm, "_post_json", recorder)

    provider = llm.GeminiProvider(
        name="gemini", model="gemini-3.6-flash", api_key="k",
        base_url="https://example.test",
    )
    assert provider.complete("SYS", MESSAGES, 100, SCHEMA) == "hi"

    assert recorder.url == (
        "https://example.test/v1beta/models/gemini-3.6-flash:generateContent"
    )
    assert recorder.headers["x-goog-api-key"] == "k"
    instruction = recorder.payload["systemInstruction"]["parts"][0]["text"]
    assert instruction.startswith("SYS")
    assert "JSON Schema" in instruction          # schema injected into the prompt
    assert recorder.payload["generationConfig"]["responseMimeType"] == "application/json"
    assert recorder.payload["contents"][0]["parts"][0]["text"] == "hello"


def test_gemini_maps_assistant_role_to_model(monkeypatch):
    recorder = Recorder({"candidates": [{"content": {"parts": [{"text": "x"}]}}]})
    monkeypatch.setattr(llm, "_post_json", recorder)
    provider = llm.GeminiProvider(name="gemini", model="m", api_key="k",
                                  base_url="https://e.test")
    provider.complete("S", [{"role": "assistant", "content": "prior"}], 50)
    assert recorder.payload["contents"][0]["role"] == "model"


def test_gemini_without_schema_omits_json_mode(monkeypatch):
    recorder = Recorder({"candidates": [{"content": {"parts": [{"text": "x"}]}}]})
    monkeypatch.setattr(llm, "_post_json", recorder)
    provider = llm.GeminiProvider(name="gemini", model="m", api_key="k",
                                  base_url="https://e.test")
    provider.complete("S", MESSAGES, 50)
    assert "responseMimeType" not in recorder.payload["generationConfig"]


def test_gemini_blocked_response_returns_none(monkeypatch):
    recorder = Recorder({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    monkeypatch.setattr(llm, "_post_json", recorder)
    provider = llm.GeminiProvider(name="gemini", model="m", api_key="k",
                                  base_url="https://e.test")
    assert provider.complete("S", MESSAGES, 50) is None


# ------------------------------------------------------- OpenAI-compatible
def test_openai_compatible_request_shape(monkeypatch):
    recorder = Recorder({"choices": [{"message": {"content": "answer"}}]})
    monkeypatch.setattr(llm, "_post_json", recorder)

    provider = llm.OpenAICompatProvider(
        name="groq", model="llama-3.3-70b-versatile", api_key="gk",
        base_url="https://api.groq.test/openai/v1",
    )
    assert provider.complete("SYS", MESSAGES, 200, SCHEMA) == "answer"

    assert recorder.url == "https://api.groq.test/openai/v1/chat/completions"
    assert recorder.headers["Authorization"] == "Bearer gk"
    assert recorder.payload["model"] == "llama-3.3-70b-versatile"
    assert recorder.payload["response_format"] == {"type": "json_object"}
    assert recorder.payload["messages"][0]["role"] == "system"
    assert recorder.payload["messages"][1] == {"role": "user", "content": "hello"}


def test_openrouter_sends_attribution_headers(monkeypatch):
    recorder = Recorder({"choices": [{"message": {"content": "a"}}]})
    monkeypatch.setattr(llm, "_post_json", recorder)
    provider = llm.OpenAICompatProvider(
        name="openrouter", model="m", api_key="k", base_url="https://o.test/api/v1",
        extra={"headers": {"X-Title": "Learning Path Recommender"}},
    )
    provider.complete("S", MESSAGES, 50)
    assert recorder.headers["X-Title"] == "Learning Path Recommender"


def test_openai_compatible_empty_choices_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "_post_json", Recorder({"choices": []}))
    provider = llm.OpenAICompatProvider(name="groq", model="m", api_key="k",
                                        base_url="https://g.test")
    assert provider.complete("S", MESSAGES, 50) is None


# ------------------------------------------------------------------- Ollama
def test_ollama_request_shape(monkeypatch):
    recorder = Recorder({"message": {"content": "local answer"}})
    monkeypatch.setattr(llm, "_post_json", recorder)

    provider = llm.OllamaProvider(
        name="ollama", model="llama3.1", base_url="http://localhost:11434"
    )
    assert provider.complete("SYS", MESSAGES, 300, SCHEMA) == "local answer"

    assert recorder.url == "http://localhost:11434/api/chat"
    assert recorder.payload["stream"] is False
    assert recorder.payload["format"] == "json"
    assert recorder.payload["options"]["num_predict"] == 300
    assert recorder.headers == {}          # no credential ever sent


def test_ollama_unreachable_returns_none(monkeypatch):
    monkeypatch.setattr(
        llm, "_post_json", Recorder(llm.LLMError("connection refused"))
    )
    provider = llm.OllamaProvider(name="ollama", model="m",
                                  base_url="http://localhost:11434")
    assert provider.complete("S", MESSAGES, 50) is None


# ---------------------------------------------------------------- selection
def test_no_credentials_means_no_provider():
    assert llm.resolve_provider() is None
    assert llm.status()["enabled"] is False
    assert llm.status()["mode"] == "rule-engine"


def test_disable_llm_wins_over_everything(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("DISABLE_LLM", "1")
    assert llm.resolve_provider() is None


@pytest.mark.parametrize(
    "env,expected",
    [
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("GEMINI_API_KEY", "gemini"),
        ("GOOGLE_API_KEY", "gemini"),
        ("GROQ_API_KEY", "groq"),
        ("OPENROUTER_API_KEY", "openrouter"),
    ],
)
def test_each_credential_selects_its_provider(monkeypatch, env, expected):
    monkeypatch.setenv(env, "test-key")
    provider = llm.resolve_provider()
    assert provider is not None and provider.name == expected


def test_priority_order_when_several_are_configured(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert llm.resolve_provider().name == "anthropic"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert llm.resolve_provider().name == "gemini"

    monkeypatch.delenv("GEMINI_API_KEY")
    assert llm.resolve_provider().name == "groq"


def test_explicit_provider_overrides_priority(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert llm.resolve_provider().name == "groq"


def test_explicit_provider_without_credential_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm.resolve_provider() is None


def test_unknown_provider_name_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "skynet")
    assert llm.resolve_provider() is None


def test_ollama_requires_explicit_opt_in(monkeypatch):
    """Otherwise every machine would look like it has an LLM."""
    assert llm.resolve_provider() is None

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    provider = llm.resolve_provider()
    assert provider is not None and provider.name == "ollama"
    assert provider.base_url == "http://localhost:11434"


def test_ollama_host_alone_is_enough(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box:11434")
    provider = llm.resolve_provider()
    assert provider.name == "ollama"
    assert provider.base_url == "http://gpu-box:11434"


def test_model_is_overridable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert llm.resolve_provider().model == llm.DEFAULT_MODELS["gemini"]
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    assert llm.resolve_provider().model == "gemini-2.5-pro"


def test_status_lists_every_configured_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    status = llm.status()
    assert status["enabled"] is True
    assert status["provider"] == "gemini"          # priority winner
    assert set(status["configured_providers"]) == {"gemini", "groq"}


def test_complete_returns_provider_name(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(
        llm, "_post_json", Recorder({"choices": [{"message": {"content": "yo"}}]})
    )
    text, provider = llm.complete("S", MESSAGES, 50)
    assert (text, provider) == ("yo", "groq")


def test_complete_with_no_provider_returns_nones():
    assert llm.complete("S", MESSAGES, 50) == (None, None)
