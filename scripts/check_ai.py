"""Manual check that the OpenRouter settings work, with 2 real requests.

Run it by hand (see the README), never in the automated tests:

    docker compose run --rm --no-deps --build api python scripts/check_ai.py

It embeds one sentence (checking the vector has EMBEDDING_DIMENSIONS
numbers) and asks the chat model for a one word reply. It prints PASS or
FAIL for each step and never prints the key. Each step makes one request,
with no second attempt, so the check costs exactly 2 requests from the free
daily allowance (shared with everything else on your OpenRouter account).
"""

import sys
from pathlib import Path

# Lets "python scripts/check_ai.py" import the app package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services import ai as ai_module  # noqa: E402
from app.services.ai import AIServiceError, OpenRouterAI  # noqa: E402

SENTENCE = "Staff may carry over five days of annual leave into the next year."
PROMPT = "You are a connection test. Reply with exactly one word: ready"


def describe(error: Exception) -> str:
    """A safe one-line reason: our own AIServiceError message, or just the
    error's type. Never raw provider text."""
    if isinstance(error, AIServiceError):
        return str(error)
    return type(error).__name__


def main() -> int:
    if settings.openrouter_api_key is None:
        print("FAIL  OPENROUTER_API_KEY is not set in .env")
        print("AI check FAILED.")
        return 1

    # One attempt per request: a check should spend exactly 2 requests
    ai_module.MAX_ATTEMPTS = 1
    ai = OpenRouterAI.from_settings(settings)
    results: list[bool] = []

    def step(name: str, check) -> None:
        try:
            ok, reason = check()
        except Exception as error:  # report every failure as FAIL, never a traceback
            ok, reason = False, describe(error)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f" ({reason})"))
        results.append(ok)

    print(f"Embedding model: {ai.embedding_model}")
    print(f"Chat models: {', '.join(ai.chat_models)}")

    def embed():
        [vector] = ai.embed_batch([SENTENCE], "document")
        return len(vector) == settings.embedding_dimensions, f"got {len(vector)} numbers"

    step(f"embed one sentence ({settings.embedding_dimensions} numbers)", embed)

    def reply():
        answer = ai.generate_reply(PROMPT, [{"role": "user", "content": "Are you there?"}])
        return bool(answer.strip()), "empty reply"

    step("get a reply from the chat model", reply)

    passed = all(results) and len(results) == 2
    print("AI check passed." if passed else "AI check FAILED.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
