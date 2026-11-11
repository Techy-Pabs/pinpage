"""The only module that talks to an AI provider. Adapted from ShopAssist.

One interface, two versions, like storage:
- OpenRouterAI talks to OpenRouter through the openai client.
- FakeAI returns fixed embeddings and replies, for tests.

Callers only ever see AIServiceError, whose messages are written by us:
never a provider's error text, a key or the text that was sent.

Every request is paced (at most AI_REQUESTS_PER_MINUTE across the whole
process) and gets at most 2 attempts in total, so a busy free model can't
use up the daily allowance on retries.
"""

import hashlib
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, TypeVar

import openai
from openai import OpenAI
from pydantic import SecretStr

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)

# A message in a conversation: {"role": "user" | "assistant", "content": "..."}
ChatMessage = dict[str, str]

# "document": text being stored. "query": a question being asked.
EmbeddingMode = Literal["document", "query"]

MAX_ATTEMPTS = 2

# Safety classifier models answer with a label such as "User Safety: safe"
# instead of a reply. A router can pick one by mistake.
_CLASSIFIER_LABEL = re.compile(
    r"^\W*(?:(?:user|response|prompt|content)\s+)?safety\s*:\W*(?:safe|unsafe)\b|^\W*(?:safe|unsafe)\W*$",
    re.IGNORECASE,
)

# nemotron-3-embed-1b was trained with these prefixes (see its model card).
# Through an OpenAI-compatible endpoint they must be added to the text.
_NEMOTRON_PREFIXES: dict[EmbeddingMode, str] = {"query": "query: ", "document": "passage: "}

T = TypeVar("T")


# finish_reason when the reply hit max_tokens before the model finished
FINISH_LENGTH = "length"


@dataclass
class ChatReply:
    """The chat model's reply and why it stopped. finish_reason "length" means
    max_tokens ran out, so the text may end mid sentence (or be empty: a
    reasoning model can spend the whole allowance thinking)."""

    text: str
    finish_reason: str | None = "stop"

    @property
    def truncated(self) -> bool:
        return self.finish_reason == FINISH_LENGTH


def looks_like_classifier_label(reply: str) -> bool:
    """True for a safety classifier's verdict ("User Safety: safe"), not a reply."""
    return bool(_CLASSIFIER_LABEL.search(reply.strip()))


