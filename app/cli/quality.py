"""The quality check: scored questions about the demo documents.

Usage (through the quality service, which mounts quality/ and docs/demo/):
    docker compose run --rm quality run --set lantern-hill
    docker compose run --rm quality run --set lantern-hill --only annual-new,car-loan
    docker compose run --rm quality run --set lantern-hill --retrieval-only
    docker compose run --rm quality run --set lantern-hill --chat-model MODEL
    docker compose run --rm quality models

It uses the real AI and real storage, so it never runs in CI or in the
tests. Before any request it prints how many it will use and asks for
confirmation (or --yes). The demo documents are uploaded once, to a
dedicated quality user's workspace, and reused on later runs. All
questions are embedded in one batched request; the answering code is then
called directly (not over HTTP), so no user's daily question limit applies
and nothing is saved as a conversation. If OpenRouter's free daily
allowance runs out, the run stops and keeps what it has.

Results go to quality/results/<timestamp>-<set>.json (not committed). They
hold the answers, so they stay on this machine. The logs never do.

Question embeddings are cached in quality/results/question-embeddings.json
(not committed either), keyed by the embedding model and the question's
text, so a later run (each --chat-model run, for example) spends no
embedding request on questions already embedded.
"""

import argparse
import json
import logging
import math
import secrets
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.cli.users import database_label
from app.core.config import settings
from app.core.security import hash_password
from app.models import Document, User
from app.models.document import RETRYABLE_CODES, STAGE_EXTRACTING, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY
from app.services import documents as docs
from app.services import modes
from app.services.ai import AIClient, AIServiceError, ChatMessage, ChatReply, DailyLimitReached, EmbeddingMode
from app.services.citations import strip_markers
from app.services.processing import ProcessingFailed, extract_pages, prepare, process_document, restart_processing
from app.services.quality import (
    Case,
    CaseOutcome,
    QualityDocument,
    QualitySet,
    QualitySetError,
    cutoff_table,
    format_cutoff_table,
    format_models,
    free_chat_models,
    load_set,
    passed,
    score_case,
    select_cases,
    summary_table,
    uses_retrieval,
)
from app.services.requests import MODE_QUESTION, parse_request
from app.services.retrieval import similar_chunks_query
from app.services.storage import Storage, document_key
from app.services.workspaces import create_personal_workspace, get_personal_workspace

# The project root: /code in the containers
ROOT = Path(__file__).resolve().parents[2]
# The quality user: never logs in (its password is random and thrown away)
QUALITY_EMAIL = "quality-check@pinpage.invalid"
MODELS_URL = "https://openrouter.ai/api/v1/models"

# Exit codes
OK, REFUSED, STOPPED = 0, 1, 2


class RunRefused(Exception):
    """The run can't start or go on; the message says why."""


# --- The AI, recorded ------------------------------------------------------------------------------

class RecordingAI(AIClient):
    """Passes every request to the real AI, recording each chat reply's
    model, finish reason, tokens and time (never its text), and answers
    query embeddings from the batch made at the start."""

    def __init__(self, inner: AIClient):
        self.inner = inner
        self.is_fake = inner.is_fake  # answering refuses fake-embedded documents to a real AI
        self.query_vectors: dict[str, list[float]] = {}
        self.replies: list[dict] = []
        self.chat_requests = 0
        self.embedding_requests = 0

    def generate(self, system_prompt: str, history: list[ChatMessage], max_tokens: int | None = None) -> ChatReply:
        self.chat_requests += 1
        started = time.monotonic()
        reply = self.inner.generate(system_prompt, history, max_tokens)
        self.replies.append({
            "model": reply.model, "finish_reason": reply.finish_reason,
            "completion_tokens": reply.completion_tokens, "reasoning_tokens": reply.reasoning_tokens,
            "max_tokens": max_tokens, "seconds": round(time.monotonic() - started, 2),
        })
        return reply

    def embed_batch(self, texts: list[str], mode: EmbeddingMode) -> list[list[float]]:
        if mode == "query" and texts and all(text in self.query_vectors for text in texts):
            return [self.query_vectors[text] for text in texts]
        self.embedding_requests += 1
        vectors = self.inner.embed_batch(texts, mode)
        if mode == "query":
            self.query_vectors.update(zip(texts, vectors))
        return vectors


