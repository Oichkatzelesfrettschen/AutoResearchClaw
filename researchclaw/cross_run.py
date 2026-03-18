"""Cross-run learning system for AutoResearchClaw.

Provides persistent memory across pipeline runs:
- Run registry (index of all past runs by topic hash)
- Prior artifact injection (hypotheses, syntheses, experiment results)
- Cross-run literature deduplication (persistent shortlists)
- Quality score tracking and regression detection
- Project-scoped skill directories

Public API
----------
- ``RunRegistry`` — index and query past runs
- ``PriorRunContext`` — extract reusable context from prior runs
- ``inject_prior_context(run_dir, config)`` — main entry point for pipeline
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REGISTRY_DIR = Path(os.path.expanduser("~/.researchclaw"))
_REGISTRY_FILE = _REGISTRY_DIR / "run_registry.jsonl"
_LITERATURE_INDEX = _REGISTRY_DIR / "literature_index.jsonl"
_QUALITY_HISTORY = _REGISTRY_DIR / "quality_history.jsonl"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _topic_hash(topic: str) -> str:
    return hashlib.sha256(topic.strip().lower().encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Run Registry
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    """A single entry in the run registry."""

    run_id: str
    topic: str
    topic_hash: str
    run_dir: str
    started: str
    status: str = "running"  # running | completed | failed
    stages_done: int = 0
    stages_failed: int = 0
    quality_score: float | None = None
    final_stage: int = 0
    artifacts: list[str] = field(default_factory=list)


class RunRegistry:
    """Persistent index of all pipeline runs.

    Stores run metadata in ``~/.researchclaw/run_registry.jsonl``.
    Enables queries like "find all prior runs on this topic" for
    cross-run learning.
    """

    def __init__(self, registry_file: Path = _REGISTRY_FILE) -> None:
        self._path = registry_file
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def register_run(self, record: RunRecord) -> None:
        """Append a new run to the registry."""
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record)) + "\n")
        logger.info("Registered run %s (topic_hash=%s)", record.run_id, record.topic_hash)

    def update_run(self, run_id: str, **updates: Any) -> None:
        """Update fields of an existing run record (rewrite file)."""
        records = self._load_all()
        updated = False
        for rec in records:
            if rec["run_id"] == run_id:
                rec.update(updates)
                updated = True
                break
        if updated:
            self._write_all(records)

    def find_prior_runs(
        self, topic: str, *, exclude_run_id: str = "", limit: int = 5
    ) -> list[dict[str, Any]]:
        """Find prior runs on the same or similar topic.

        Returns most recent runs first, excluding the current run.
        """
        target_hash = _topic_hash(topic)
        records = self._load_all()
        matches = [
            r for r in records
            if r.get("topic_hash") == target_hash
            and r.get("run_id") != exclude_run_id
            and r.get("status") in ("completed", "failed")
        ]
        # Most recent first
        matches.sort(key=lambda r: r.get("started", ""), reverse=True)
        return matches[:limit]

    def _load_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        with self._path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Prior Run Context Extraction
# ---------------------------------------------------------------------------


@dataclass
class PriorRunContext:
    """Reusable context extracted from prior runs on the same topic."""

    prior_hypotheses: str = ""
    prior_synthesis: str = ""
    prior_experiment_results: str = ""
    prior_critiques: str = ""
    prior_quality_scores: list[float] = field(default_factory=list)
    prior_shortlisted_papers: list[dict[str, Any]] = field(default_factory=list)
    prior_lessons: list[str] = field(default_factory=list)
    prior_critique_lessons: list[str] = field(default_factory=list)
    run_count: int = 0


def extract_prior_context(
    topic: str,
    current_run_id: str,
    *,
    registry: RunRegistry | None = None,
) -> PriorRunContext:
    """Extract reusable context from all prior runs on the same topic.

    Reads prior run artifacts (hypotheses, synthesis, experiment results,
    shortlists, lessons) and assembles them into a context object that
    can be injected into current-run prompts.
    """
    reg = registry or RunRegistry()
    prior_runs = reg.find_prior_runs(topic, exclude_run_id=current_run_id)

    if not prior_runs:
        return PriorRunContext()

    ctx = PriorRunContext(run_count=len(prior_runs))

    for run in prior_runs:
        run_dir = Path(run.get("run_dir", ""))
        if not run_dir.is_dir():
            continue

        # Hypotheses from Stage 8
        hyp_path = run_dir / "stage-08" / "hypotheses.md"
        if hyp_path.is_file() and not ctx.prior_hypotheses:
            text = hyp_path.read_text(encoding="utf-8")
            # Truncate to avoid prompt bloat
            ctx.prior_hypotheses = text[:8000] if len(text) > 8000 else text

        # Synthesis from Stage 7
        syn_path = run_dir / "stage-07" / "synthesis.md"
        if syn_path.is_file() and not ctx.prior_synthesis:
            text = syn_path.read_text(encoding="utf-8")
            ctx.prior_synthesis = text[:5000] if len(text) > 5000 else text

        # Experiment results from Stage 14
        exp_path = run_dir / "stage-14" / "experiment_summary.json"
        if exp_path.is_file() and not ctx.prior_experiment_results:
            ctx.prior_experiment_results = exp_path.read_text(encoding="utf-8")[:3000]

        # Quality score
        qs = run.get("quality_score")
        if qs is not None:
            ctx.prior_quality_scores.append(float(qs))

        # Shortlisted papers from Stage 5
        sl_path = run_dir / "stage-05" / "shortlist.jsonl"
        if sl_path.is_file():
            for line in sl_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        paper = json.loads(line)
                        ctx.prior_shortlisted_papers.append(paper)
                    except json.JSONDecodeError:
                        continue

        # Lessons from evolution store
        lessons_path = run_dir / "evolution" / "lessons.jsonl"
        if lessons_path.is_file():
            for line in lessons_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        lesson = json.loads(line)
                        desc = lesson.get("description", "")
                        if desc and desc not in ctx.prior_lessons:
                            ctx.prior_lessons.append(desc)
                    except json.JSONDecodeError:
                        continue

        # Prior peer review critiques from Stage 18
        reviews_path = run_dir / "stage-18" / "reviews.md"
        if reviews_path.is_file() and not ctx.prior_critiques:
            text = reviews_path.read_text(encoding="utf-8")
            ctx.prior_critiques = text[:6000] if len(text) > 6000 else text
            # Extract tagged critique lessons for cross-run learning
            try:
                from researchclaw.critique import format_critique_lessons
                critique_lessons = format_critique_lessons(text)
                ctx.prior_critique_lessons.extend(critique_lessons)
            except Exception:  # noqa: BLE001
                pass

    # Deduplicate shortlisted papers by title
    seen_titles: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for paper in ctx.prior_shortlisted_papers:
        title = paper.get("title", "").strip().lower()
        if title and title not in seen_titles:
            seen_titles.add(title)
            deduped.append(paper)
    ctx.prior_shortlisted_papers = deduped

    return ctx


def format_prior_context_for_prompt(ctx: PriorRunContext) -> str:
    """Format prior-run context as text for LLM prompt injection.

    Returns empty string if no prior context exists.
    """
    if ctx.run_count == 0:
        return ""

    parts: list[str] = []
    parts.append(
        f"## Prior Run Context ({ctx.run_count} previous run(s) on this topic)\n"
    )

    if ctx.prior_quality_scores:
        scores_str = ", ".join(f"{s:.1f}" for s in ctx.prior_quality_scores)
        parts.append(f"**Prior quality scores**: {scores_str}")
        best = max(ctx.prior_quality_scores)
        parts.append(
            f"**Target**: Exceed best prior score ({best:.1f}). "
            "Do NOT regress.\n"
        )

    if ctx.prior_lessons:
        parts.append("**Lessons from prior runs** (avoid these mistakes):")
        for lesson in ctx.prior_lessons[:10]:
            parts.append(f"- {lesson[:200]}")
        parts.append("")

    if ctx.prior_hypotheses:
        parts.append("**Prior hypotheses** (build on these, do not repeat):")
        parts.append(ctx.prior_hypotheses[:4000])
        parts.append("")

    if ctx.prior_synthesis:
        parts.append("**Prior synthesis** (extend, do not duplicate):")
        parts.append(ctx.prior_synthesis[:3000])
        parts.append("")

    if ctx.prior_shortlisted_papers:
        parts.append(
            f"**Prior literature** ({len(ctx.prior_shortlisted_papers)} "
            "papers from previous runs -- reuse relevant ones):"
        )
        for paper in ctx.prior_shortlisted_papers[:20]:
            title = paper.get("title", "?")[:100]
            year = paper.get("year", "?")
            parts.append(f"- [{year}] {title}")
        parts.append("")

    if ctx.prior_critique_lessons:
        parts.append("**Prior review critiques** (address these or escalate):")
        for lesson in ctx.prior_critique_lessons[:8]:
            parts.append(f"- {lesson[:200]}")
        parts.append("")

    if ctx.prior_critiques:
        parts.append("**Full prior peer review** (verify issues were resolved):")
        parts.append(ctx.prior_critiques[:3000])
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Quality Regression Detection
# ---------------------------------------------------------------------------


def check_quality_regression(
    current_score: float,
    topic: str,
    current_run_id: str,
    *,
    registry: RunRegistry | None = None,
    threshold: float = 0.5,
) -> tuple[bool, str]:
    """Check if current quality score regressed from prior runs.

    Returns (is_regression, message).
    """
    reg = registry or RunRegistry()
    prior_runs = reg.find_prior_runs(topic, exclude_run_id=current_run_id)
    prior_scores = [
        r["quality_score"] for r in prior_runs
        if r.get("quality_score") is not None
    ]

    if not prior_scores:
        return False, "No prior scores to compare."

    best_prior = max(prior_scores)
    if current_score < best_prior - threshold:
        return True, (
            f"REGRESSION: current={current_score:.1f} vs "
            f"best_prior={best_prior:.1f} (delta={current_score - best_prior:+.1f})"
        )

    return False, (
        f"OK: current={current_score:.1f} vs "
        f"best_prior={best_prior:.1f} (delta={current_score - best_prior:+.1f})"
    )


# ---------------------------------------------------------------------------
# Literature Index (Cross-Run Deduplication)
# ---------------------------------------------------------------------------


def index_shortlisted_papers(
    run_id: str,
    papers: list[dict[str, Any]],
    *,
    index_file: Path = _LITERATURE_INDEX,
) -> int:
    """Append shortlisted papers to the persistent literature index.

    Returns count of new papers added (not already in index).
    """
    index_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing titles
    existing_titles: set[str] = set()
    if index_file.exists():
        for line in index_file.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                existing_titles.add(entry.get("title", "").strip().lower())
            except json.JSONDecodeError:
                continue

    new_count = 0
    with index_file.open("a", encoding="utf-8") as f:
        for paper in papers:
            title = paper.get("title", "").strip().lower()
            if title and title not in existing_titles:
                entry = {
                    "title": paper.get("title", ""),
                    "year": paper.get("year"),
                    "doi": paper.get("doi", ""),
                    "source": paper.get("source", ""),
                    "run_id": run_id,
                    "indexed_at": _utcnow_iso(),
                }
                f.write(json.dumps(entry) + "\n")
                existing_titles.add(title)
                new_count += 1

    logger.info(
        "Indexed %d new papers from run %s (%d total in index)",
        new_count, run_id, len(existing_titles),
    )
    return new_count


def get_indexed_paper_titles(
    *, index_file: Path = _LITERATURE_INDEX
) -> set[str]:
    """Return all paper titles in the persistent literature index."""
    if not index_file.exists():
        return set()
    titles: set[str] = set()
    for line in index_file.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            titles.add(entry.get("title", "").strip().lower())
        except json.JSONDecodeError:
            continue
    return titles
