"""The quality check: the question set, scoring, the cut off table, the
model list and the runner. The runner runs against FakeAI and FakeStorage
here; it never runs against the real AI in the tests."""

import argparse
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from sqlalchemy import func, select

from app.cli import quality as runner
from app.core.config import settings
from app.models import Document, Message, QuestionUsage, User
from app.services.ai import AIServiceError, ChatReply, DailyLimitReached
from app.services.prompts import NOT_FOUND_MESSAGE
from app.services.quality import (
    Case,
    CaseOutcome,
    QualitySetError,
    contains,
    cutoff_table,
    format_cutoff_table,
    free_chat_models,
    load_set,
    parse_set,
    score_case,
    select_cases,
    summary_table,
)

ROOT = Path(__file__).resolve().parents[1]
LANTERN_HILL = ROOT / "quality" / "lantern-hill.yaml"

SPEC_CASES = [
    "annual-new", "annual-ten", "carry-over", "doctor-note", "sick-half", "maternity", "paternity", "probation",
    "salary-date", "transport", "pension", "marking", "notice", "gifts", "breach", "flexible", "car-loan", "gym",
    "partial", "page-8", "section-12", "follow-up", "inject-fact", "inject-leak", "inject-marker",
]


def case(**fields) -> Case:
    defaults = {"id": "c", "kind": "answer", "turns": ["Q?"], "document": "doc"}
    return Case(**{**defaults, **fields})


def cited(section, page_start=4, page_end=None):
    return {"marker": "S1", "section_number": section, "page_start": page_start, "page_end": page_end or page_start}


# --- The question set --------------------------------------------------------------------------------

def test_the_lantern_hill_set_has_every_case_in_the_spec():
    quality_set = load_set(LANTERN_HILL)

    ids = [c.id for c in quality_set.cases]
    assert ids == SPEC_CASES  # 25 cases
    by_id = {c.id: c for c in quality_set.cases}
    for name in ("inject-fact", "inject-leak", "inject-marker"):
        assert by_id[name].document == "injection-test" and by_id[name].kind == "injection"
    assert by_id["inject-fact"].expect_any == [["18"]] and by_id["inject-fact"].expect_none == ["60"]
    assert by_id["inject-leak"].expect_none == ["Rules:", "Never reveal", "excerpts"]
    assert by_id["inject-marker"].expect_sections == ["3"]
    assert by_id["carry-over"].expect_any == [["five", "5"], ["31 December"]]
    assert by_id["follow-up"].turns == ["How long is maternity leave?", "And paternity?"]
    assert by_id["page-8"].expect_pages == [8] and by_id["page-8"].expect_mode == "page"
    assert by_id["section-12"].expect_sections == ["12"] and by_id["section-12"].expect_mode == "section"
    for c in quality_set.cases:
        assert (ROOT / quality_set.documents[c.document].file).is_file()


@pytest.mark.parametrize(("broken", "message"), [
    ({"cases": [{"id": "a", "kind": "answer", "question": "Q"}, {"id": "a", "kind": "answer", "question": "Q"}]}, "twice"),
    ({"cases": [{"id": "a", "kind": "guess", "question": "Q"}]}, "kind must be"),
    ({"cases": [{"id": "a", "kind": "follow_up", "turns": ["only one"]}]}, "two or more"),
    ({"cases": [{"id": "a", "kind": "answer"}]}, "needs a question"),
    ({"cases": [{"id": "a", "kind": "answer", "question": "Q", "expect_any": [[]]}]}, "group is empty"),
    ({"cases": [{"id": "a", "kind": "answer", "question": "Q", "document": "other"}]}, "unknown document"),
    ({"cases": []}, "no cases"),
])
def test_an_invalid_set_is_refused_with_the_reason(broken, message):
    data = {"name": "s", "documents": {"doc": {"file": "x.pdf", "title": "X"}}, **broken}

    with pytest.raises(QualitySetError, match=message):
        parse_set(data)


def test_only_picks_cases_in_set_order_and_refuses_unknown_names():
    cases = [case(id="a"), case(id="b"), case(id="c")]

    assert [c.id for c in select_cases(cases, ["c", "a"])] == ["a", "c"]
    with pytest.raises(QualitySetError, match="no case named zz"):
        select_cases(cases, ["a", "zz"])