# --- The quality user and documents ------------------------------------------------------------------

def quality_user(session_factory: sessionmaker) -> uuid.UUID:
    """The quality user's id, created with a personal workspace if needed."""
    with session_factory() as db:
        user_id = db.scalar(select(User.id).where(User.email == QUALITY_EMAIL))
        if user_id is None:
            user = User(email=QUALITY_EMAIL, password_hash=hash_password(secrets.token_urlsafe(32)))
            db.add(user)
            db.flush()
            create_personal_workspace(db, user)
            db.commit()
            user_id = user.id
        return user_id


def existing_document(session_factory: sessionmaker, user_id: uuid.UUID, data: bytes) -> Document | None:
    with session_factory() as db:
        workspace = get_personal_workspace(db, user_id)
        return docs.find_duplicate(db, workspace.id, docs.sha256_hex(data))


def embedding_requests_for(data: bytes, title: str) -> int:
    """Embedding requests processing this PDF will use: its chunks, worked
    out locally (no AI), in batches."""
    try:
        chunks = prepare(extract_pages(data, settings.max_pages), title).chunks
    except ProcessingFailed as failure:
        raise RunRefused(f"the demo document {title} can't be read ({failure.code})") from None
    return math.ceil(len(chunks) / settings.embedding_batch_size)


def ready_document(
    session_factory: sessionmaker, storage: Storage, ai: AIClient, user_id: uuid.UUID,
    qdoc: QualityDocument, data: bytes,
) -> Document:
    """The document, uploaded and processed if it isn't there yet. Raises RunRefused."""
    document = existing_document(session_factory, user_id, data)
    if document is not None and document.status == STATUS_FAILED and document.error_code in RETRYABLE_CODES:
        with session_factory() as db, db.begin():
            restart_processing(db, db.get(Document, document.id))  # resume, as a retry does
        process_document(document.id, storage, ai, session_factory)
    elif document is None:
        with session_factory() as db:
            workspace = get_personal_workspace(db, user_id)
            document_id = uuid.uuid4()
            key = document_key(workspace.id, document_id)
            db.add(Document(
                id=document_id, workspace_id=workspace.id, uploaded_by=user_id, title=qdoc.title,
                original_filename=Path(qdoc.file).name, storage_key=key, size_bytes=len(data),
                sha256=docs.sha256_hex(data), status=STATUS_PROCESSING, stage=STAGE_EXTRACTING,
            ))
            db.flush()
            storage.save(key, data, "application/pdf")
            db.commit()
        process_document(document_id, storage, ai, session_factory)
    document = existing_document(session_factory, user_id, data)
    if document.status != STATUS_READY:
        raise RunRefused(f"the demo document {qdoc.key} is {document.status} ({document.error_code or 'no code'})")
    return document


# --- The question embedding cache ---------------------------------------------------------------------

EMBEDDING_CACHE = "question-embeddings.json"


def load_embedding_cache(path: Path, model: str, dimensions: int) -> dict[str, list[float]]:
    """{question text: vector} cached for this embedding model. A missing or
    unreadable file is an empty cache; vectors of the wrong size are left out."""
    try:
        cached = json.loads(path.read_text(encoding="utf-8")).get(model, {})
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(cached, dict):
        return {}
    return {
        text: vector for text, vector in cached.items()
        if isinstance(vector, list) and len(vector) == dimensions
    }


def save_embedding_cache(path: Path, model: str, vectors: dict[str, list[float]]) -> None:
    """Adds vectors to this model's part of the cache, keeping other models'."""
    try:
        everything = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(everything, dict):
            everything = {}
    except (OSError, ValueError):
        everything = {}
    if not isinstance(everything.get(model), dict):
        everything[model] = {}
    everything[model].update(vectors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(everything), encoding="utf-8")