class AIServiceError(Exception):
    """The AI couldn't produce a result (timeout, rate limit, provider error,
    empty result). The message is ours and safe to log.

    retryable=False for problems a second attempt can't fix, such as a
    missing key or an embedding of the wrong size.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class RequestPacer:
    """Spaces requests out to at most per_minute a minute, across threads.

    Each caller reserves the next free slot under a lock, then sleeps
    outside it until its slot comes.
    """

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = 60.0 / per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_slot = float("-inf")
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
        if slot > now:
            self._sleep(slot - now)


class AIClient(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, history: list[ChatMessage], max_tokens: int | None = None) -> ChatReply:
        """The chat model's reply, possibly cut off at max_tokens (the
        AI_MAX_TOKENS setting when None). Raises AIServiceError."""

    def generate_reply(self, system_prompt: str, history: list[ChatMessage], max_tokens: int | None = None) -> str:
        """The reply's text, for callers that can't use part of a reply: one
        cut off at max_tokens is an error."""
        reply = self.generate(system_prompt, history, max_tokens)
        if reply.truncated:
            raise AIServiceError("reply cut off at the output limit", retryable=False)
        return reply.text

    @abstractmethod
    def embed_batch(self, texts: list[str], mode: EmbeddingMode) -> list[list[float]]:
        """One embedding per text, in order, in one request. Raises AIServiceError."""


class OpenRouterAI(AIClient):
    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        base_url: str,
        chat_models: list[str],
        embedding_model: str,
        dimensions: int,
        timeout_seconds: float,
        max_tokens: int,
        pacer: RequestPacer,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self.chat_models = chat_models
        self.embedding_model = embedding_model
        self.dimensions = dimensions
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self.pacer = pacer
        self._client: OpenAI | None = None
        self._client_lock = threading.Lock()

    @classmethod
    def from_settings(cls, config: Settings) -> "OpenRouterAI":
        return cls(
            api_key=config.openrouter_api_key,
            base_url=config.ai_base_url,
            chat_models=config.ai_chat_models,
            embedding_model=config.ai_embedding_model,
            dimensions=config.embedding_dimensions,
            timeout_seconds=config.ai_timeout_seconds,
            max_tokens=config.ai_max_tokens,
            pacer=RequestPacer(config.ai_requests_per_minute),
        )

    def __repr__(self) -> str:
        # Never shows the key
        return f"OpenRouterAI(chat_models={self.chat_models!r}, embedding_model={self.embedding_model!r})"

    @property
    def client(self) -> OpenAI:
        """Created on first use, so the app starts without contacting OpenRouter."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = OpenAI(
                        base_url=self._base_url,
                        api_key=self._api_key.get_secret_value(),
                        timeout=self._timeout_seconds,
                        # Our own attempts (2 at most) replace the client's
                        # retries, which would multiply waits and requests
                        max_retries=0,
                    )
        return self._client

    def _attempts(self, operation: str, request: Callable[[], T]) -> T:
        """Runs one request, paced, with at most MAX_ATTEMPTS attempts."""
        if self._api_key is None:
            logger.error("OPENROUTER_API_KEY is not set")
            raise AIServiceError("OPENROUTER_API_KEY is not set", retryable=False)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.pacer.wait()
            try:
                return request()
            except AIServiceError as error:
                if not error.retryable or attempt == MAX_ATTEMPTS:
                    raise
                logger.info("AI %s attempt %d failed (%s); trying once more", operation, attempt, error)
        raise AssertionError("unreachable")

    def _call(self, operation: str, send: Callable[[], T]) -> T:
        """Sends one request, turning every openai error into AIServiceError."""
        try:
            return send()
        except openai.APIStatusError as error:
            # Only the status code: the error body could echo the request
            logger.warning("AI %s: provider returned HTTP %s", operation, error.status_code)
            raise AIServiceError(f"provider returned HTTP {error.status_code}") from None
        except openai.OpenAIError as error:
            # Timeouts, connection failures, etc.
            logger.warning("AI %s: request failed (%s)", operation, type(error).__name__)
            raise AIServiceError(f"request failed ({type(error).__name__})") from None

    # --- Chat ------------------------------------------------------------------------

    def _reply_once(
        self, system_prompt: str, history: list[ChatMessage], max_tokens: int, models: list[str]
    ) -> ChatReply:
        response = self._call("reply", lambda: self.client.chat.completions.create(
            model=models[0],
            messages=[{"role": "system", "content": system_prompt}, *history],
            max_tokens=max_tokens,
            # OpenRouter's fallback list: if the first model can't answer
            # (for example its provider is down), the next one is tried
            **({"extra_body": {"models": models}} if len(models) > 1 else {}),
        ))
        choice = response.choices[0] if response.choices else None
        reply = (choice.message.content if choice else None) or ""
        finish_reason = getattr(choice, "finish_reason", None)
        # Counts only, never the text
        logger.info(
            "AI reply from model=%s: finish_reason=%s, %d characters, %s (max_tokens=%d)",
            response.model, finish_reason, len(reply.strip()), _token_counts(response), max_tokens,
        )
        if finish_reason == FINISH_LENGTH:
            # Another attempt with the same limit would be cut off again;
            # the caller decides what part of the reply to keep
            logger.warning("AI reply was cut off at max_tokens=%d (model=%s)", max_tokens, response.model)
            if looks_like_classifier_label(reply):
                raise AIServiceError("classifier label instead of a reply")
            return ChatReply(reply.strip(), finish_reason)
        if not reply.strip():
            logger.warning("AI reply was empty (model=%s)", response.model)
            raise AIServiceError("empty reply")
        if looks_like_classifier_label(reply):
            # Never show a classifier's verdict as if it were an answer
            logger.warning("AI reply was a safety classifier label (model=%s)", response.model)
            raise AIServiceError("classifier label instead of a reply")
        return ChatReply(reply.strip(), finish_reason)

    def generate(self, system_prompt: str, history: list[ChatMessage], max_tokens: int | None = None) -> ChatReply:
        """The first attempt asks the primary model alone, so if it fails we
        know why and log it; the second asks the fallback models (OpenRouter
        tries them in order), or the primary again if there are none."""
        limit = max_tokens or self._max_tokens
        primary, fallbacks = self.chat_models[0], self.chat_models[1:]
        attempts = 0

        def request() -> ChatReply:
            nonlocal attempts
            attempts += 1
            first = attempts == 1
            try:
                return self._reply_once(
                    system_prompt, history, limit, [primary] if first or not fallbacks else fallbacks
                )
            except AIServiceError as error:
                if first and fallbacks:
                    # Our own message: a status code or an error type, never text
                    logger.warning("AI primary chat model %s failed (%s); using the fallback models", primary, error)
                raise

        return self._attempts("reply", request)

    # --- Embeddings --------------------------------------------------------------------

    def _embed_once(self, texts: list[str], mode: EmbeddingMode) -> list[list[float]]:
        prefix = _NEMOTRON_PREFIXES[mode] if "nemotron" in self.embedding_model else ""
        response = self._call("embedding", lambda: self.client.embeddings.create(
            model=self.embedding_model,
            input=[prefix + text for text in texts],
        ))
        # Results carry their position; sort so they line up with the texts
        vectors = [item.embedding for item in sorted(response.data or [], key=lambda d: d.index)]
        if len(vectors) != len(texts) or any(not v for v in vectors):
            logger.warning("AI embedding: got %d vectors for %d texts", len(vectors), len(texts))
            raise AIServiceError("missing embeddings")
        if any(len(v) != self.dimensions for v in vectors):
            logger.error("AI embedding size %d does not match EMBEDDING_DIMENSIONS=%d", len(vectors[0]), self.dimensions)
            raise AIServiceError("wrong embedding size", retryable=False)
        return vectors

    def embed_batch(self, texts: list[str], mode: EmbeddingMode) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._attempts("embedding", lambda: self._embed_once(texts, mode))
        logger.info("Embedded %d texts (%s) with model=%s", len(texts), mode, self.embedding_model)
        return vectors