# --- Scoring -------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "word", "found"), [
    ("New staff get 20 days [S1].", "20", True),
    ("New staff get 200 days.", "20", False),  # not part of a longer number
    ("Since 2020.", "20", False),
    ("Paid on the 25th of each month.", "25", True),  # a suffix after a number is fine
    ("It is NOT covered.", "not", True),
    ("Please note this.", "not", False),
    ("Ten  PERCENT\nof pay", "10 percent", False),
    ("Ten  PERCENT\nof pay", "ten percent", True),  # spacing and case ignored
    ("A 10% contribution", "10%", True),
    ("By 31 December.", "31 december", True),
])
def test_contains_matches_whole_words_and_numbers(text, word, found):
    assert contains(text, word) is found


def test_an_answer_case_passes_every_check():
    c = case(expect_any=[["five", "5"], ["31 December"]], expect_sections=["4.3"])
    outcome = CaseOutcome(
        answer="Up to five days, used by 31 December [S1].", outcome="answered", mode="question",
        citations=[cited("4.3")], retrieved_sections=["4.3", "4.1"],
    )

    assert score_case(c, outcome) == {"retrieval": True, "answer": True, "citation": True}


def test_any_word_of_a_group_counts_but_every_group_is_needed():
    c = case(expect_any=[["five", "5"], ["31 December"]])

    assert score_case(c, CaseOutcome(answer="5 days by 31 December"))["answer"] is True
    assert score_case(c, CaseOutcome(answer="Five days."))["answer"] is False


def test_expect_none_fails_the_answer():
    c = case(kind="injection", expect_none=["Rules:", "excerpts"])

    assert score_case(c, CaseOutcome(answer="I can only help with the document."))["answer"] is True
    assert score_case(c, CaseOutcome(answer="Rules: answer only from the excerpts"))["answer"] is False


def test_retrieval_and_citation_need_an_expected_section_or_its_subsection():
    c = case(expect_sections=["12"])
    outcome = CaseOutcome(answer="x", citations=[cited("12.3")], retrieved_sections=["11.1", "12.1"])

    assert score_case(c, outcome) == {"retrieval": True, "citation": True}
    wrong = CaseOutcome(answer="x", citations=[cited("1.2"), cited("4")], retrieved_sections=["120", None])
    assert score_case(case(expect_sections=["4.1", "12"]), wrong) == {"retrieval": False, "citation": False}


def test_a_not_found_case_needs_the_not_found_answer_with_no_citation():
    c = case(kind="not_found")

    assert score_case(c, CaseOutcome(answer=NOT_FOUND_MESSAGE, outcome="not_found", retrieved_sections=[])) == {"outcome": True}
    made_up = CaseOutcome(answer="Yes, a car loan [S1].", outcome="answered", citations=[cited("3.5")])
    assert score_case(c, made_up) == {"outcome": False}


def test_a_partial_case_needs_the_answer_and_the_part_not_covered():
    c = case(kind="partial", expect_any=[["10"], ["not", "doesn't", "does not"]], expect_sections=["4.6"])

    good = CaseOutcome(
        answer="You get 10 days [S1]. The handbook doesn't mention childcare vouchers.",
        outcome="answered", citations=[cited("4.6")], retrieved_sections=["4.6"],
    )
    assert all(score_case(c, good).values())
    half = CaseOutcome(answer="You get 10 days [S1].", outcome="answered", citations=[cited("4.6")], retrieved_sections=["4.6"])
    assert score_case(c, half)["answer"] is False


def test_page_and_section_cases_check_pages_and_mode_but_not_retrieval():
    page = case(kind="page", expect_any=[["25"]], expect_pages=[8], expect_mode="page")
    outcome = CaseOutcome(answer="Salaries are paid on the 25th [S1].", mode="page", citations=[cited("3.1", 8)])

    assert score_case(page, outcome) == {"answer": True, "citation": True, "mode": True}
    wrong_page = CaseOutcome(answer="Paid on the 25th [S1].", mode="question", citations=[cited("3.1", 9)])
    assert score_case(page, wrong_page) == {"answer": True, "citation": False, "mode": False}

    section = case(kind="section", expect_any=[["notice"], ["final pay"]], expect_sections=["12"], expect_mode="section")
    summary = CaseOutcome(answer="Notice periods [S1]. Final pay [S2].", mode="section", citations=[cited("12.1"), cited("12.4")])
    assert score_case(section, summary) == {"answer": True, "citation": True, "mode": True}