def question_texts(cases: list[Case]) -> list[str]:
    """The questions embedded at the start: each retrieval case's first turn."""
    return list(dict.fromkeys(case.turns[0] for case in cases if uses_retrieval(case)))


# --- Planning -------------------------------------------------------------------------------------------

def request_plan(
    cases: list[Case], uploads: int, retrieval_only: bool, cached: set[str] | frozenset[str] = frozenset()
) -> tuple[int, str]:
    """(requests, explanation) for the run, one attempt each. Questions in
    cached are already embedded, so need no request."""
    texts = question_texts(cases)
    missing = [text for text in texts if text not in cached]
    embed = 1 if missing else 0
    parts = [f"{uploads} to embed demo documents"] if uploads else []
    if texts and not missing:
        parts.append("0 to embed the questions (all cached)")
    elif texts:
        reused = len(texts) - len(missing)
        parts.append("1 to embed the questions" + (f" ({reused} of {len(texts)} cached)" if reused else ""))
    if retrieval_only:
        return uploads + embed, ", ".join(parts)
    chats = follow_ups = 0
    for case in cases:
        chats += len(case.turns)
        # Each later turn of a follow up: one rewrite, and one embedding for it
        follow_ups += 2 * (len(case.turns) - 1)
    parts.append(f"{chats} for answers")
    if follow_ups:
        parts.append(f"{follow_ups} for rewriting and embedding follow ups")
    return uploads + embed + chats + follow_ups, ", ".join(parts)


