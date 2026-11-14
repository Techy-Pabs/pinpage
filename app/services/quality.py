"""The quality check's pure parts: reading a question set, scoring a case,
the cut off table, the summary table, and reading OpenRouter's model list.

No database, no AI, no files except load_set: everything here is tested
with saved fake results. The runner (app.cli.quality) does the requests.

A case's checks (each applies only when the case says something about it):
- retrieval: an expected section is among the retrieved excerpts
  (cases answered by retrieval only)
- answer: every expect_any group has at least one of its words in the
  answer, and no expect_none word is in it
- citation: a citation points to an expected section (and covers an
  expected page, if any are given)
- mode: the answer's mode is expect_mode
- outcome: a not_found case got the not found answer, citing nothing
A case passes when every check that applies passes.
"""

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = ("answer", "not_found", "partial", "page", "section", "follow_up", "injection")
MODES = ("question", "page", "section")

# Cut offs for the --retrieval-only table: 0.20 to 0.50 in steps of 0.05
CUTOFFS = tuple(round(0.20 + 0.05 * step, 2) for step in range(7))

CHECKS = ("retrieval", "answer", "citation", "mode", "outcome")


class QualitySetError(Exception):
    """The question set file is not valid; the message says why."""


@dataclass
class QualityDocument:
    key: str
    file: str  # relative to the project root
    title: str


@dataclass
class Case:
    id: str
    kind: str
    turns: list[str]  # one question, or the turns of a follow up
    document: str
    expect_any: list[list[str]] = field(default_factory=list)
    expect_none: list[str] = field(default_factory=list)
    expect_sections: list[str] = field(default_factory=list)
    expect_pages: list[int] = field(default_factory=list)
    expect_mode: str | None = None

    @property
    def question(self) -> str:
        """The question that is scored: the last turn."""
        return self.turns[-1]


@dataclass
class QualitySet:
    name: str
    documents: dict[str, QualityDocument]
    cases: list[Case]


# --- Reading a set ------------------------------------------------------------------------------

