"""The AI layer. Nothing here contacts OpenRouter: OpenRouterAI gets a stand-in
client, and conftest blocks the openai client's HTTP layer."""

import logging
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.dependencies import get_ai
from app.services.ai import AIServiceError, ChatReply, FakeAI, OpenRouterAI, RequestPacer, default_ai

KEY = "sk-or-test-key-do-not-leak"
SECRET_TEXT = "Confidential: salary of a named employee"
REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings")


class CountingPacer:
    def __init__(self):
        self.waits = 0

    def wait(self):
        self.waits += 1


def make_ai(responses, dimensions=4, api_key=KEY, models=("chat-model", "fallback-model")):
    """An OpenRouterAI whose client returns (or raises) each response in turn."""
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    ai = OpenRouterAI(
        api_key=SecretStr(api_key) if api_key else None,
        base_url="https://openrouter.ai/api/v1",
        chat_models=list(models),
        embedding_model="nvidia/nemotron-3-embed-1b:free",
        dimensions=dimensions,
        timeout_seconds=45,
        max_tokens=1000,
        pacer=CountingPacer(),
    )
    ai._client = SimpleNamespace(
        embeddings=SimpleNamespace(create=respond),
        chat=SimpleNamespace(completions=SimpleNamespace(create=respond)),
    )
    return ai, calls


def embeddings(*vectors):
    # Returned out of order on purpose: the client sorts them by index
    data = [SimpleNamespace(index=i, embedding=v) for i, v in enumerate(vectors)]
    return SimpleNamespace(data=list(reversed(data)))


def chat(reply, model="chat-model", finish_reason="stop", usage=None):
    return SimpleNamespace(
        model=model, usage=usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content=reply), finish_reason=finish_reason)],
    )


def rate_limited():
    return openai.RateLimitError(
        f"rate limited while sending {SECRET_TEXT}",
        response=httpx.Response(429, request=REQUEST),
        body={"error": SECRET_TEXT},
    )


# --- Pacing --------------------------------------------------------------------------------

def test_pacer_spaces_requests_to_the_rate_limit():
    sleeps = []
    pacer = RequestPacer(15, clock=lambda: 100.0, sleep=sleeps.append)

    for _ in range(3):
        pacer.wait()

    assert sleeps == [4.0, 8.0]  # 15 a minute: one every 4 seconds


def test_pacer_does_not_wait_once_the_interval_has_passed():
    now = [0.0]
    sleeps = []
    pacer = RequestPacer(15, clock=lambda: now[0], sleep=sleeps.append)

    pacer.wait()
    now[0] = 10.0
    pacer.wait()

    assert sleeps == []


# --- Embeddings ------------------------------------------------------------------------------

def test_embedding_adds_the_nemotron_prefix_and_keeps_the_order():
    ai, calls = make_ai([embeddings([1, 0, 0, 0], [0, 1, 0, 0])])

    vectors = ai.embed_batch(["first", "second"], "document")

    assert vectors == [[1, 0, 0, 0], [0, 1, 0, 0]]
    assert calls == [{"model": "nvidia/nemotron-3-embed-1b:free", "input": ["passage: first", "passage: second"]}]
    assert ai.pacer.waits == 1


def test_query_embedding_uses_the_query_prefix():
    ai, calls = make_ai([embeddings([1, 0, 0, 0])])

    ai.embed_batch(["How much leave?"], "query")

    assert calls[0]["input"] == ["query: How much leave?"]


def test_a_failed_request_gets_one_more_attempt():
    ai, calls = make_ai([rate_limited(), embeddings([1, 0, 0, 0])])

    assert ai.embed_batch(["text"], "document") == [[1, 0, 0, 0]]
    assert len(calls) == 2
    assert ai.pacer.waits == 2  # the second attempt is paced too


def test_at_most_two_attempts_then_a_safe_error(caplog):
    ai, calls = make_ai([rate_limited(), rate_limited(), embeddings([1, 0, 0, 0])])

    with caplog.at_level(logging.INFO), pytest.raises(AIServiceError) as error:
        ai.embed_batch([SECRET_TEXT], "document")

    assert len(calls) == 2  # the third response was never asked for
    assert str(error.value) == "provider returned HTTP 429"
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert "HTTP 429" in caplog.text
    assert SECRET_TEXT not in caplog.text and KEY not in caplog.text