def _token_counts(response) -> str:
    """"completion_tokens=900 (reasoning 850)" from the response's usage, if any."""
    usage = getattr(response, "usage", None)
    completion = getattr(usage, "completion_tokens", None)
    if completion is None:
        return "completion_tokens=unknown"
    reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    return f"completion_tokens={completion}" + (f" (reasoning {reasoning})" if reasoning is not None else "")


class FakeAI(AIClient):
    """For tests: fixed replies, and embeddings worked out from the words in
    each text, so similar texts get similar vectors. Records every call.

    fail_on = {"reply"} or {"embed"} makes those calls fail like an outage;
    fail_after_batches = n lets n embedding requests succeed first;
    vectors = {text: vector} fixes the embedding of chosen texts;
    replies = [...] answers the next chat requests in turn (an exception in
    the list is raised instead, and a ChatReply such as
    ChatReply("...", "length") stands for a reply cut off at max_tokens),
    then reply answers the rest.
    """

    def __init__(self, dimensions: int = 2048):
        self.dimensions = dimensions
        self.reply = "This is a fake AI reply."
        self.fail_on: set[str] = set()
        self.fail_after_batches: int | None = None
        self.reply_calls: list[dict] = []
        self.embed_calls: list[dict] = []
        self.vectors: dict[str, list[float]] = {}
        self.replies: list[str | ChatReply | Exception] = []

    def generate(self, system_prompt: str, history: list[ChatMessage], max_tokens: int | None = None) -> ChatReply:
        self.reply_calls.append({"system_prompt": system_prompt, "history": history, "max_tokens": max_tokens})
        if "reply" in self.fail_on:
            raise AIServiceError("simulated outage")
        reply = self.replies.pop(0) if self.replies else self.reply
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, ChatReply) else ChatReply(reply)

    def embed_batch(self, texts: list[str], mode: EmbeddingMode) -> list[list[float]]:
        if "embed" in self.fail_on or (
            self.fail_after_batches is not None and len(self.embed_calls) >= self.fail_after_batches
        ):
            self.embed_calls.append({"texts": list(texts), "mode": mode, "failed": True})
            raise AIServiceError("simulated outage")
        self.embed_calls.append({"texts": list(texts), "mode": mode, "failed": False})
        return [self.vector_for(text) for text in texts]

    def vector_for(self, text: str) -> list[float]:
        if text in self.vectors:
            return self.vectors[text]
        values = [0.0] * self.dimensions
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            values[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dimensions] += 1.0
        if not any(values):
            values[0] = 1.0
        return values


@lru_cache(maxsize=1)
def default_ai() -> OpenRouterAI:
    """The app's one OpenRouterAI, built from settings on first use. One
    instance means one pacer for the whole process."""
    return OpenRouterAI.from_settings(settings)