def test_a_follow_up_is_scored_on_its_last_answer():
    c = case(kind="follow_up", turns=["How long is maternity leave?", "And paternity?"], expect_any=[["10"]], expect_sections=["4.6"])
    outcome = CaseOutcome(answer="Paternity leave is 10 days [S1].", citations=[cited("4.6")], retrieved_sections=["4.6"])

    assert c.question == "And paternity?"
    assert all(score_case(c, outcome).values())


def test_a_case_with_an_error_fails_every_check_that_applies():
    c = case(expect_any=[["20"]], expect_sections=["4.1"])

    checks = score_case(c, CaseOutcome(error="ai_unavailable"))

    assert checks == {"retrieval": False, "answer": False, "citation": False}
    assert score_case(case(), CaseOutcome(error="ai_unavailable")) == {"answer": False}


def test_a_question_answered_without_retrieval_fails_the_retrieval_check():
    # The parser sent an ordinary question to page mode: nothing was retrieved
    c = case(expect_sections=["4.1"])

    assert score_case(c, CaseOutcome(answer="x", mode="page", retrieved_sections=None))["retrieval"] is False


def test_summary_table_lists_cases_and_totals():
    results = [
        {"id": "annual-new", "kind": "answer", "checks": {"retrieval": True, "answer": True, "citation": True},
         "passed": True, "latency_seconds": 4.0, "model": "m1"},
        {"id": "car-loan", "kind": "not_found", "checks": {"outcome": False}, "passed": False,
         "latency_seconds": 2.0, "model": "m1"},
        {"id": "gym", "kind": "not_found", "checks": {"outcome": False}, "passed": False,
         "latency_seconds": None, "model": None, "error": "ai_unavailable"},
    ]

    table = summary_table(results)

    lines = table.splitlines()
    assert lines[0].split() == ["case", "kind", "retrieval", "answer", "citation", "mode", "outcome", "pass", "seconds", "model"]
    assert lines[2].split() == ["annual-new", "answer", "ok", "ok", "ok", "-", "-", "PASS", "4.0", "m1"]
    assert lines[3].split() == ["car-loan", "not_found", "-", "-", "-", "-", "FAIL", "FAIL", "2.0", "m1"]
    assert "Passed 1 of 3 cases." in table
    assert "  outcome: 0 of 2" in table
    assert "Median latency: 3.0 seconds" in table  # failed cases left out


# --- The cut off table ------------------------------------------------------------------------------

def test_cutoff_table_counts_from_stored_similarities():
    cases = [
        case(id="a", expect_sections=["4.1"]),
        case(id="b", expect_sections=["3.3"]),
        case(id="n", kind="not_found"),
        case(id="skipped", expect_sections=["9"]),  # no similarities stored (e.g. a page case)
    ]
    similarities = {
        "a": [{"section_number": "4.1", "similarity": 0.52}, {"section_number": "2", "similarity": 0.60}],
        "b": [{"section_number": "3.3", "similarity": 0.31}, {"section_number": "3.4", "similarity": 0.45}],
        "n": [{"section_number": "3.5", "similarity": 0.33}],
    }

    rows = {row["cutoff"]: row for row in cutoff_table(cases, similarities)}

    assert list(rows) == [0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5]
    assert (rows[0.3]["answerable_kept"], rows[0.3]["answerable"]) == (2, 2)
    assert rows[0.35]["answerable_kept"] == 1  # b's expected section is at 0.31; its 0.45 is another section
    assert rows[0.5]["answerable_kept"] == 1 and rows[0.5]["not_found_with_chunks"] == 0
    assert (rows[0.3]["not_found_with_chunks"], rows[0.3]["not_found"]) == (1, 1)  # at the cut off counts
    assert rows[0.35]["not_found_with_chunks"] == 0
    assert "0.30" in format_cutoff_table(list(rows.values()))


# --- Models -------------------------------------------------------------------------------------------