def test_timeouts_become_a_safe_error():
    ai, calls = make_ai([openai.APITimeoutError(request=REQUEST), openai.APITimeoutError(request=REQUEST)])

    with pytest.raises(AIServiceError, match=r"request failed \(APITimeoutError\)"):
        ai.embed_batch(["text"], "document")
    assert len(calls) == 2


def test_wrong_embedding_size_is_not_retried():
    ai, calls = make_ai([embeddings([1, 0, 0]), embeddings([1, 0, 0, 0])])

    with pytest.raises(AIServiceError, match="wrong embedding size") as error:
        ai.embed_batch(["text"], "document")
    assert error.value.retryable is False
    assert len(calls) == 1


def test_missing_vectors_are_an_error():
    ai, _ = make_ai([embeddings([1, 0, 0, 0]), embeddings([1, 0, 0, 0])])

    with pytest.raises(AIServiceError, match="missing embeddings"):
        ai.embed_batch(["one", "two"], "document")


def test_missing_key_fails_without_any_request():
    ai, calls = make_ai([embeddings([1, 0, 0, 0])], api_key=None)

    with pytest.raises(AIServiceError, match="OPENROUTER_API_KEY is not set") as error:
        ai.embed_batch(["text"], "document")
    assert error.value.retryable is False
    assert calls == [] and ai.pacer.waits == 0


def test_no_texts_means_no_request():
    ai, calls = make_ai([])

    assert ai.embed_batch([], "document") == []
    assert calls == []


# --- Chat ---------------------------------------------------------------------------------------

def test_reply_sends_the_fallback_models_and_strips_the_reply():
    ai, calls = make_ai([chat("  Ready.  ")])

    reply = ai.generate_reply("System prompt", [{"role": "user", "content": "Hi"}])

    assert reply == "Ready."
    assert calls[0]["model"] == "chat-model"
    assert calls[0]["messages"][0] == {"role": "system", "content": "System prompt"}
    assert calls[0]["extra_body"] == {"models": ["chat-model", "fallback-model"]}
    assert calls[0]["max_tokens"] == 1000


@pytest.mark.parametrize("bad", ["", "   ", "User Safety: safe", "unsafe"])
def test_empty_replies_and_classifier_labels_are_errors(bad):
    ai, calls = make_ai([chat(bad), chat(bad)])

    with pytest.raises(AIServiceError):
        ai.generate_reply("System", [{"role": "user", "content": "Hi"}])
    assert len(calls) == 2


def test_each_request_can_set_its_own_output_limit():
    ai, calls = make_ai([chat("Ready"), chat("Ready")])

    ai.generate("System", [], max_tokens=4000)
    ai.generate("System", [])

    assert [call["max_tokens"] for call in calls] == [4000, 1000]  # AI_MAX_TOKENS by default


@pytest.mark.parametrize("text", ["Staff get 25 days [S1].\nThey can carry over fi", "", None])
def test_a_reply_cut_off_at_the_limit_is_returned_once_marked_truncated(text):
    # Empty included: a reasoning model can spend the whole limit thinking.
    # Another attempt with the same limit would be cut off again.
    ai, calls = make_ai([chat(text, finish_reason="length"), chat("unused")])

    reply = ai.generate("System", [{"role": "user", "content": "Hi"}], max_tokens=50)

    assert reply.truncated and reply.text == (text or "").strip()
    assert len(calls) == 1


def test_generate_reply_refuses_a_cut_off_reply():
    ai, calls = make_ai([chat("Half a sent", finish_reason="length")])

    with pytest.raises(AIServiceError, match="cut off") as error:
        ai.generate_reply("System", [])
    assert error.value.retryable is False and len(calls) == 1


def test_an_empty_reply_that_was_not_cut_off_is_still_retried_then_an_error():
    ai, calls = make_ai([chat("", finish_reason="stop"), chat("", finish_reason="stop")])

    with pytest.raises(AIServiceError, match="empty reply"):
        ai.generate("System", [])
    assert len(calls) == 2