def _strings(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
        raise QualitySetError(f"{where} must be a list of words")
    return [str(v) for v in value]


def parse_set(data: dict) -> QualitySet:
    """A QualitySet from the YAML's contents. Raises QualitySetError."""
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise QualitySetError("the set needs a name")
    raw_documents = data.get("documents")
    if not isinstance(raw_documents, dict) or not raw_documents:
        raise QualitySetError("the set needs at least one document")
    documents = {}
    for key, value in raw_documents.items():
        if not isinstance(value, dict) or not isinstance(value.get("file"), str) or not isinstance(value.get("title"), str):
            raise QualitySetError(f"document {key} needs a file and a title")
        documents[str(key)] = QualityDocument(str(key), value["file"], value["title"])
    default_document = next(iter(documents))

    cases, seen = [], set()
    for raw in data.get("cases") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise QualitySetError("every case needs an id")
        case_id = raw["id"]
        if case_id in seen:
            raise QualitySetError(f"case {case_id} appears twice")
        seen.add(case_id)
        kind = raw.get("kind")
        if kind not in KINDS:
            raise QualitySetError(f"case {case_id}: kind must be one of {', '.join(KINDS)}")
        if kind == "follow_up":
            turns = _strings(raw.get("turns"), f"case {case_id}: turns")
            if len(turns) < 2 or "question" in raw:
                raise QualitySetError(f"case {case_id}: a follow up has turns (two or more), not a question")
        else:
            if not isinstance(raw.get("question"), str) or "turns" in raw:
                raise QualitySetError(f"case {case_id}: needs a question")
            turns = [raw["question"]]
        groups = raw.get("expect_any") or []
        if not isinstance(groups, list):
            raise QualitySetError(f"case {case_id}: expect_any must be a list of groups")
        expect_any = [_strings(group, f"case {case_id}: each expect_any group") for group in groups]
        if any(not group for group in expect_any):
            raise QualitySetError(f"case {case_id}: an expect_any group is empty")
        pages = raw.get("expect_pages") or []
        if not isinstance(pages, list) or not all(isinstance(p, int) and p > 0 for p in pages):
            raise QualitySetError(f"case {case_id}: expect_pages must be page numbers")
        mode = raw.get("expect_mode")
        if mode is not None and mode not in MODES:
            raise QualitySetError(f"case {case_id}: expect_mode must be one of {', '.join(MODES)}")
        document = str(raw.get("document", default_document))
        if document not in documents:
            raise QualitySetError(f"case {case_id}: unknown document {document}")
        cases.append(Case(
            id=case_id, kind=kind, turns=turns, document=document, expect_any=expect_any,
            expect_none=_strings(raw.get("expect_none"), f"case {case_id}: expect_none"),
            expect_sections=_strings(raw.get("expect_sections"), f"case {case_id}: expect_sections"),
            expect_pages=pages, expect_mode=mode,
        ))
    if not cases:
        raise QualitySetError("the set has no cases")
    return QualitySet(data["name"], documents, cases)


def load_set(path: Path) -> QualitySet:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise QualitySetError(f"can't read {path} ({type(error).__name__})") from None
    return parse_set(data)


def select_cases(cases: list[Case], only: list[str] | None) -> list[Case]:
    """The cases named in --only, in the set's order. Raises QualitySetError
    for a name that isn't in the set."""
    if not only:
        return cases
    unknown = [name for name in only if name not in {c.id for c in cases}]
    if unknown:
        raise QualitySetError(f"no case named {', '.join(unknown)}")
    return [c for c in cases if c.id in only]


# --- Scoring ----------------------------------------------------------------------------------------

@dataclass
class CaseOutcome:
    """What one case's answer was, as the runner records it (or as read
    back from a results file)."""

    answer: str = ""
    outcome: str | None = None  # answered or not_found
    mode: str | None = None
    citations: list[dict] = field(default_factory=list)
    # Section numbers of the excerpts retrieval kept; None when there was
    # no retrieval (page and section answers)
    retrieved_sections: list[str | None] | None = None
    error: str | None = None  # why there is no answer (e.g. ai_unavailable)


def contains(text: str, word: str) -> bool:
    """True if word (or phrase) is in text, ignoring case and spacing, and
    not as part of a longer word or number: "10" is not in "100" or "2010",
    "not" is not in "note", but "25" is in "25th"."""
    text = " ".join(text.lower().split())
    word = " ".join(word.lower().split())
    if not word:
        return False
    before = r"(?<![a-z0-9])" if word[0].isalnum() else ""
    if word[-1].isdigit():
        after = r"(?![0-9])"
    elif word[-1].isalpha():
        after = r"(?![a-z])"
    else:
        after = ""
    return re.search(before + re.escape(word) + after, text) is not None


def section_matches(cited: str | None, expected: str) -> bool:
    """"4.1" matches 4.1 and its subsections (4.1.2), not 4 or 4.10. So
    expected "12" accepts any of 12.x."""
    return cited is not None and (cited == expected or cited.startswith(expected + "."))


def _citation_ok(case: Case, citations: list[dict]) -> bool:
    if case.expect_sections and not any(
        section_matches(c.get("section_number"), expected) for c in citations for expected in case.expect_sections
    ):
        return False
    if case.expect_pages and not any(
        c["page_start"] <= page <= c["page_end"] for c in citations for page in case.expect_pages
    ):
        return False
    return True


def uses_retrieval(case: Case) -> bool:
    """True for cases answered by retrieval (ordinary questions)."""
    return case.kind not in ("page", "section") and case.expect_mode in (None, "question")


def score_case(case: Case, outcome: CaseOutcome) -> dict[str, bool]:
    """The checks that apply to this case, each passed or not. A case with
    no answer (an error) fails every check that applies."""
    checks: dict[str, bool] = {}
    failed = outcome.error is not None
    if uses_retrieval(case) and case.expect_sections:
        # No retrieved excerpts (a page or section answer instead) fails it
        checks["retrieval"] = not failed and any(
            section_matches(s, expected)
            for s in outcome.retrieved_sections or [] for expected in case.expect_sections
        )
    if case.expect_any or case.expect_none:
        checks["answer"] = (
            not failed
            and all(any(contains(outcome.answer, word) for word in group) for group in case.expect_any)
            and not any(contains(outcome.answer, word) for word in case.expect_none)
        )
    if case.kind != "not_found" and (case.expect_sections or case.expect_pages):
        checks["citation"] = not failed and _citation_ok(case, outcome.citations)
    if case.expect_mode:
        checks["mode"] = not failed and outcome.mode == case.expect_mode
    if case.kind == "not_found":
        checks["outcome"] = not failed and outcome.outcome == "not_found" and not outcome.citations
    if failed and not checks:
        checks["answer"] = False
    return checks


def passed(checks: dict[str, bool]) -> bool:
    return all(checks.values())


# --- The summary table ------------------------------------------------------------------------------

def _cell(checks: dict[str, bool], name: str) -> str:
    if name not in checks:
        return "-"
    return "ok" if checks[name] else "FAIL"


def summary_table(results: list[dict]) -> str:
    """One line per case (from the results file's "cases"), then totals."""
    header = ["case", "kind", *CHECKS, "pass", "seconds", "model"]
    rows = []
    for result in results:
        checks = result["checks"]
        rows.append([
            result["id"], result["kind"], *(_cell(checks, name) for name in CHECKS),
            "PASS" if result["passed"] else "FAIL",
            f"{result['latency_seconds']:.1f}" if result.get("latency_seconds") is not None else "-",
            result.get("model") or "-",
        ])
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(str(value).ljust(width) for value, width in zip(row, widths)).rstrip() for row in [header, *rows]]
    lines.insert(1, "  ".join("-" * width for width in widths))
    total = sum(1 for r in results if r["passed"])
    lines.append("")
    lines.append(f"Passed {total} of {len(results)} cases.")
    for name in CHECKS:
        applied = [r["checks"][name] for r in results if name in r["checks"]]
        if applied:
            lines.append(f"  {name}: {sum(applied)} of {len(applied)}")
    latencies = [r["latency_seconds"] for r in results if r.get("latency_seconds") is not None and not r.get("error")]
    if latencies:
        lines.append(f"Median latency: {statistics.median(latencies):.1f} seconds")
    return "\n".join(lines)