def test_free_chat_models_from_the_public_list():
    payload = {"data": [
        {"id": "a/thinker:free", "context_length": 128000, "supported_parameters": ["max_tokens", "reasoning"],
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        {"id": "b/plain", "context_length": 32000, "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"modality": "text->text"}},
        {"id": "c/paid", "pricing": {"prompt": "0.000001", "completion": "0.000002"},
         "architecture": {"output_modalities": ["text"]}},
        {"id": "d/embed:free", "architecture": {"input_modalities": ["text"], "output_modalities": ["embeddings"]}},
    ]}

    assert free_chat_models(payload) == [
        {"id": "a/thinker:free", "context_length": 128000, "reasoning": True},
        {"id": "b/plain", "context_length": 32000, "reasoning": False},
    ]


def test_models_command_prints_the_list_without_a_chat_request(fake_ai):
    printed = []
    payload = {"data": [{"id": "a/m:free", "context_length": 8000, "architecture": {"output_modalities": ["text"]}}]}

    assert runner.list_models(lambda url: payload, out=printed.append) == runner.OK
    assert "a/m:free" in printed[0] and "1 free chat models." in printed[0]
    assert fake_ai.reply_calls == []

    def offline(url):
        raise httpx.ConnectError("no network")

    assert runner.list_models(offline, out=printed.append) == runner.REFUSED


# --- The AI layer's part --------------------------------------------------------------------------

def openrouter_error(message: str) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return openai.RateLimitError(message, response=httpx.Response(429, request=request), body={"error": {"message": message}})


def ai_with(responses):
    from tests.test_ai import make_ai

    return make_ai(responses)


def test_a_daily_limit_429_is_its_own_error_and_never_retried():
    ai, calls = ai_with([openrouter_error("Rate limit exceeded: free-models-per-day."), "unused"])

    with pytest.raises(DailyLimitReached) as error:
        ai.generate("System", [])
    assert error.value.retryable is False and len(calls) == 1


def test_a_per_minute_429_is_an_ordinary_retried_error():
    from tests.test_ai import chat

    ai, calls = ai_with([openrouter_error("Rate limit exceeded: free-models-per-min."), chat("Ready", model="fallback-model")])

    assert ai.generate("System", []).text == "Ready"
    assert len(calls) == 2


def test_replies_carry_their_model_and_tokens():
    from tests.test_ai import chat

    usage = SimpleNamespace(completion_tokens=900, completion_tokens_details=SimpleNamespace(reasoning_tokens=700))
    ai, _ = ai_with([chat("Ready", model="the-model", usage=usage)])

    reply = ai.generate("System", [])

    assert (reply.model, reply.completion_tokens, reply.reasoning_tokens) == ("the-model", 900, 700)


# --- The runner ---------------------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path):
    """A project root with the question set and the handbook, so results
    are written to a temporary folder."""
    (tmp_path / "quality").mkdir()
    shutil.copy(LANTERN_HILL, tmp_path / "quality" / "lantern-hill.yaml")
    (tmp_path / "docs" / "demo").mkdir(parents=True)
    quality_set = load_set(LANTERN_HILL)
    for document in quality_set.documents.values():
        shutil.copy(ROOT / document.file, tmp_path / document.file)
    return tmp_path


def args(**changes) -> argparse.Namespace:
    return argparse.Namespace(**{"set": "lantern-hill", "only": None, "retrieval_only": False, "chat_model": None, "yes": True, **changes})


@pytest.fixture
def run_quality(project, fake_ai, fake_storage, session_factory, monkeypatch):
    """run_quality(**args) -> (exit code, printed lines, results or None).
    Every chunk passes the cut off, so fake embeddings always retrieve."""
    monkeypatch.setattr(settings, "retrieval_min_similarity", -1.0)
    monkeypatch.setattr(settings, "rewrite_follow_ups", True)

    def _run(answers=("y",), **changes):
        printed: list[str] = []
        replies = list(answers)
        code = runner.run(
            args(**changes), ai=fake_ai, storage=fake_storage, session_factory=session_factory, root=project,
            ask=lambda prompt: replies.pop(0), out=printed.append,
        )
        saved = sorted(
            path for path in (project / "quality" / "results").glob("*.json") if path.name != runner.EMBEDDING_CACHE
        )
        return code, printed, json.loads(saved[-1].read_text()) if saved else None

    return _run


def test_the_runner_refuses_without_confirmation(db, run_quality, fake_ai):
    code, printed, results = run_quality(answers=["n"], yes=False, only="annual-new")

    assert code == runner.REFUSED
    assert any(line.startswith("This run uses about ") for line in printed)
    assert "Cancelled. No AI request was made." in printed
    assert fake_ai.reply_calls == [] and fake_ai.embed_calls == []
    assert results is None
    assert db.scalar(select(func.count()).select_from(Document)) == 0  # nothing uploaded either


def test_the_request_count_is_shown_before_starting(run_quality):
    code, printed, _ = run_quality(answers=["n"], yes=False, only="annual-new,follow-up,page-8")

    # 2 to embed the handbook's chunks, 1 for the questions, 4 answers, a
    # follow up rewrite and its embedding
    assert printed[1] == (
        "This run uses about 9 real AI requests (2 to embed demo documents, 1 to embed the questions, "
        "4 for answers, 2 for rewriting and embedding follow ups)."
    )


def test_a_run_uploads_once_answers_directly_and_saves_scored_results(db, run_quality, fake_ai, monkeypatch):
    monkeypatch.setattr(settings, "daily_questions_per_user", 0)  # a user's limit never applies
    fake_ai.reply = "New full time staff get 20 days of annual leave [S1]."

    code, printed, results = run_quality(only="annual-new,page-8")

    assert code == runner.OK
    [query_batch] = [c for c in fake_ai.embed_calls if c["mode"] == "query"]
    assert query_batch["texts"] == ["How many annual leave days do new full time staff get?"]  # page-8 needs none
    assert [c["id"] for c in results["cases"]] == ["annual-new", "page-8"]
    annual = results["cases"][0]
    assert annual["answer"] == fake_ai.reply and annual["mode"] == "question"
    assert set(annual["checks"]) == {"retrieval", "answer", "citation"} and annual["checks"]["answer"] is True
    assert annual["retrieved_sections"] and annual["requests"][0]["max_tokens"] == settings.ai_max_tokens_question
    assert results["cases"][1]["mode"] == "page" and results["cases"][1]["retrieved_sections"] is None
    assert results["stopped"] is None and results["total"] == 2
    assert any(line.startswith("Passed ") for line in "\n".join(printed).splitlines())
    # Nothing saved as a conversation, nothing counted
    assert db.scalar(select(func.count()).select_from(Message)) == 0
    assert db.scalar(select(func.count()).select_from(QuestionUsage)) == 0
    assert db.scalar(select(User.email)) == runner.QUALITY_EMAIL

    # A second run reuses the processed document and the cached question
    # embedding: no embedding request at all
    fake_ai.embed_calls.clear()
    code, printed, _ = run_quality(only="annual-new")
    assert printed[1] == "This run uses about 1 real AI request (0 to embed the questions (all cached), 1 for answers)."
    assert fake_ai.embed_calls == []
    assert db.scalar(select(func.count()).select_from(Document)) == 1


def test_a_follow_up_is_asked_as_one_conversation(run_quality, fake_ai):
    fake_ai.replies = ["Maternity leave is 16 weeks [S1].", "And paternity leave?", "Paternity leave is 10 days [S1]."]

    _, _, results = run_quality(only="follow-up")

    [result] = results["cases"]
    assert result["answer"] == "Paternity leave is 10 days [S1]."
    assert len(result["requests"]) == 3  # answer, rewrite, answer
    rewrite_history = fake_ai.reply_calls[1]["history"][0]["content"]
    assert "Maternity leave is 16 weeks." in rewrite_history  # the first answer, markers removed


def test_an_ai_failure_fails_the_case_and_the_run_goes_on(run_quality, fake_ai):
    fake_ai.replies = [AIServiceError("simulated outage"), "Paid on the 25th [S1]."]

    code, _, results = run_quality(only="annual-new,salary-date")

    assert code == runner.OK
    assert results["cases"][0]["error"] == "ai_unavailable" and results["cases"][0]["passed"] is False
    assert results["cases"][1]["error"] is None


def test_the_run_stops_cleanly_on_the_daily_limit(run_quality, fake_ai):
    fake_ai.replies = ["New staff get 20 days [S1].", DailyLimitReached()]

    code, printed, results = run_quality(only="annual-new,annual-ten,carry-over")

    assert code == runner.STOPPED
    assert results["stopped"] == "daily_limit"
    assert [c["id"] for c in results["cases"]] == ["annual-new"]  # kept what it had
    assert len(fake_ai.reply_calls) == 2  # carry-over was never asked
    assert any("free daily allowance is used up" in line for line in printed)


def test_retrieval_only_uses_one_embedding_and_no_chat(run_quality, fake_ai):
    code, printed, results = run_quality(retrieval_only=True)

    assert code == runner.OK
    assert fake_ai.reply_calls == []
    assert [c["mode"] for c in fake_ai.embed_calls if c["mode"] == "query"] == ["query"]
    assert "page-8" not in results["similarities"] and "follow-up" not in results["similarities"]
    annual = results["similarities"]["annual-new"]
    assert annual and all(set(s) == {"section_number", "similarity"} for s in annual)
    assert [row["cutoff"] for row in results["cutoff_table"]] == [0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5]
    assert results["cutoff_table"][0]["not_found"] == 2
    # The handbook takes 2 embedding requests and the hostile guide 1
    assert printed[1].startswith("This run uses about 4 real AI requests (3 to embed demo documents, 1 to embed the questions)")
    assert "inject-marker" in results["similarities"]
    assert any(line.startswith("cut off") for line in "\n".join(printed).splitlines())


def test_chat_model_and_retrieval_only_together_are_refused(run_quality, fake_ai):
    code, printed, _ = run_quality(retrieval_only=True, chat_model="some/model")

    assert code == runner.REFUSED and fake_ai.embed_calls == []


def test_an_unknown_case_is_refused_before_any_request(run_quality, fake_ai):
    code, printed, _ = run_quality(only="annual-new,nope")

    assert code == runner.REFUSED and printed == ["Can't run: no case named nope"]
    assert fake_ai.embed_calls == []


def test_recording_ai_keeps_counts_never_text(fake_ai):
    recording = runner.RecordingAI(fake_ai)
    fake_ai.replies = [ChatReply("secret answer", "stop", "m", 10, 4)]

    recording.generate("System", [], max_tokens=100)

    [record] = recording.replies
    assert record["model"] == "m" and record["completion_tokens"] == 10 and record["reasoning_tokens"] == 4
    assert "secret answer" not in json.dumps(record)


# --- The question embedding cache ------------------------------------------------------------------

def cache_file(project) -> Path:
    return project / "quality" / "results" / runner.EMBEDDING_CACHE


def query_batches(fake_ai) -> list[list[str]]:
    return [c["texts"] for c in fake_ai.embed_calls if c["mode"] == "query"]


def test_question_embeddings_are_cached_per_model_and_reused_by_later_runs(project, run_quality, fake_ai):
    annual = "How many annual leave days do new full time staff get?"
    carry = "Can I carry unused leave into next year?"

    run_quality(only="annual-new", retrieval_only=True)

    cache = json.loads(cache_file(project).read_text())
    assert list(cache) == [settings.ai_embedding_model]
    assert cache[settings.ai_embedding_model][annual] == fake_ai.vector_for(annual)

    # Later runs (for example one per --chat-model) count and spend only
    # the questions not cached yet
    fake_ai.embed_calls.clear()
    code, printed, results = run_quality(only="annual-new,carry-over")
    assert code == runner.OK
    assert printed[1] == "This run uses about 3 real AI requests (1 to embed the questions (1 of 2 cached), 2 for answers)."
    assert query_batches(fake_ai) == [[carry]]
    assert results["requests"]["embedding"] == 1

    fake_ai.embed_calls.clear()
    _, printed, results = run_quality(only="annual-new,carry-over", retrieval_only=True)
    assert printed[1] == "This run uses about 0 real AI requests (0 to embed the questions (all cached))."
    assert fake_ai.embed_calls == [] and results["requests"]["embedding"] == 0
    assert results["similarities"]["carry-over"]  # scored from the cached vector

    # Another embedding model gets its own entries, and the first model's stay
    fake_ai.embedding_model = "other/embedder"
    run_quality(only="annual-new", retrieval_only=True)
    assert query_batches(fake_ai) == [[annual]]
    cache = json.loads(cache_file(project).read_text())
    assert set(cache) == {settings.ai_embedding_model, "other/embedder"}
    assert set(cache[settings.ai_embedding_model]) == {annual, carry}


def test_follow_up_rewrites_are_cached_too(project, run_quality, fake_ai):
    fake_ai.replies = ["Maternity leave is 16 weeks [S1].", "How long is paternity leave?", "10 days [S1]."]

    run_quality(only="follow-up")

    cached = json.loads(cache_file(project).read_text())[settings.ai_embedding_model]
    assert set(cached) == {"How long is maternity leave?", "How long is paternity leave?"}


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"m": "not a dict"})])
def test_an_unreadable_cache_is_treated_as_empty(tmp_path, content):
    path = tmp_path / "cache.json"
    path.write_text(content)

    assert runner.load_embedding_cache(path, "m", 4) == {}
    runner.save_embedding_cache(path, "m", {"Q?": [1.0, 0, 0, 0]})
    assert runner.load_embedding_cache(path, "m", 4) == {"Q?": [1.0, 0, 0, 0]}


def test_cached_vectors_of_the_wrong_size_are_not_used(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"m": {"old": [1.0, 2.0], "good": [1.0, 0, 0, 0]}}))

    assert runner.load_embedding_cache(path, "m", 4) == {"good": [1.0, 0, 0, 0]}
    assert runner.load_embedding_cache(tmp_path / "missing.json", "m", 4) == {}
