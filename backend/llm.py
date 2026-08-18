"""Provider-agnostic LLM layer.

The application is not tied to any one vendor. This module exposes a single
`complete()` call and adapts it to whichever provider is configured:

    anthropic   Claude          (paid credits)
    gemini      Google AI Studio (free tier, no card)
    groq        Groq            (free tier, very fast open models)
    openrouter  OpenRouter      (free `:free` models)
    ollama      local Ollama    (no key at all, fully offline)

Selection is automatic: whichever credential is present wins, in the priority
order below, unless `LLM_PROVIDER` names one explicitly. If none is configured
the caller falls back to the deterministic rule engine, so the product always
works.

Only the standard library is used — adding a provider costs no dependency.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("llm")

DEFAULT_TIMEOUT = 60

#: Checked in order; the first provider with a usable credential is selected.
PRIORITY = ("anthropic", "gemini", "groq", "openrouter", "ollama")

#: Model ids move fast. Every one of these is overridable by env var, and an
#: unknown id surfaces as a provider error and falls back to the rule engine
#: rather than breaking the app.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "gemini": "gemini-3.6-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "ollama": "llama3.1",
}


class LLMError(RuntimeError):
    """Raised inside an adapter; always caught and converted to a fallback."""


# ------------------------------------------------------------------ utilities
def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"connection failed: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise LLMError(f"request failed: {exc}") from exc


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Optional[str]:
    """Pull a JSON object out of a model response.

    Models that lack native structured output wrap JSON in prose or markdown
    fences. This recovers the object rather than discarding a good answer over
    formatting.
    """
    if not text:
        return None
    candidate = text.strip()

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    if candidate.startswith("{") and candidate.endswith("}"):
        return candidate

    # Fall back to the first balanced {...} span.
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return candidate[start : index + 1]
    return None


def _schema_instruction(schema: dict[str, Any]) -> str:
    """Prompt-level structured output, for providers without a native mode."""
    return (
        "\n\nReturn ONLY a single JSON object — no prose, no markdown fences — "
        "conforming exactly to this JSON Schema:\n"
        f"{json.dumps(schema, indent=2)}"
    )


# ------------------------------------------------------------------ providers
@dataclass
class Provider:
    """Base adapter. Subclasses implement `_complete`."""

    name: str
    model: str
    api_key: Optional[str] = None
    base_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        schema: dict[str, Any] | None = None,
    ) -> Optional[str]:
        """Return response text, or None if anything at all went wrong."""
        try:
            text = self._complete(system, messages, max_tokens, schema)
        except LLMError as exc:
            log.warning("%s provider failed: %s", self.name, exc)
            return None
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("%s provider raised unexpectedly: %s", self.name, exc)
            return None
        return (text or "").strip() or None

    def _complete(self, system, messages, max_tokens, schema) -> Optional[str]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}


class AnthropicProvider(Provider):
    """Claude via the official SDK, with native structured outputs."""

    def _complete(self, system, messages, max_tokens, schema):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("anthropic package not installed") from exc

        client = anthropic.Anthropic(api_key=self.api_key)
        output_config: dict[str, Any] = {"effort": self.extra.get("effort", "medium")}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}

        request = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": output_config,
        }
        try:
            # Server-side refusal fallbacks, opted into by default.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **request,
            )
        except Exception as exc:
            log.info("beta path unavailable (%s); using standard call", exc)
            try:
                response = client.messages.create(**request)
            except Exception as inner:
                raise LLMError(str(inner)) from inner

        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMError("model declined the request")
        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )


class GeminiProvider(Provider):
    """Google AI Studio. Free tier, no credit card."""

    def _complete(self, system, messages, max_tokens, schema):
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
        ]
        instruction = system + (_schema_instruction(schema) if schema else "")
        generation: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if schema is not None:
            generation["responseMimeType"] = "application/json"

        url = (
            f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        )
        data = _post_json(
            url,
            {
                "systemInstruction": {"parts": [{"text": instruction}]},
                "contents": contents,
                "generationConfig": generation,
            },
            {"x-goog-api-key": self.api_key or ""},
        )

        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(f"no candidates returned (blockReason={blocked})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(part.get("text", "") for part in parts)


class OpenAICompatProvider(Provider):
    """Anything speaking the OpenAI chat-completions wire format.

    Covers Groq, OpenRouter, Together, vLLM, LM Studio and OpenAI itself — one
    adapter, many providers.
    """

    def _complete(self, system, messages, max_tokens, schema):
        instruction = system + (_schema_instruction(schema) if schema else "")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": instruction}, *messages],
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key or ''}"}
        headers.update(self.extra.get("headers", {}))

        data = _post_json(f"{self.base_url}/chat/completions", payload, headers)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"no choices returned: {str(data)[:200]}")
        return (choices[0].get("message") or {}).get("content") or ""


class OllamaProvider(Provider):
    """A local Ollama server. No API key, no network, no cost."""

    def _complete(self, system, messages, max_tokens, schema):
        instruction = system + (_schema_instruction(schema) if schema else "")
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": instruction}, *messages],
            "options": {"num_predict": max_tokens},
        }
        if schema is not None:
            payload["format"] = "json"

        data = _post_json(f"{self.base_url}/api/chat", payload, {})
        return (data.get("message") or {}).get("content") or ""


# ------------------------------------------------------------------ selection
def _env(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def _build(name: str) -> Optional[Provider]:
    """Construct a provider if its credential is present."""
    model_override = _env(f"{name.upper()}_MODEL")
    model = model_override or DEFAULT_MODELS[name]

    if name == "anthropic":
        key = _env("ANTHROPIC_API_KEY")
        if not key:
            return None
        return AnthropicProvider(name=name, model=model, api_key=key)

    if name == "gemini":
        key = _env("GEMINI_API_KEY", "GOOGLE_API_KEY")
        if not key:
            return None
        return GeminiProvider(
            name=name,
            model=model,
            api_key=key,
            base_url=_env("GEMINI_BASE_URL")
            or "https://generativelanguage.googleapis.com",
        )

    if name == "groq":
        key = _env("GROQ_API_KEY")
        if not key:
            return None
        return OpenAICompatProvider(
            name=name,
            model=model,
            api_key=key,
            base_url=_env("GROQ_BASE_URL") or "https://api.groq.com/openai/v1",
        )

    if name == "openrouter":
        key = _env("OPENROUTER_API_KEY")
        if not key:
            return None
        return OpenAICompatProvider(
            name=name,
            model=model,
            api_key=key,
            base_url=_env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
            extra={
                "headers": {
                    "HTTP-Referer": "http://localhost",
                    "X-Title": "Learning Path Recommender",
                }
            },
        )

    if name == "ollama":
        # Ollama needs no key, so it is only auto-selected when explicitly
        # pointed at — otherwise every machine would appear to have an LLM.
        host = _env("OLLAMA_HOST")
        if not host and _env("LLM_PROVIDER") != "ollama":
            return None
        return OllamaProvider(
            name=name, model=model, base_url=host or "http://localhost:11434"
        )

    return None


def resolve_provider() -> Optional[Provider]:
    """The active provider, or None to use the rule engine."""
    if _env("DISABLE_LLM", "") in ("1", "true", "yes", "TRUE", "True"):
        return None

    requested = (_env("LLM_PROVIDER") or "auto").lower()
    if requested in ("none", "off", "rules"):
        return None

    if requested != "auto":
        if requested not in DEFAULT_MODELS:
            log.warning("unknown LLM_PROVIDER '%s'; using the rule engine", requested)
            return None
        provider = _build(requested)
        if provider is None:
            log.warning(
                "LLM_PROVIDER=%s but no credential found; using the rule engine",
                requested,
            )
        return provider

    for name in PRIORITY:
        provider = _build(name)
        if provider is not None:
            return provider
    return None


def complete(
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    schema: dict[str, Any] | None = None,
) -> tuple[Optional[str], Optional[str]]:
    """Run a completion. Returns (text, provider_name); either may be None."""
    provider = resolve_provider()
    if provider is None:
        return None, None
    return provider.complete(system, messages, max_tokens, schema), provider.name


def status() -> dict[str, Any]:
    """What the health endpoint and the UI badge report."""
    provider = resolve_provider()
    configured = [name for name in PRIORITY if _build(name) is not None]
    if provider is None:
        return {
            "enabled": False,
            "provider": None,
            "model": None,
            "mode": "rule-engine",
            "configured_providers": configured,
        }
    return {
        "enabled": True,
        "provider": provider.name,
        "model": provider.model,
        "mode": provider.name,
        "configured_providers": configured,
    }