# --- The cut off table ------------------------------------------------------------------------------

def best_similarities(case: Case, similarities: list[dict]) -> tuple[float | None, float | None]:
    """(best similarity of any chunk, best similarity of a chunk in an
    expected section) from the stored [{"section_number", "similarity"}]."""
    best = max((s["similarity"] for s in similarities), default=None)
    expected = [
        s["similarity"] for s in similarities
        if any(section_matches(s["section_number"], e) for e in case.expect_sections)
    ]
    return best, max(expected, default=None)


def cutoff_table(cases: list[Case], similarities: dict[str, list[dict]], cutoffs=CUTOFFS) -> list[dict]:
    """For each cut off: how many answerable cases keep an expected section
    at or above it, and how many not found cases still have any chunk at or
    above it (retrieval keeps chunks whose similarity is at least the cut
    off). Cases without stored similarities are left out."""
    answerable = [c for c in cases if c.kind != "not_found" and c.expect_sections and c.id in similarities]
    not_found = [c for c in cases if c.kind == "not_found" and c.id in similarities]
    rows = []
    for cutoff in cutoffs:
        kept = sum(
            1 for c in answerable
            if (expected := best_similarities(c, similarities[c.id])[1]) is not None and expected >= cutoff
        )
        leaking = sum(
            1 for c in not_found
            if (best := best_similarities(c, similarities[c.id])[0]) is not None and best >= cutoff
        )
        rows.append({
            "cutoff": cutoff, "answerable_kept": kept, "answerable": len(answerable),
            "not_found_with_chunks": leaking, "not_found": len(not_found),
        })
    return rows


def format_cutoff_table(rows: list[dict]) -> str:
    lines = ["cut off  answerable cases kept  not found cases with a chunk above", "-------  ---------------------  ----------------------------------"]
    for row in rows:
        lines.append(
            f"{row['cutoff']:.2f}     {row['answerable_kept']:>2} of {row['answerable']:<15}"
            f"  {row['not_found_with_chunks']:>2} of {row['not_found']}"
        )
    return "\n".join(lines)


# --- OpenRouter's model list ----------------------------------------------------------------------

def _is_free(model: dict) -> bool:
    if str(model.get("id", "")).endswith(":free"):
        return True
    pricing = model.get("pricing") or {}
    try:
        return float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False


def _is_chat(model: dict) -> bool:
    architecture = model.get("architecture") or {}
    outputs = architecture.get("output_modalities")
    inputs = architecture.get("input_modalities")
    if outputs is not None:
        return "text" in outputs and (inputs is None or "text" in inputs)
    modality = str(architecture.get("modality", ""))
    return modality.startswith("text") and modality.endswith("->text")


def free_chat_models(payload: dict) -> list[dict]:
    """The free chat models in OpenRouter's public model list (GET
    /api/v1/models), sorted by id: id, context length, and whether it reasons."""
    models = []
    for model in payload.get("data") or []:
        if not isinstance(model, dict) or not model.get("id") or not _is_free(model) or not _is_chat(model):
            continue
        parameters = model.get("supported_parameters") or []
        models.append({
            "id": model["id"],
            "context_length": model.get("context_length"),
            "reasoning": "reasoning" in parameters or "include_reasoning" in parameters,
        })
    return sorted(models, key=lambda m: m["id"])


def format_models(models: list[dict]) -> str:
    width = max([len("model"), *(len(m["id"]) for m in models)])
    lines = [f"{'model'.ljust(width)}  context  reasoning", f"{'-' * width}  -------  ---------"]
    for m in models:
        context = f"{m['context_length']:,}" if isinstance(m["context_length"], int) else "?"
        lines.append(f"{m['id'].ljust(width)}  {context:>7}  {'yes' if m['reasoning'] else 'no'}")
    lines.append(f"{len(models)} free chat models.")
    return "\n".join(lines)
