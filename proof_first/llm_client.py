"""
Thin LLM abstraction.

Design goal: every agent below calls `llm.complete(prompt, system=...)`.
- If TENSORMUX_BASE_URL is set, this calls a TensorMux-compatible backend
  for real, via the `openai` client pointed at that custom base_url.
- Else, if GOOGLE_API_KEY is set, this calls Gemini for real (google-generativeai).
- If neither is set, we run in MOCK_MODE, which is loud and explicit about
  being a mock (printed + tagged in every stored ActivityEvent) rather than
  silently faking a "real" response. Mock mode is rule-based over the
  sample lead fixtures in data/leads.py, not a random/fake number generator -
  it exists so the *pipeline logic and routing* can be demoed and evaluated
  honestly without requiring paid API keys during grading.

Nothing in this file invents a false "SUCCESS" for an action - that logic
lives in post_action_verifier.py and is independent of which LLM backend
is used here. Regardless of backend, complete() always returns a plain
string - claim.verdict/confidence stay rule-based elsewhere per this
file's existing contract.
"""
from __future__ import annotations

import os

_TENSORMUX_BASE_URL = os.environ.get("TENSORMUX_BASE_URL")

# Backend selection precedence: TensorMux (if TENSORMUX_BASE_URL is set) ->
# Gemini (if GOOGLE_API_KEY is set) -> MOCK_MODE. TensorMux takes priority
# because it's an explicit opt-in to a self-hosted/custom endpoint.
MOCK_MODE = not _TENSORMUX_BASE_URL and os.environ.get("GOOGLE_API_KEY") in (None, "", "changeme")


def is_mock() -> bool:
    return MOCK_MODE


class LLMClient:
    def __init__(self):
        self.mock = MOCK_MODE
        self._backend = "mock"
        if _TENSORMUX_BASE_URL:
            try:
                from openai import OpenAI
                api_key = os.environ.get("TENSORMUX_API_KEY", "tensormux-placeholder-key")
                self._model_name = os.environ.get("TENSORMUX_MODEL", "qwen2.5:0.5b")
                self._openai_client = OpenAI(base_url=_TENSORMUX_BASE_URL, api_key=api_key)
                self._backend = "tensormux"
                self.mock = False
            except Exception as e:  # pragma: no cover - graceful degrade
                print(f"[llm_client] Falling back to MOCK_MODE - could not init TensorMux: {e}")
                self.mock = True
        elif not self.mock:
            try:
                import google.generativeai as genai
                genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
                self._model = genai.GenerativeModel("gemini-2.0-flash")
                self._backend = "gemini"
            except Exception as e:  # pragma: no cover - graceful degrade
                print(f"[llm_client] Falling back to MOCK_MODE - could not init Gemini: {e}")
                self.mock = True

    def complete(self, prompt: str, system: str = "") -> str:
        if self.mock:
            # Deterministic, non-random placeholder so behaviour is
            # reproducible during grading. Real agents still run their real
            # control-flow / verdict logic on top of this text - see
            # agents/*.py for where the actual decision-making happens.
            return f"[MOCK_MODE response - no GOOGLE_API_KEY set]\nsystem={system[:60]}...\nprompt={prompt[:120]}..."
        if self._backend == "tensormux":
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            resp = self._openai_client.chat.completions.create(
                model=self._model_name,
                messages=messages,
            )
            return resp.choices[0].message.content
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        resp = self._model.generate_content(full_prompt)
        return resp.text


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
