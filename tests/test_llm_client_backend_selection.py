"""
Backend selection precedence for proof_first/llm_client.py:

    TENSORMUX_BASE_URL set  -> TensorMux (via the `openai` client)
    else GOOGLE_API_KEY set -> Gemini (existing path)
    else                    -> MOCK_MODE (existing path)

These tests reload the module under different monkeypatched env vars so
the module-level `MOCK_MODE` constant (computed at import time) is
recomputed for each case, and mock out the actual network-touching
clients (`openai.OpenAI`, `google.generativeai`) so nothing real-world is
called.
"""
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import proof_first.llm_client as llm_client_module


def _reload_with_env(monkeypatch, **env):
    """Clears the three relevant env vars, sets only the given ones, and
    reloads llm_client so MOCK_MODE/backend selection is recomputed."""
    for key in ("TENSORMUX_BASE_URL", "TENSORMUX_API_KEY", "TENSORMUX_MODEL", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(llm_client_module)


def test_no_keys_set_is_mock_mode(monkeypatch):
    mod = _reload_with_env(monkeypatch)
    assert mod.is_mock() is True
    client = mod.LLMClient()
    assert client.mock is True
    assert "MOCK_MODE" in client.complete("hello")


def test_google_api_key_alone_selects_gemini(monkeypatch):
    mod = _reload_with_env(monkeypatch, GOOGLE_API_KEY="real-key")
    assert mod.is_mock() is False

    fake_genai = MagicMock()
    fake_model = MagicMock()
    fake_model.generate_content.return_value = MagicMock(text="gemini says hi")
    fake_genai.GenerativeModel.return_value = fake_model

    with patch.dict(sys.modules, {"google.generativeai": fake_genai}):
        client = mod.LLMClient()
        assert client.mock is False
        assert client._backend == "gemini"
        assert client.complete("hello") == "gemini says hi"
    # Restore real module state for other tests in the process.
    _reload_with_env(monkeypatch)


def test_tensormux_base_url_takes_precedence_over_google_api_key(monkeypatch):
    """Even with GOOGLE_API_KEY also set, TENSORMUX_BASE_URL must win."""
    mod = _reload_with_env(
        monkeypatch,
        TENSORMUX_BASE_URL="http://localhost:9999/v1",
        GOOGLE_API_KEY="real-key",
    )
    assert mod.is_mock() is False

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="tensormux says hi"))]
    fake_openai_client = MagicMock()
    fake_openai_client.chat.completions.create.return_value = fake_response

    with patch("openai.OpenAI", return_value=fake_openai_client) as mock_openai_ctor:
        client = mod.LLMClient()
        assert client.mock is False
        assert client._backend == "tensormux"
        result = client.complete("hello", system="sys")
        assert result == "tensormux says hi"

    # Constructed against the custom base_url, not the default OpenAI one.
    mock_openai_ctor.assert_called_once()
    _, kwargs = mock_openai_ctor.call_args
    assert kwargs["base_url"] == "http://localhost:9999/v1"
    # Default placeholder api_key/model used since neither was explicitly set.
    assert kwargs["api_key"]  # non-empty placeholder, not a real network call
    assert client._model_name == "qwen2.5:0.5b"

    # Restore real module state for other tests in the process.
    _reload_with_env(monkeypatch)


def test_tensormux_reads_custom_api_key_and_model(monkeypatch):
    mod = _reload_with_env(
        monkeypatch,
        TENSORMUX_BASE_URL="http://localhost:9999/v1",
        TENSORMUX_API_KEY="tmx-secret",
        TENSORMUX_MODEL="llama3:8b",
    )

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="ok"))]
    fake_openai_client = MagicMock()
    fake_openai_client.chat.completions.create.return_value = fake_response

    with patch("openai.OpenAI", return_value=fake_openai_client) as mock_openai_ctor:
        client = mod.LLMClient()
        client.complete("hello")

    _, kwargs = mock_openai_ctor.call_args
    assert kwargs["api_key"] == "tmx-secret"
    assert client._model_name == "llama3:8b"

    # Restore real module state for other tests in the process.
    _reload_with_env(monkeypatch)