def confirmed(args, requests: int, explanation: str, ask: Callable[[str], str], out: Callable[[str], None]) -> bool:
    out(f"This run uses about {requests} real AI request{'' if requests == 1 else 's'} ({explanation}).")
    out("A request that fails can take a second attempt; long summaries use one more per part.")
    if args.yes:
        return True
    try:
        answer = ask("Go ahead? [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


# --- Running cases --------------------------------------------------------------------------------------

def run_case(session_factory: sessionmaker, ai: RecordingAI, user_id: uuid.UUID, document: Document, case: Case) -> dict:
    """Asks the case's turns in order, as one conversation. Raises
    DailyLimitReached; any other AI failure is recorded as the case's error."""
    history: list[ChatMessage] = []
    follow_up = False
    outcome = CaseOutcome()
    first_reply = len(ai.replies)
    latency = None
    for turn in case.turns:
        turn_reply = len(ai.replies)
        started = time.monotonic()
        try:
            with session_factory() as db:
                answer = modes.answer_message(db, ai, user_id, document, history, turn, follow_up)
        except DailyLimitReached:
            raise
        except modes.EmbeddedWithFakeAI:
            outcome = CaseOutcome(error="embedded_with_fake_ai")  # retry the document with the real AI
            break
        except AIServiceError:
            outcome = CaseOutcome(error="ai_unavailable")
            break
        latency = round(time.monotonic() - started, 2)
        outcome = CaseOutcome(
            answer=answer.content, outcome=answer.outcome, mode=answer.mode, citations=answer.citations,
            retrieved_sections=[c.section_number for c in answer.retrieved] if answer.mode == MODE_QUESTION else None,
        )
        history += [{"role": "user", "content": turn}, {"role": "assistant", "content": strip_markers(answer.content)}]
        follow_up = follow_up or answer.outcome == "answered"
    scored_replies = ai.replies[turn_reply:]  # the last turn's requests
    checks = score_case(case, outcome)
    return {
        "id": case.id, "kind": case.kind, "checks": checks, "passed": passed(checks),
        "latency_seconds": latency,
        "model": scored_replies[-1]["model"] if scored_replies else None,
        "finish_reason": scored_replies[-1]["finish_reason"] if scored_replies else None,
        "completion_tokens": sum(r["completion_tokens"] or 0 for r in scored_replies) if scored_replies else None,
        "reasoning_tokens": sum(r["reasoning_tokens"] or 0 for r in scored_replies) if scored_replies else None,
        "requests": ai.replies[first_reply:],
        **asdict(outcome),
    }


def chunk_similarities(session_factory: sessionmaker, user_id: uuid.UUID, document_id: uuid.UUID, vector) -> list[dict]:
    """The similarity of every chunk retrieval could use (body sections), most similar first."""
    with session_factory() as db:
        rows = db.execute(similar_chunks_query(user_id, document_id, vector, top_k=100_000)).all()
    return [{"section_number": row[4], "similarity": round(float(row[6]), 4)} for row in rows]


def question_mode(session_factory: sessionmaker, user_id: uuid.UUID, document: Document, question: str) -> bool:
    with session_factory() as db:
        sections = modes.section_infos(db, user_id, document.id)
    return parse_request(question, document.page_count or 0, sections).mode == MODE_QUESTION


# --- The run ---------------------------------------------------------------------------------------------

def save_results(results_dir: Path, quality_set: QualitySet, results: dict) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{results['started_at_compact']}-{quality_set.name}.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def run(
    args, *, ai: AIClient, storage: Storage, session_factory: sessionmaker, root: Path = ROOT,
    ask: Callable[[str], str] = input, out: Callable[[str], None] = print,
) -> int:
    try:
        quality_set = load_set(root / "quality" / f"{args.set}.yaml")
        cases = select_cases(quality_set.cases, [n.strip() for n in args.only.split(",") if n.strip()] if args.only else None)
    except QualitySetError as error:
        out(f"Can't run: {error}")
        return REFUSED
    if args.retrieval_only and args.chat_model:
        out("Can't run: --chat-model has no effect with --retrieval-only (no chat requests).")
        return REFUSED

    recording = RecordingAI(ai)
    cache_path = root / "quality" / "results" / EMBEDDING_CACHE
    embedding_model = getattr(ai, "embedding_model", None) or settings.ai_embedding_model
    cached = load_embedding_cache(cache_path, embedding_model, settings.embedding_dimensions)
    recording.query_vectors.update(cached)
    out(f"Database: {database_label()}")
    user_id = quality_user(session_factory)
    needed = {case.document for case in cases}
    files: dict[str, bytes] = {}
    uploads = 0
    try:
        for key in needed:
            qdoc = quality_set.documents[key]
            files[key] = (root / qdoc.file).read_bytes()
            existing = existing_document(session_factory, user_id, files[key])
            if existing is None or existing.status != STATUS_READY:
                uploads += embedding_requests_for(files[key], qdoc.title)
    except (OSError, RunRefused) as error:
        out(f"Can't run: {error}")
        return REFUSED

    requests, explanation = request_plan(cases, uploads, args.retrieval_only, set(cached))
    if not confirmed(args, requests, explanation, ask, out):
        out("Cancelled. No AI request was made.")
        return REFUSED

    started = datetime.now(UTC)
    results = {
        "set": quality_set.name,
        "run": "retrieval_only" if args.retrieval_only else "full",
        "started_at": started.isoformat(timespec="seconds"),
        "started_at_compact": started.strftime("%Y%m%d-%H%M%S"),
        "settings": {
            "chat_models": getattr(ai, "chat_models", None),
            "embedding_model": getattr(ai, "embedding_model", None),
            "retrieval_min_similarity": settings.retrieval_min_similarity,
            "retrieval_top_k": settings.retrieval_top_k,
            "max_tokens": {
                "question": settings.ai_max_tokens_question, "page": settings.ai_max_tokens_page,
                "summary": settings.ai_max_tokens_summary, "rewrite": settings.ai_max_tokens,
            },
        },
        "stopped": None,
        "cases": [],
    }
    results_dir = root / "quality" / "results"
    try:
        documents = {
            key: ready_document(session_factory, storage, recording, user_id, quality_set.documents[key], files[key])
            for key in sorted(needed)
        }
        # Every question answered by retrieval and not cached yet, in one request
        missing = [text for text in question_texts(cases) if text not in recording.query_vectors]
        if missing:
            recording.embed_batch(missing, "query")

        if args.retrieval_only:
            similarities = {}
            for case in cases:
                if case.kind == "follow_up" or not uses_retrieval(case):
                    continue  # no chat requests here, so a follow up can't be rewritten
                document = documents[case.document]
                if not question_mode(session_factory, user_id, document, case.question):
                    continue
                vector = recording.query_vectors[case.question]
                similarities[case.id] = chunk_similarities(session_factory, user_id, document.id, vector)
            results["similarities"] = similarities
            results["cutoff_table"] = cutoff_table(cases, similarities)
        else:
            for case in cases:
                out(f"  {case.id}...")
                results["cases"].append(run_case(session_factory, recording, user_id, documents[case.document], case))
    except DailyLimitReached:
        results["stopped"] = "daily_limit"
    except RunRefused as error:
        out(f"Stopped: {error}")
        results["stopped"] = "refused"
    except AIServiceError as error:
        out(f"Stopped: the AI couldn't prepare the run ({error}).")
        results["stopped"] = "ai_unavailable"
    finally:
        results["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        results["requests"] = {"chat": recording.chat_requests, "embedding": recording.embedding_requests}
        if results["cases"]:
            results["passed"] = sum(1 for c in results["cases"] if c["passed"])
            results["total"] = len(results["cases"])
        path = save_results(results_dir, quality_set, results)
        new_vectors = {t: v for t, v in recording.query_vectors.items() if t not in cached}
        if new_vectors:
            save_embedding_cache(cache_path, embedding_model, new_vectors)

    if results.get("cutoff_table"):
        out(format_cutoff_table(results["cutoff_table"]))
    if results["cases"]:
        out(summary_table(results["cases"]))
    out(f"Requests used: {recording.chat_requests} chat, {recording.embedding_requests} embedding.")
    out(f"Results saved to {path.relative_to(root)}")
    if results["stopped"] == "daily_limit":
        out("Stopped early: OpenRouter's free daily allowance is used up. Run the rest tomorrow with --only.")
        return STOPPED
    return OK if results["stopped"] is None else STOPPED


def list_models(fetch: Callable[[str], dict], out: Callable[[str], None] = print) -> int:
    """The free chat models from OpenRouter's public model list. Not a chat
    request, and no key is sent."""
    try:
        payload = fetch(MODELS_URL)
    except Exception as error:
        out(f"Couldn't read OpenRouter's model list ({type(error).__name__}).")
        return REFUSED
    out(format_models(free_chat_models(payload)))
    return OK


def fetch_json(url: str) -> dict:
    response = httpx.get(url, timeout=30, headers={"User-Agent": "pinpage-quality-check"})
    response.raise_for_status()
    return response.json()


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pinpage's quality check (uses the real AI).")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="Ask the set's questions and score the answers")
    run_parser.add_argument("--set", required=True, help="The question set: quality/<set>.yaml")
    run_parser.add_argument("--only", help="Comma separated case ids")
    run_parser.add_argument("--retrieval-only", action="store_true", help="One embedding request, no chat: the cut off table")
    run_parser.add_argument("--chat-model", help="Ask this chat model only (no fallback)")
    run_parser.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    commands.add_parser("models", help="List OpenRouter's free chat models")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(name)s: %(message)s")
    args = parser().parse_args(argv)
    if args.command == "models":
        return list_models(fetch_json)

    from app.db.session import SessionLocal
    from app.services.ai import OpenRouterAI
    from app.services.storage import default_storage

    if settings.openrouter_api_key is None:
        print("Can't run: OPENROUTER_API_KEY is not set.")
        return REFUSED
    ai = OpenRouterAI.from_settings(settings)
    if args.chat_model:
        ai.chat_models = [args.chat_model]
    return run(args, ai=ai, storage=default_storage(), session_factory=SessionLocal)


if __name__ == "__main__":
    sys.exit(main())