def test_reply_log_has_finish_reason_length_and_tokens_but_never_the_text(caplog):
    usage = SimpleNamespace(completion_tokens=50, completion_tokens_details=SimpleNamespace(reasoning_tokens=42))
    ai, _ = make_ai([chat(SECRET_TEXT, finish_reason="length", usage=usage)])

    with caplog.at_level(logging.INFO, logger="app.services.ai"):
        ai.generate("System", [], max_tokens=50)

    assert "finish_reason=length" in caplog.text
    assert f"{len(SECRET_TEXT)} characters" in caplog.text
    assert "completion_tokens=50 (reasoning 42)" in caplog.text
    assert SECRET_TEXT not in caplog.text


def test_fake_ai_can_give_a_cut_off_reply():
    fake = FakeAI()
    fake.replies = [ChatReply("Half", "length")]

    assert fake.generate("System", [], max_tokens=10).truncated
    assert fake.reply_calls[0]["max_tokens"] == 10
    assert fake.generate("System", []).text == fake.reply


def test_one_model_means_no_fallback_list():
    ai, calls = make_ai([chat("Ready")], models=("only-model",))

    ai.generate_reply("System", [])

    assert "extra_body" not in calls[0]


# --- Safety ------------------------------------------------------------------------------------

def test_repr_hides_the_key():
    ai, _ = make_ai([])

    assert KEY not in repr(ai)


def test_a_real_request_in_a_test_is_stopped_before_the_network():
    ai = OpenRouterAI(
        api_key=SecretStr(KEY), base_url="https://openrouter.ai/api/v1", chat_models=["m"],
        embedding_model="e", dimensions=4, timeout_seconds=1, max_tokens=10, pacer=CountingPacer(),
    )

    with pytest.raises(RuntimeError, match="tried to reach OpenRouter"):
        ai.embed_batch(["text"], "document")


def test_get_ai_gives_one_lazy_openrouter_ai():
    ai = get_ai()

    assert isinstance(ai, OpenRouterAI)
    assert ai is default_ai()  # one instance, so one pacer for the whole process
    assert ai._client is None  # the app starts without contacting OpenRouter


def test_fake_ai_gives_similar_texts_similar_vectors():
    fake = FakeAI(dimensions=64)

    a, b, c = (fake.vector_for(t) for t in ("annual leave days", "annual leave", "parking permits"))

    def overlap(x, y):
        return sum(p * q for p, q in zip(x, y))

    assert overlap(a, b) > overlap(a, c)


# --- Settings ----------------------------------------------------------------------------------

def make_settings(**changes):
    values = dict(
        database_url="postgresql://u:p@h/db", jwt_secret="x", r2_account_id="a", r2_access_key_id="b",
        r2_secret_access_key="c", r2_bucket="d",
    )
    return Settings(_env_file=None, **{**values, **changes})


@pytest.fixture
def clean_env(monkeypatch):
    for name in ["OPENROUTER_API_KEY", "AI_BASE_URL", "AI_CHAT_MODEL", "AI_CHAT_FALLBACK_MODELS",
                 "AI_EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "EMBEDDING_BATCH_SIZE", "AI_REQUESTS_PER_MINUTE",
                 "AI_TIMEOUT_SECONDS", "AI_MAX_TOKENS"]:
        monkeypatch.delenv(name, raising=False)


def test_ai_defaults_match_shopassist(clean_env):
    settings = make_settings()

    assert settings.ai_chat_models == ["nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-super-vl:free"]
    assert settings.ai_embedding_model == "nvidia/nemotron-3-embed-1b:free"
    assert settings.embedding_dimensions == 2048
    assert settings.ai_timeout_seconds == 45
    assert (settings.embedding_batch_size, settings.ai_requests_per_minute) == (64, 15)
    assert settings.openrouter_api_key is None


def test_empty_key_counts_as_not_set_and_a_key_is_hidden(clean_env):
    assert make_settings(openrouter_api_key="  ").openrouter_api_key is None
    settings = make_settings(openrouter_api_key=KEY)
    assert KEY not in repr(settings) and KEY not in str(settings.openrouter_api_key)


@pytest.mark.parametrize(
    "changes",
    [
        {"embedding_dimensions": 1024},  # doesn't match the column
        {"ai_base_url": "http://openrouter.ai/api/v1"},  # the key would go over plain HTTP
        {"ai_requests_per_minute": 21},  # over OpenRouter's free limit
        {"ai_requests_per_minute": 0},
        {"embedding_batch_size": 0},
    ],
)
def test_impossible_ai_settings_refuse_to_start(clean_env, changes):
    with pytest.raises(ValidationError):
        make_settings(**changes)
