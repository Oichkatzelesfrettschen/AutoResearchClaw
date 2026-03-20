"""Stages 18-23: Peer review, paper revision, quality gate, knowledge archive, export/publish, and citation verify."""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml  # noqa: F401 — available for downstream use

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline._domain import _detect_domain  # noqa: F401
from researchclaw.pipeline._helpers import (
    StageResult,
    _build_context_preamble,
    _chat_with_prompt,
    _collect_experiment_results,  # noqa: F401
    _default_quality_report,
    _extract_paper_title,
    _find_prior_file,
    _generate_framework_diagram_prompt,
    _generate_neurips_checklist,
    _get_evolution_overlay,
    _read_prior_artifact,
    _safe_json_loads,
    _topic_constraint_block,  # noqa: F401
    _utcnow_iso,
    reconcile_figure_refs,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers imported from executor.py (not yet moved to _helpers.py).
# Lazy-imported inside functions to avoid circular import when executor.py
# imports this module.
# ---------------------------------------------------------------------------


def _get_collect_raw_experiment_metrics():
    from researchclaw.pipeline.stage_impls._paper_writing import _collect_raw_experiment_metrics
    return _collect_raw_experiment_metrics


def _get_review_compiled_pdf():
    from researchclaw.pipeline.stage_impls._paper_writing import _review_compiled_pdf
    return _review_compiled_pdf


# ---------------------------------------------------------------------------
# _collect_experiment_evidence
# ---------------------------------------------------------------------------

def _collect_experiment_evidence(run_dir: Path) -> str:
    """Collect actual experiment parameters and results for peer review."""
    evidence_parts: list[str] = []

    # 1. Read experiment code to find actual trial count, methods used
    exp_dir = _read_prior_artifact(run_dir, "experiment/")
    if exp_dir and Path(exp_dir).is_dir():
        main_py = Path(exp_dir) / "main.py"
        if main_py.exists():
            code = main_py.read_text(encoding="utf-8")
            evidence_parts.append(f"### Actual Experiment Code (main.py)\n```python\n{code[:3000]}\n```")

    # 2. Read sandbox run results (actual metrics, runtime, stderr)
    runs_text = _read_prior_artifact(run_dir, "runs/")
    if runs_text and Path(runs_text).is_dir():
        for run_file in sorted(Path(runs_text).glob("*.json"))[:5]:
            payload = _safe_json_loads(run_file.read_text(encoding="utf-8"), {})
            if isinstance(payload, dict):
                summary = {
                    "metrics": payload.get("metrics"),
                    "elapsed_sec": payload.get("elapsed_sec"),
                    "timed_out": payload.get("timed_out"),
                }
                stderr = payload.get("stderr", "")
                if stderr:
                    summary["stderr_excerpt"] = stderr[:500]
                evidence_parts.append(
                    f"### Run Result: {run_file.name}\n```json\n{json.dumps(summary, indent=2)}\n```"
                )

    # 3. Read refinement log for actual iteration count
    refine_log_text = _read_prior_artifact(run_dir, "refinement_log.json")
    if refine_log_text:
        try:
            rlog = json.loads(refine_log_text)
            summary = {
                "iterations_executed": len(rlog.get("iterations", [])),
                "converged": rlog.get("converged"),
                "stop_reason": rlog.get("stop_reason"),
                "best_metric": rlog.get("best_metric"),
            }
            evidence_parts.append(
                f"### Refinement Summary\n```json\n{json.dumps(summary, indent=2)}\n```"
            )
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. Count actual number of experiment runs
    actual_run_count = 0
    for stage_subdir in sorted(run_dir.glob("stage-*/runs")):
        for rf in stage_subdir.glob("*.json"):
            if rf.name != "results.json":
                actual_run_count += 1
    if actual_run_count > 0:
        evidence_parts.append(
            f"### Actual Trial Count\n"
            f"**The experiment was executed {actual_run_count} time(s).** "
            f"If the paper claims a different number of trials, this is a CRITICAL discrepancy."
        )

    if not evidence_parts:
        return ""

    return (
        "\n\n## Actual Experiment Evidence\n"
        "Use the evidence below to verify the paper's methodology claims.\n\n"
        + "\n\n".join(evidence_parts)
    )


# ---------------------------------------------------------------------------
# Stage 18: Peer Review
# ---------------------------------------------------------------------------

def _execute_peer_review(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    draft = _read_prior_artifact(run_dir, "paper_draft.md") or ""
    experiment_evidence = _collect_experiment_evidence(run_dir)

    # Load draft quality warnings from Stage 17 (if available)
    _quality_suffix = ""
    _quality_json_path = _find_prior_file(run_dir, "draft_quality.json")
    if _quality_json_path and _quality_json_path.exists():
        try:
            _dq = json.loads(_quality_json_path.read_text(encoding="utf-8"))
            _dq_warnings = _dq.get("overall_warnings", [])
            if _dq_warnings:
                _quality_suffix = (
                    "\n\nAUTOMATED QUALITY ISSUES (flag these in your review):\n"
                    + "\n".join(f"- {w}" for w in _dq_warnings)
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            pass

    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "peer_review")
        sp = _pm.for_stage(
            "peer_review",
            evolution_overlay=_overlay,
            topic=config.research.topic,
            draft=draft,
            experiment_evidence=experiment_evidence,
        )
        _review_user = sp.user + _quality_suffix
        resp = _chat_with_prompt(
            llm,
            sp.system,
            _review_user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        reviews = resp.content
    else:
        reviews = """# Reviews

## Reviewer A
- Strengths: Clear problem statement.
- Weaknesses: Limited ablation details.
- Actionable revisions: Add uncertainty analysis and stronger baselines.

## Reviewer B
- Strengths: Reproducibility focus.
- Weaknesses: Discussion underdeveloped.
- Actionable revisions: Expand limitations and broader impact.
"""
    (stage_dir / "reviews.md").write_text(reviews, encoding="utf-8")
    return StageResult(
        stage=Stage.PEER_REVIEW,
        status=StageStatus.DONE,
        artifacts=("reviews.md",),
        evidence_refs=("stage-18/reviews.md",),
    )


# ---------------------------------------------------------------------------
# Stage 19: Paper Revision
# ---------------------------------------------------------------------------

def _execute_paper_revision(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    draft = _read_prior_artifact(run_dir, "paper_draft.md") or ""
    reviews = _read_prior_artifact(run_dir, "reviews.md") or ""
    draft_word_count = len(draft.split())

    # R4-2: Collect real metrics for anti-fabrication guard in revision
    # BUG-47: _collect_raw_experiment_metrics returns tuple[str, bool], must unpack
    _raw_metrics_tuple = _get_collect_raw_experiment_metrics()(run_dir)
    raw_metrics_revision = _raw_metrics_tuple[0] if isinstance(_raw_metrics_tuple, tuple) else (_raw_metrics_tuple or "")
    data_integrity_revision = ""
    if raw_metrics_revision:
        data_integrity_revision = (
            raw_metrics_revision
            + "\nDATA INTEGRITY: Do NOT add new numbers that are not in the "
            "experiment data above. If a reviewer asks for additional results "
            "you do not have, state 'Due to computational constraints, "
            "this analysis was not conducted' instead of fabricating data.\n"
        )

    if llm is not None:
        _pm = prompts or PromptManager()
        try:
            _ws_revision = _pm.block("writing_structure")
        except (KeyError, Exception):  # noqa: BLE001
            _ws_revision = ""
        # IMP-20/25/31/24: Load style blocks for revision prompt
        _rev_blocks: dict[str, str] = {}
        for _bname in ("academic_style_guide", "narrative_writing_rules",
                        "anti_hedging_rules", "anti_repetition_rules"):
            try:
                _rev_blocks[_bname] = _pm.block(_bname)
            except (KeyError, Exception):  # noqa: BLE001
                _rev_blocks[_bname] = ""
        # Load draft quality directives from Stage 17
        _quality_prefix = ""
        _quality_json_path = _find_prior_file(run_dir, "draft_quality.json")
        if _quality_json_path and _quality_json_path.exists():
            try:
                _dq = json.loads(_quality_json_path.read_text(encoding="utf-8"))
                _dq_directives = _dq.get("revision_directives", [])
                if _dq_directives:
                    _quality_prefix = (
                        "MANDATORY QUALITY FIXES (address ALL of these):\n"
                        + "\n".join(f"- {d}" for d in _dq_directives)
                        + "\n\n"
                    )
            except Exception:  # noqa: BLE001
                pass

        _overlay = _get_evolution_overlay(run_dir, "paper_revision")
        sp = _pm.for_stage(
            "paper_revision",
            evolution_overlay=_overlay,
            topic_constraint=_pm.block("topic_constraint", topic=config.research.topic),
            writing_structure=_ws_revision,
            draft=draft,
            reviews=_quality_prefix + reviews + data_integrity_revision,
            **_rev_blocks,
        )
        # R10-Fix2: Ensure max_tokens is sufficient for full paper revision
        revision_max_tokens = sp.max_tokens
        if revision_max_tokens and draft_word_count > 0:
            # ~1.5 tokens per word, 20% headroom
            min_tokens_needed = int(draft_word_count * 1.5 * 1.2)
            if revision_max_tokens < min_tokens_needed:
                revision_max_tokens = min_tokens_needed
                logger.info(
                    "Stage 19: Increased max_tokens from %d to %d to fit full paper revision",
                    sp.max_tokens,
                    revision_max_tokens,
                )

        # R10-Fix4: Retry on timeout for paper revision (critical stage)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=revision_max_tokens,
            retries=2,
        )
        revised = resp.content
        revised_word_count = len(revised.split())
        # Length guard: if revision is shorter than 80% of draft, retry once
        if draft_word_count > 500 and revised_word_count < int(draft_word_count * 0.8):
            logger.warning(
                "Paper revision (%d words) is shorter than draft (%d words). "
                "Retrying with stronger length enforcement.",
                revised_word_count,
                draft_word_count,
            )
            retry_user = (
                f"CRITICAL LENGTH REQUIREMENT: The draft is {draft_word_count} words. "
                f"Your revision MUST be at least {draft_word_count} words — ideally longer. "
                f"Do NOT summarize or condense ANY section. Copy each section verbatim "
                f"and ONLY make targeted improvements to address reviewer comments. "
                f"If a section has no reviewer comments, include it UNCHANGED.\n\n"
                + sp.user
            )
            resp2 = _chat_with_prompt(
                llm, sp.system, retry_user,
                json_mode=sp.json_mode, max_tokens=revision_max_tokens,
            )
            revised2 = resp2.content
            revised2_word_count = len(revised2.split())
            if revised2_word_count >= int(draft_word_count * 0.8):
                revised = revised2
            elif revised2_word_count > revised_word_count:
                # Retry improved but still not enough — use the longer version
                revised = revised2
                logger.warning(
                    "Retry improved (%d → %d words) but still shorter than draft (%d).",
                    revised_word_count,
                    revised2_word_count,
                    draft_word_count,
                )
            else:
                # Both attempts produced short output — preserve full original draft
                logger.warning(
                    "Retry also produced short output (%d words). "
                    "Falling back to FULL ORIGINAL DRAFT to prevent content loss.",
                    revised2_word_count,
                )
                # Extract useful revision points as appendix
                revision_words = revised.split()
                revision_summary = (
                    " ".join(revision_words[:500]) + "\n\n*(Revision summary truncated)*"
                    if len(revision_words) > 500
                    else revised
                )
                if revision_summary.strip():
                    # Save revision notes to internal file, not paper body
                    (stage_dir / "revision_notes_internal.md").write_text(
                        revision_summary, encoding="utf-8"
                    )
                revised = draft
    else:
        revised = draft
    (stage_dir / "paper_revised.md").write_text(revised, encoding="utf-8")
    return StageResult(
        stage=Stage.PAPER_REVISION,
        status=StageStatus.DONE,
        artifacts=("paper_revised.md",),
        evidence_refs=("stage-19/paper_revised.md",),
    )


# ---------------------------------------------------------------------------
# Stage 20: Quality Gate
# ---------------------------------------------------------------------------

def _execute_quality_gate(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    report: dict[str, Any] | None = None

    # BUG-25: Load experiment summary for cross-checking
    _exp_summary_text = _read_prior_artifact(run_dir, "experiment_summary.json") or ""
    _exp_summary = _safe_json_loads(_exp_summary_text, {}) if _exp_summary_text else {}
    _exp_failed = False
    if isinstance(_exp_summary, dict):
        _best_run = _exp_summary.get("best_run", {})
        if isinstance(_best_run, dict):
            _exp_failed = (
                _best_run.get("status") == "failed"
                and not _best_run.get("metrics")
            )
        # Also check if metrics_summary is empty
        if not _exp_summary.get("metrics_summary"):
            _exp_failed = True

    if llm is not None:
        _pm = prompts or PromptManager()
        # IMP-33: Evaluate the full paper instead of truncating to 12K chars.
        # Split into chunks if very long, but prefer sending the full text.
        paper_for_eval = revised[:40000] if len(revised) > 40000 else revised

        # BUG-25: Inject experiment status into quality gate prompt
        _exp_context = ""
        if _exp_summary and isinstance(_exp_summary, dict):
            _exp_status_keys = {
                k: _exp_summary.get(k) for k in (
                    "total_conditions", "total_metric_keys",
                    "metrics_summary",
                ) if _exp_summary.get(k) is not None
            }
            if _best_run := _exp_summary.get("best_run"):
                _exp_status_keys["best_run_status"] = (
                    _best_run.get("status") if isinstance(_best_run, dict) else str(_best_run)
                )
            _exp_context = (
                "\n\nExperiment summary (for cross-checking reported numbers):\n"
                + json.dumps(_exp_status_keys, indent=2, default=str)[:4000]
                + "\n\nCross-check: If the experiment status is 'failed' with "
                "empty metrics, any numerical results in tables constitute "
                "fabrication. Penalize severely.\n"
            )

        _overlay = _get_evolution_overlay(run_dir, "quality_gate")
        sp = _pm.for_stage(
            "quality_gate",
            evolution_overlay=_overlay,
            quality_threshold=str(config.research.quality_threshold),
            revised=paper_for_eval + _exp_context,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        parsed = _safe_json_loads(resp.content, {})
        if isinstance(parsed, dict):
            report = parsed
    # BUG-25: If experiment failed with no metrics, cap the quality score
    if report is not None and _exp_failed:
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)) and _orig_score > 3:
            report["score_1_to_10"] = min(_orig_score, 3.0)
            report.setdefault("weaknesses", []).append(
                "Experiment failed with no metrics — any reported numerical "
                "results are unsupported and likely fabricated."
            )
            logger.warning(
                "BUG-25: Experiment failed — capping quality score from %.1f to 3.0",
                _orig_score,
            )
    if report is None:
        report = _default_quality_report(config.research.quality_threshold)
    report.setdefault("generated", _utcnow_iso())
    (stage_dir / "quality_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    # T2.1: Enforce quality gate — fail if score below threshold
    score = report.get("score_1_to_10", 0)
    # BUG-R5-01: score can be string from LLM JSON — coerce to float
    if not isinstance(score, (int, float)):
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0
    verdict = report.get("verdict", "proceed")
    threshold = config.research.quality_threshold or 5.0

    # --- Fabrication flag: collect real metrics for Stage 22 sanitization ---
    _fabrication_info: dict[str, Any] = {
        "experiment_failed": _exp_failed,
        "quality_score": score,
        "real_metric_values": [],
    }
    if isinstance(_exp_summary, dict):
        # Collect ALL real numeric values from experiment_summary.json
        _cond_summaries = _exp_summary.get("condition_summaries", {})
        if isinstance(_cond_summaries, dict):
            for cond_name, cond_data in _cond_summaries.items():
                if not isinstance(cond_data, dict):
                    continue
                cond_status = cond_data.get("status", "")
                if cond_status == "failed":
                    continue  # skip failed conditions
                for k, v in cond_data.items():
                    if isinstance(v, (int, float)) and k not in (
                        "seed_count", "total_steps", "training_steps",
                    ):
                        _fabrication_info["real_metric_values"].append(
                            round(float(v), 4)
                        )
        _ms = _exp_summary.get("metrics_summary", {})
        if isinstance(_ms, dict):
            for _mk, _mv in _ms.items():
                if isinstance(_mv, dict):
                    for _stat in ("mean", "min", "max"):
                        _sv = _mv.get(_stat)
                        if isinstance(_sv, (int, float)):
                            _fabrication_info["real_metric_values"].append(
                                round(float(_sv), 4)
                            )
    _fabrication_info["has_real_data"] = bool(
        _fabrication_info["real_metric_values"]
    )
    _fabrication_info["fabrication_suspected"] = (
        _exp_failed and not _fabrication_info["has_real_data"]
    )
    # Phase 1: Enhanced fabrication detection via VerifiedRegistry
    # BUG-108: Also pass refinement_log so NaN best_metric is properly handled
    _rl20_candidates = sorted(run_dir.glob("stage-13*/refinement_log.json"), reverse=True)
    _rl20_path = _rl20_candidates[0] if _rl20_candidates else None
    _rl20: dict | None = None
    if _rl20_path and _rl20_path.is_file():
        try:
            _rl20 = json.loads(_rl20_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    try:
        from researchclaw.pipeline.verified_registry import VerifiedRegistry as _VR20
        _vr20 = _VR20.from_experiment(_exp_summary, refinement_log=_rl20) if isinstance(_exp_summary, dict) else None
        if _vr20:
            _fabrication_info["verified_values_count"] = len(_vr20.values)
            _fabrication_info["verified_conditions"] = sorted(_vr20.condition_names)
    except Exception:
        pass
    (stage_dir / "fabrication_flags.json").write_text(
        json.dumps(_fabrication_info, indent=2), encoding="utf-8"
    )

    if isinstance(score, (int, float)) and score < threshold:
        if config.research.graceful_degradation:
            logger.warning(
                "Quality gate DEGRADED: score %.1f < threshold %.1f — "
                "continuing with sanitization (graceful_degradation=True)",
                score, threshold,
            )
            # Write degradation signal for downstream stages
            signal = {
                "score": score,
                "threshold": threshold,
                "verdict": verdict,
                "weaknesses": report.get("weaknesses", []),
                "generated": _utcnow_iso(),
            }
            (run_dir / "degradation_signal.json").write_text(
                json.dumps(signal, indent=2), encoding="utf-8"
            )
            return StageResult(
                stage=Stage.QUALITY_GATE,
                status=StageStatus.DONE,
                artifacts=("quality_report.json",),
                evidence_refs=("stage-20/quality_report.json",),
                decision="degraded",
            )
        logger.warning(
            "Quality gate FAILED: score %.1f < threshold %.1f (verdict=%s)",
            score, threshold, verdict,
        )
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=("quality_report.json", "fabrication_flags.json"),
            evidence_refs=("stage-20/quality_report.json",),
            error=f"Quality score {score:.1f}/10 below threshold {threshold:.1f}. "
                  f"Paper needs revision before export.",
        )

    logger.info(
        "Quality gate PASSED: score %.1f >= threshold %.1f",
        score, threshold,
    )
    return StageResult(
        stage=Stage.QUALITY_GATE,
        status=StageStatus.DONE,
        artifacts=("quality_report.json", "fabrication_flags.json"),
        evidence_refs=("stage-20/quality_report.json",),
    )


# ---------------------------------------------------------------------------
# Stage 21: Knowledge Archive
# ---------------------------------------------------------------------------

def _execute_knowledge_archive(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    decision = _read_prior_artifact(run_dir, "decision.md") or ""
    preamble = _build_context_preamble(config, run_dir, include_goal=True)
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "knowledge_archive")
        sp = _pm.for_stage(
            "knowledge_archive",
            evolution_overlay=_overlay,
            preamble=preamble,
            decision=decision,
            analysis=analysis,
            revised=revised[:15000],
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        archive = resp.content
    else:
        archive = f"""# Knowledge Archive

## Lessons Learned
- Preserve strict metric reporting protocol.
- Keep refinement logs aligned with code changes.

## Reproducibility
- Include exact experiment script and schedule.
- Capture run-level JSON metrics.

## Future Work
- Extend robustness and external validity checks.

Generated: {_utcnow_iso()}
"""
    (stage_dir / "archive.md").write_text(archive, encoding="utf-8")

    files: list[str] = []
    for stage_subdir in sorted(run_dir.glob("stage-*")):
        for artifact in sorted(stage_subdir.rglob("*")):
            if artifact.is_file() and artifact != (stage_dir / "bundle_index.json"):
                files.append(str(artifact.relative_to(run_dir)))
    index = {
        "run_id": run_dir.name,
        "generated": _utcnow_iso(),
        "artifact_count": len(files),
        "artifacts": files,
    }
    (stage_dir / "bundle_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    return StageResult(
        stage=Stage.KNOWLEDGE_ARCHIVE,
        status=StageStatus.DONE,
        artifacts=("archive.md", "bundle_index.json"),
        evidence_refs=("stage-21/archive.md", "stage-21/bundle_index.json"),
    )


# ---------------------------------------------------------------------------
# _sanitize_fabricated_data helper
# ---------------------------------------------------------------------------

def _sanitize_fabricated_data(
    paper: str,
    run_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Replace unverified numerical data in markdown tables with '---'.

    Loads experiment_summary.json as ground truth, extracts all verified
    metric values, then scans markdown tables in Results/Experiment sections.
    Numbers not matching any verified value (within 1% relative tolerance)
    are replaced with ``---``.

    Returns (sanitized_paper, sanitization_report).
    """
    import re as _re_san

    # --- 1. Build verified values set from experiment_summary.json ---
    verified_values: set[float] = set()
    exp_path = run_dir / "stage-14" / "experiment_summary.json"
    if not exp_path.exists():
        # Try other common locations
        for candidate in sorted(run_dir.glob("stage-14*/experiment_summary.json")):
            exp_path = candidate
            break

    if exp_path.exists():
        try:
            exp_data = json.loads(exp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            exp_data = {}

        def _collect_numbers(obj: Any, depth: int = 0) -> None:
            if depth > 10:
                return
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                import math as _math_vv
                if _math_vv.isfinite(float(obj)):
                    verified_values.add(float(obj))
            elif isinstance(obj, dict):
                for v in obj.values():
                    _collect_numbers(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    _collect_numbers(v, depth + 1)

        # Extract from well-known keys
        for key in (
            "metrics_summary", "condition_summaries", "best_run",
            "condition_metrics", "conditions", "ablation_results",
        ):
            if key in exp_data:
                _collect_numbers(exp_data[key])

    if not verified_values:
        report: dict[str, Any] = {
            "sanitized": False,
            "reason": "no verified values found in experiment_summary.json",
            "tables_processed": 0,
            "numbers_replaced": 0,
        }
        return paper, report

    def _is_verified(num: float) -> bool:
        """Check if num matches any verified value within 1% relative tolerance.

        BUG-R5-20: Also checks percentage/decimal cross-matching
        (e.g., 73.42 in paper vs 0.7342 in experiment, or vice versa).
        """
        for v in verified_values:
            if v == 0.0:
                if abs(num) < 1e-9:
                    return True
            elif abs(num - v) / abs(v) <= 0.01:
                return True
            # Cross-match: num might be percentage form of v (or vice versa)
            elif v != 0.0 and abs(num / 100.0 - v) / abs(v) <= 0.01:
                return True
            elif v != 0.0 and abs(num - v * 100.0) / abs(v * 100.0) <= 0.01:
                return True
        return False

    # --- 2. Find and sanitize markdown tables ---
    # Match markdown table blocks (header + separator + data rows)
    table_pat = _re_san.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)+"  # one or more pipe-delimited lines
        r")",
        _re_san.MULTILINE,
    )
    # Match numbers in table cells (integers, decimals, percentages, scientific)
    num_pat = _re_san.compile(
        r"(?<![a-zA-Z_])"  # not preceded by letter/underscore
        r"(-?\d+\.?\d*(?:[eE][+-]?\d+)?)"
        r"(%?)"  # optional percent
        r"(?![a-zA-Z_])"  # not followed by letter/underscore
    )

    numbers_replaced = 0
    numbers_kept = 0
    tables_processed = 0
    replaced_values: list[str] = []

    def _sanitize_table(match: _re_san.Match[str]) -> str:
        nonlocal numbers_replaced, numbers_kept, tables_processed
        table_text = match.group(0)
        lines = table_text.split("\n")

        # Check if this looks like a results/experiment table
        # (heuristic: has a separator row with dashes)
        has_separator = any(
            _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            for line in lines
        )
        if not has_separator:
            return table_text

        tables_processed += 1
        sanitized_lines: list[str] = []
        for i, line in enumerate(lines):
            # Skip header row and separator row
            is_separator = bool(
                _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            )
            is_header = i == 0  # first line is typically the header
            if is_separator or is_header:
                sanitized_lines.append(line)
                continue

            def _replace_num(m: _re_san.Match[str]) -> str:
                nonlocal numbers_replaced, numbers_kept
                num_str = m.group(1)
                pct = m.group(2)
                try:
                    val = float(num_str)
                except ValueError:
                    return m.group(0)
                if _is_verified(val):
                    numbers_kept += 1
                    return m.group(0)
                numbers_replaced += 1
                replaced_values.append(num_str + pct)
                return "---"

            sanitized_lines.append(num_pat.sub(_replace_num, line))
        return "\n".join(sanitized_lines)

    sanitized = table_pat.sub(_sanitize_table, paper)

    report = {
        "sanitized": numbers_replaced > 0,
        "tables_processed": tables_processed,
        "numbers_replaced": numbers_replaced,
        "numbers_kept": numbers_kept,
        "verified_values_count": len(verified_values),
        "replaced_samples": replaced_values[:20],
        "generated": _utcnow_iso(),
    }
    return sanitized, report


# ---------------------------------------------------------------------------
# Stage 22: Export & Publish
# ---------------------------------------------------------------------------

def _execute_export_publish(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "export_publish")
        sp = _pm.for_stage("export_publish", evolution_overlay=_overlay, revised=revised)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        final_paper = resp.content
        # Content guard: reject LLM output that truncates the paper
        if revised and len(final_paper) < 0.6 * len(revised):
            logger.warning(
                "Stage 22: LLM output is %.0f%% of input length — using original",
                100 * len(final_paper) / max(len(revised), 1),
            )
            final_paper = revised
    else:
        final_paper = revised
    if not final_paper.strip():
        final_paper = "# Final Paper\n\nNo content generated."

    # --- Always-on fabrication sanitization (Phase 1 anti-fabrication) ---
    # Back up pre-sanitized version
    (stage_dir / "paper_presanitized.md").write_text(
        final_paper, encoding="utf-8"
    )

    # Sanitize unverified data in tables — always-on, not just degraded mode
    final_paper, _san_report = _sanitize_fabricated_data(
        final_paper, run_dir
    )
    (stage_dir / "sanitization_report.json").write_text(
        json.dumps(_san_report, indent=2), encoding="utf-8"
    )
    if _san_report.get("numbers_replaced", 0) > 0:
        logger.info(
            "Stage 22: Fabrication sanitization — %d numbers replaced, %d kept",
            _san_report.get("numbers_replaced", 0),
            _san_report.get("numbers_kept", 0),
        )

    # Graceful degradation: insert notice only when quality gate was degraded
    _degradation_signal_path = run_dir / "degradation_signal.json"
    if _degradation_signal_path.exists():
        try:
            _deg_signal = json.loads(
                _degradation_signal_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            _deg_signal = {}

        # Insert degradation notice after abstract
        _deg_score = _deg_signal.get("score", "N/A")
        _deg_threshold = _deg_signal.get("threshold", "N/A")
        _deg_notice = (
            "\n\n> **Note:** This paper was produced in degraded mode. "
            f"Quality gate score ({_deg_score}/{_deg_threshold}) was below "
            "threshold. Unverified numerical results in tables have been "
            "replaced with `---` and require independent verification.\n\n"
        )
        # Try to insert after ## Abstract section
        _abstract_markers = ["## Abstract\n", "# Abstract\n"]
        _notice_inserted = False
        for _marker in _abstract_markers:
            if _marker in final_paper:
                _marker_end = final_paper.index(_marker) + len(_marker)
                # Find the end of the abstract paragraph
                _next_section = final_paper.find("\n## ", _marker_end)
                _next_heading = final_paper.find("\n# ", _marker_end)
                _insert_pos = min(
                    p for p in (_next_section, _next_heading)
                    if p > 0
                ) if any(p > 0 for p in (_next_section, _next_heading)) else len(final_paper)
                final_paper = (
                    final_paper[:_insert_pos]
                    + _deg_notice
                    + final_paper[_insert_pos:]
                )
                _notice_inserted = True
                break
        if not _notice_inserted:
            # Fallback: prepend to paper
            final_paper = _deg_notice + final_paper

        logger.info(
            "Stage 22: Applied degraded-mode notice (score=%s, threshold=%s)",
            _deg_score, _deg_threshold,
        )

    # IMP-3: Deduplicate "due to computational constraints" — keep at most 1
    import re as _re_imp3
    _CONSTRAINT_PAT = _re_imp3.compile(
        r"[Dd]ue to computational constraints", _re_imp3.IGNORECASE
    )
    _matches = list(_CONSTRAINT_PAT.finditer(final_paper))
    if len(_matches) > 1:
        # Keep only the first occurrence; remove subsequent ones by
        # deleting the enclosing sentence.
        for m in reversed(_matches[1:]):
            # Find sentence boundaries around the match
            start = final_paper.rfind(".", 0, m.start())
            start = start + 1 if start >= 0 else m.start()
            end = final_paper.find(".", m.end())
            end = end + 1 if end >= 0 else m.end()
            sentence = final_paper[start:end].strip()
            if sentence:
                final_paper = final_paper[:start] + final_paper[end:]
        final_paper = re.sub(r"[^\S\n]{2,}", " ", final_paper)
        logger.info(
            "Stage 22: Removed %d duplicate 'computational constraints' "
            "disclaimers",
            len(_matches) - 1,
        )

    # IMP-19 Layer 2: Ensure at least figures are referenced in the paper
    import re as _re_fig
    chart_files = []
    for _chart_src_dir in [stage_dir / "charts", run_dir / "stage-14" / "charts"]:
        if _chart_src_dir.is_dir():
            chart_files.extend(sorted(_chart_src_dir.glob("*.png")))
    if chart_files and "![" not in final_paper:
        # Distribute figures to relevant sections based on filename keywords
        _fig_placement: dict[str, list[str]] = {
            "method": [],       # architecture, method, model, pipeline diagrams
            "result": [],       # experiment, comparison, ablation charts
            "intro": [],        # concept, overview, illustration
        }
        _fig_counter = 0
        for cf in chart_files[:6]:
            _fig_counter += 1
            stem_lower = cf.stem.lower()
            label = cf.stem.replace("_", " ").title()
            fig_md = f"![Figure {_fig_counter}: {label}](charts/{cf.name})"
            if any(k in stem_lower for k in ("architecture", "model", "pipeline", "method", "flowchart")):
                _fig_placement["method"].append(fig_md)
            elif any(k in stem_lower for k in ("experiment", "comparison", "ablation", "result", "metric")):
                _fig_placement["result"].append(fig_md)
            elif any(k in stem_lower for k in ("concept", "overview", "illustration", "threat", "attack")):
                _fig_placement["intro"].append(fig_md)
            else:
                _fig_placement["result"].append(fig_md)  # default to results

        # Insert figures at relevant section boundaries
        _section_markers = {
            "method": ["## Method", "## Methodology", "## Approach", "## Framework",
                        "## 3. Method", "## 3 Method"],
            "result": ["## Results", "## Experiments", "## Evaluation",
                        "## 5. Results", "## 4. Experiments", "## 5 Results"],
            "intro": ["## Related Work", "## Background", "## 2. Related",
                       "## 2 Related Work"],
        }
        _total_inserted = 0
        for category, figs in _fig_placement.items():
            if not figs:
                continue
            fig_block = "\n\n" + "\n\n".join(figs) + "\n\n"
            inserted = False
            for marker in _section_markers.get(category, []):
                if marker in final_paper:
                    # Insert BEFORE the marker section (so figure appears at end of previous section)
                    final_paper = final_paper.replace(marker, fig_block + marker, 1)
                    inserted = True
                    _total_inserted += len(figs)
                    break
            if not inserted:
                # Fallback: insert before Conclusion/Limitations/Discussion
                for fallback in ["## Conclusion", "## Limitations", "## Discussion"]:
                    if fallback in final_paper:
                        final_paper = final_paper.replace(fallback, fig_block + fallback, 1)
                        inserted = True
                        _total_inserted += len(figs)
                        break
            if not inserted:
                final_paper += fig_block
                _total_inserted += len(figs)

        logger.info(
            "IMP-19: Injected %d figure references into paper_final.md (distributed across sections)",
            _total_inserted,
        )

    # IMP-24: Detect excessive number repetition
    _numbers_found = _re_fig.findall(r"\b\d+\.\d{2,}\b", final_paper)
    _num_counts = Counter(_numbers_found)
    _repeated = {n: c for n, c in _num_counts.items() if c > 3}
    if _repeated:
        logger.warning(
            "IMP-24: Numbers repeated >3 times: %s",
            _repeated,
        )

    (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")

    # --- Legacy fabrication sanitization (disabled — superseded by Phase 1 _sanitize_fabricated_data above) ---
    # Kept but guarded: Phase 1 always-on sanitization handles this now.
    # Only run if Phase 1 was somehow skipped (should never happen).
    _fab_flags_text = _read_prior_artifact(run_dir, "fabrication_flags.json") or ""
    _fab_flags = _safe_json_loads(_fab_flags_text, {}) if _fab_flags_text else {}
    if (
        isinstance(_fab_flags, dict)
        and _fab_flags.get("fabrication_suspected")
        and _san_report.get("numbers_replaced", 0) == 0  # Phase 1 didn't run/replace
    ):
        import re as _re_fab
        _real_vals = set()
        for rv in _fab_flags.get("real_metric_values", []):
            if isinstance(rv, (int, float)) and math.isfinite(rv):
                _real_vals.add(str(round(rv, 4)))
                _real_vals.add(str(round(rv, 2)))
                _real_vals.add(str(round(rv, 1)))
                if rv == int(rv):
                    _real_vals.add(str(int(rv)))

        def _sanitize_number(m: _re_fab.Match) -> str:  # type: ignore[name-defined]
            """Replace fabricated numbers with '--' but keep real ones."""
            num_str = m.group(0)
            # Keep the number if it matches any known real metric value
            try:
                num_val = float(num_str)
                if not math.isfinite(num_val):
                    return "--"
                rounded_strs = {
                    str(round(num_val, 4)),
                    str(round(num_val, 2)),
                    str(round(num_val, 1)),
                    *(
                        [str(int(num_val))] if num_val == int(num_val) else []
                    ),
                }
                if rounded_strs & _real_vals:
                    return num_str  # real value — keep it
            except (ValueError, OverflowError):
                return num_str
            return "--"

        # Only sanitize numbers in Results/Experiments/Evaluation/Ablation sections
        _result_section_pat = _re_fab.compile(
            r"(##\s*(?:\d+\.?\s*)?(?:Results|Experiments|Evaluation|Ablation"
            r"|Experimental Results|Quantitative).*?)(?=\n##\s|\Z)",
            _re_fab.DOTALL | _re_fab.IGNORECASE,
        )
        _sanitized_count = 0

        def _sanitize_section(sec_match: _re_fab.Match) -> str:  # type: ignore[name-defined]
            nonlocal _sanitized_count
            section_text = sec_match.group(0)
            # Replace decimal numbers (e.g., 73.42, 0.891) but NOT integers
            # that are likely structural (year, section number, figure number)
            def _replace_in_section(m: _re_fab.Match) -> str:  # type: ignore[name-defined]
                nonlocal _sanitized_count
                result = _sanitize_number(m)
                if result == "--":
                    _sanitized_count += 1
                return result
            return _re_fab.sub(
                r"\b\d+\.\d{1,6}\b", _replace_in_section, section_text
            )

        final_paper = _result_section_pat.sub(_sanitize_section, final_paper)

        if _sanitized_count > 0:
            logger.warning(
                "Stage 22: Fabrication sanitization — blanked %d unsupported "
                "numbers in Results sections (experiment had no real metrics)",
                _sanitized_count,
            )
            # Rewrite the sanitized paper
            (stage_dir / "paper_final.md").write_text(
                final_paper, encoding="utf-8"
            )

    # Initialize artifacts list
    artifacts = ["paper_final.md"]
    # F2.7: Post-process citations — [cite_key] → \cite{cite_key}
    # and copy final references.bib to export stage
    _ay_map: dict[str, str] = {}  # BUG-102: author-year → cite_key map
    bib_text = _read_prior_artifact(run_dir, "references.bib")
    if bib_text:
        # Replace [cite_key] patterns in the final paper with \cite{cite_key}
        # Collect all valid cite_keys from the bib file
        import re as _re

        valid_keys = set(_re.findall(r"@\w+\{([^,]+),", bib_text))

        # BUG-102: Recover author-year citations → [cite_key] format.
        # When Stage 19 (paper_revision) converts [cite_key] to [Author et al., 2024],
        # the downstream regex can't match them. Build a reverse map from bib entries.
        def _build_author_year_map(bib: str, keys: set[str]) -> dict[str, str]:
            """Build mapping from author-year patterns to cite_keys.

            Returns dict like:
              "Raissi et al., 2019" → "raissi2019physicsinformed"
              "Tavella and Randall, 2000" → "tavella2000pricing"
            """
            mapping: dict[str, str] = {}
            # Parse each bib entry for author + year
            entry_pat = _re.compile(
                r"@\w+\{([^,]+),\s*(.*?)\n\}", _re.DOTALL
            )
            for m in entry_pat.finditer(bib):
                key = m.group(1).strip()
                if key not in keys:
                    continue
                body = m.group(2)
                # Extract author field
                author_m = _re.search(
                    r"author\s*=\s*[\{\"](.*?)[\}\"]", body, _re.IGNORECASE
                )
                year_m = _re.search(
                    r"year\s*=\s*[\{\"]?(\d{4})[\}\"]?", body, _re.IGNORECASE
                )
                if not author_m or not year_m:
                    continue
                author_raw = author_m.group(1).strip()
                year = year_m.group(1)
                # Parse author names (split on " and ")
                authors = [a.strip() for a in _re.split(r"\s+and\s+", author_raw)]
                # Extract last names
                last_names = []
                for a in authors:
                    if "," in a:
                        last_names.append(a.split(",")[0].strip())
                    else:
                        parts = a.split()
                        last_names.append(parts[-1] if parts else a)
                if not last_names:
                    continue
                # Generate author-year patterns:
                # 1 author: "Smith, 2024"
                # 2 authors: "Smith and Jones, 2024"
                # 3+ authors: "Smith et al., 2024"
                if len(last_names) == 1:
                    patterns = [f"{last_names[0]}, {year}"]
                elif len(last_names) == 2:
                    patterns = [
                        f"{last_names[0]} and {last_names[1]}, {year}",
                        f"{last_names[0]} \\& {last_names[1]}, {year}",
                    ]
                else:
                    patterns = [
                        f"{last_names[0]} et al., {year}",
                        f"{last_names[0]} et al. {year}",
                    ]
                    # Also add "Smith and Jones, 2024" for first two authors
                    patterns.append(
                        f"{last_names[0]} and {last_names[1]}, {year}"
                    )
                for pat in patterns:
                    mapping[pat] = key
            return mapping

        _ay_map = _build_author_year_map(bib_text, valid_keys)
        if _ay_map:
            # Count how many author-year citations exist in the paper
            _ay_found = 0
            for _ay_pat in _ay_map:
                if _ay_pat in final_paper:
                    _ay_found += 1
            if _ay_found > 0:
                logger.info(
                    "Stage 22: Found %d author-year citation patterns — "
                    "converting back to [cite_key] format.",
                    _ay_found,
                )
                # Sort by longest pattern first to avoid partial matches
                for _ay_pat in sorted(_ay_map, key=len, reverse=True):
                    _ay_key = _ay_map[_ay_pat]
                    # Match [Author et al., 2024] or [Author and Jones, 2024; ...]
                    # Handle single-citation brackets
                    final_paper = final_paper.replace(
                        f"[{_ay_pat}]", f"[{_ay_key}]"
                    )
                    # Handle within multi-citation brackets [A et al., 2020; B et al., 2021]
                    # Replace the author-year segment only inside [...] brackets
                    final_paper = _re.sub(
                        r'\[([^\]]*?)' + _re.escape(_ay_pat) + r'([^\]]*?)\]',
                        lambda _m: '[' + _m.group(1) + _ay_key + _m.group(2) + ']',
                        final_paper,
                    )
                # Fix multi-key brackets: [key1; key2] → [key1, key2]
                # (author-year uses semicolons, cite-keys use commas)
                def _fix_semicolon_cites(m_sc: _re.Match[str]) -> str:
                    inner = m_sc.group(1)
                    # Only convert if ALL segments look like cite keys
                    parts = [p.strip() for p in inner.split(";")]
                    _ck = r"[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*"
                    if all(_re.fullmatch(_ck, p) for p in parts):
                        return "[" + ", ".join(parts) + "]"
                    return m_sc.group(0)
                final_paper = _re.sub(
                    r"\[([^\]]+;[^\]]+)\]", _fix_semicolon_cites, final_paper
                )
                (stage_dir / "paper_final.md").write_text(
                    final_paper, encoding="utf-8"
                )

        # R10-Fix4: Citation cross-validation
        cited_keys_in_paper = set(_re.findall(r"\[([a-zA-Z]+\d{4}[a-zA-Z0-9_-]*)\]", final_paper))
        if valid_keys and cited_keys_in_paper:
            invalid_keys = cited_keys_in_paper - valid_keys
            if invalid_keys:
                logger.warning(
                    "Stage 22: Found %d citation keys in paper not in references.bib: %s",
                    len(invalid_keys),
                    ", ".join(sorted(invalid_keys)[:20]),
                )
                # IMP-29: Silently remove invalid citations instead of
                # leaving ugly [?key:NOT_IN_BIB] markers in the output.
                for bad_key in invalid_keys:
                    final_paper = final_paper.replace(f"[{bad_key}]", "")
                # Clean up whitespace artifacts from removed citations
                import re as _re_imp29
                final_paper = _re_imp29.sub(r"  +", " ", final_paper)
                final_paper = _re_imp29.sub(r" ([.,;:)])", r"\1", final_paper)
                (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")
                (stage_dir / "invalid_citations.json").write_text(
                    json.dumps(sorted(invalid_keys), indent=2), encoding="utf-8"
                )
                artifacts.append("invalid_citations.json")

        final_paper_latex = final_paper  # default: no citation conversion
        if valid_keys:
            _CITE_KEY_PAT = r"[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9]*"

            # Step 1: Convert multi-key brackets [key1, key2] → \cite{key1, key2}
            def _replace_multi_cite(m: _re.Match[str]) -> str:
                keys = [k.strip() for k in m.group(1).split(",")]
                matched = [k for k in keys if k in valid_keys]
                if matched:
                    return "\\cite{" + ", ".join(matched) + "}"
                return m.group(0)

            final_paper_latex = _re.sub(
                rf"\[({_CITE_KEY_PAT}(?:\s*,\s*{_CITE_KEY_PAT})+)\]",
                _replace_multi_cite,
                final_paper,
            )

            # Step 2: Convert single-key brackets [key] → \cite{key}
            def _replace_cite(m: _re.Match[str]) -> str:
                key = m.group(1)
                if key in valid_keys:
                    return f"\\cite{{{key}}}"
                return m.group(0)

            final_paper_latex = _re.sub(
                rf"\[({_CITE_KEY_PAT})\]", _replace_cite, final_paper_latex
            )

            # Step 3: Merge adjacent \cite{a} \cite{b} → \cite{a, b}
            def _merge_adjacent_cites(m: _re.Match[str]) -> str:
                keys = _re.findall(r"\\cite\{([^}]+)\}", m.group(0))
                return "\\cite{" + ", ".join(keys) + "}"

            final_paper_latex = _re.sub(
                r"\\cite\{[^}]+\}(?:\s*\\cite\{[^}]+\})+",
                _merge_adjacent_cites,
                final_paper_latex,
            )

            (stage_dir / "paper_final_latex.md").write_text(
                final_paper_latex, encoding="utf-8"
            )
            artifacts.append("paper_final_latex.md")
        # IMP-1: Prune uncited bibliography entries — keep only keys
        # that actually appear in the paper text (bracket or \cite form).
        if valid_keys:
            _all_cited: set[str] = set()
            # Bracket-format citations [key]
            _all_cited.update(
                _re.findall(r"\[([a-zA-Z]+\d{4}[a-zA-Z0-9_-]*)\]", final_paper)
            )
            # \cite{key, key2} format (original + latex-converted)
            for _src in (
                final_paper,
                final_paper_latex,
            ):
                for _cm in _re.finditer(r"\\cite\{([^}]+)\}", _src):
                    _all_cited.update(
                        k.strip() for k in _cm.group(1).split(",")
                    )
            uncited_keys = valid_keys - _all_cited
            if uncited_keys:
                bib_text = _remove_bibtex_entries(bib_text, uncited_keys)
                logger.info(
                    "Stage 22: Pruned %d uncited bibliography entries "
                    "(kept %d)",
                    len(uncited_keys),
                    len(valid_keys) - len(uncited_keys),
                )

        # Write final references.bib
        (stage_dir / "references.bib").write_text(bib_text, encoding="utf-8")
        artifacts.append("references.bib")
        logger.info(
            "Stage 22: Exported references.bib with %d entries",
            len(valid_keys) if valid_keys else 0,
        )

    # Conference template: generate .tex file
    try:
        from researchclaw.templates import get_template, markdown_to_latex

        tpl = get_template(config.export.target_conference)
        # Use the latex-citation-processed version if available
        tex_source = final_paper_latex
        # Append NeurIPS-style checklist if target is a ML conference
        if tpl.name in ("neurips_2024", "neurips_2025", "icml_2025", "icml_2026",
                         "iclr_2025", "iclr_2026"):
            _has_exp = bool(_read_prior_artifact(run_dir, "experiment_summary.json"))
            _checklist = _generate_neurips_checklist(
                has_experiments=_has_exp,
                has_code=True,
            )
            if "NeurIPS Paper Checklist" not in tex_source:
                tex_source = tex_source.rstrip() + "\n\n" + _checklist
        tex_content = markdown_to_latex(
            tex_source,
            tpl,
            title=_extract_paper_title(tex_source),
            authors=config.export.authors,
            bib_file=config.export.bib_file,
            bib_entries=_ay_map or None,
        )
        (stage_dir / "paper.tex").write_text(tex_content, encoding="utf-8")
        artifacts.append("paper.tex")
        logger.info(
            "Stage 22: Generated paper.tex for %s (%d chars)",
            tpl.display_name,
            len(tex_content),
        )
        # --- Phase 1 anti-fabrication: verify paper against VerifiedRegistry ---
        try:
            from researchclaw.pipeline.paper_verifier import verify_paper as _verify_paper
            _exp_sum_text = _read_prior_artifact(run_dir, "experiment_summary.json")
            if _exp_sum_text:
                _exp_sum_for_vr = _safe_json_loads(_exp_sum_text, {})
                if isinstance(_exp_sum_for_vr, dict) and _exp_sum_for_vr:
                    from researchclaw.pipeline.verified_registry import (
                        VerifiedRegistry as _VR22,
                    )
                    # BUG-108: Pass refinement_log for complete verification
                    _rl22_candidates = sorted(run_dir.glob("stage-13*/refinement_log.json"), reverse=True)
                    _rl22_path = _rl22_candidates[0] if _rl22_candidates else None
                    _rl22: dict | None = None
                    if _rl22_path and _rl22_path.is_file():
                        try:
                            _rl22 = json.loads(_rl22_path.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError):
                            pass
                    _vr22 = _VR22.from_experiment(_exp_sum_for_vr, refinement_log=_rl22)
                    _vresult = _verify_paper(tex_content, _vr22)
                    (stage_dir / "paper_verification.json").write_text(
                        json.dumps({
                            "passed": _vresult.passed,
                            "severity": _vresult.severity,
                            "total_checked": _vresult.total_numbers_checked,
                            "total_verified": _vresult.total_numbers_verified,
                            "strict_violations": _vresult.strict_violations,
                            "lenient_violations": _vresult.lenient_violations,
                            "fabrication_rate": round(_vresult.fabrication_rate, 4),
                            "unverified_numbers": [
                                {"value": u.value, "line": u.line_number,
                                 "section": u.section, "in_table": u.in_table}
                                for u in _vresult.unverified_numbers[:20]
                            ],
                            "fabricated_conditions": [
                                {"name": fc.name, "line": fc.line_number}
                                for fc in _vresult.fabricated_conditions
                            ],
                            "config_warnings": getattr(_vresult, "config_warnings", []),
                            "summary": _vresult.summary,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    logger.info(
                        "Stage 22: Paper verification — %s (%d checked, %d verified, "
                        "%d strict violations, fabrication_rate=%.1f%%)",
                        _vresult.severity,
                        _vresult.total_numbers_checked,
                        _vresult.total_numbers_verified,
                        _vresult.strict_violations,
                        _vresult.fabrication_rate * 100,
                    )
        except Exception as _pv_exc:
            logger.debug("Stage 22: Paper verification skipped: %s", _pv_exc)

        # BUG-23 P1: Enforce REJECT verdict — sanitize unverified numbers
        if "_vresult" in dir() and hasattr(_vresult, "severity") and _vresult.severity == "REJECT":
            logger.warning(
                "Stage 22: Paper REJECTED by verifier (fabrication_rate=%.1f%%, "
                "%d strict violations). Sanitizing unverified numbers.",
                _vresult.fabrication_rate * 100,
                _vresult.strict_violations,
            )
            # Replace unverified numbers in strict sections/tables with "---"
            _sanitized_tex = tex_content
            for _uv in sorted(_vresult.unverified_numbers, key=lambda u: -u.line_number):
                # Only sanitize strict-section / in-table numbers
                _uv_section_lower = (_uv.section or "").lower()
                _uv_is_strict = any(
                    s in _uv_section_lower
                    for s in ("results", "experiment", "evaluation",
                              "ablation", "comparison", "analysis")
                )
                if _uv_is_strict or _uv.in_table:
                    _lines = _sanitized_tex.split("\n")
                    if 0 < _uv.line_number <= len(_lines):
                        _orig_line = _lines[_uv.line_number - 1]
                        _val_str = str(_uv.value)
                        # Try common representations
                        for _rep in (
                            f"{_uv.value:.4f}".rstrip("0").rstrip("."),
                            f"{_uv.value:.3f}",
                            f"{_uv.value:.2f}",
                            f"{_uv.value:.1f}",
                            f"{_uv.value:g}",
                            _val_str,
                        ):
                            if _rep in _orig_line:
                                _lines[_uv.line_number - 1] = _orig_line.replace(
                                    _rep, "---", 1,
                                )
                                break
                        _sanitized_tex = "\n".join(_lines)
            if _sanitized_tex != tex_content:
                tex_content = _sanitized_tex
                (stage_dir / "paper.tex").write_text(tex_content, encoding="utf-8")
                logger.info("Stage 22: Sanitized paper.tex — replaced unverified numbers with '---'")

        # Copy bundled style files alongside paper.tex
        for sf in tpl.get_style_files():
            import shutil as _shutil_sty
            _shutil_sty.copy2(sf, stage_dir / sf.name)

        # --- Pre-compilation: copy charts and fix figure paths ---
        # BUG-R41-12: Charts MUST be available before compile_latex(),
        # otherwise \includegraphics references fail → "Float(s) lost".
        try:
            chart_dir = stage_dir / "charts"
            chart_dir.mkdir(parents=True, exist_ok=True)
            charts: list[Path] = []

            # Copy FigureAgent charts from stage-14 (any version)
            _fa_charts_found = False
            for _fa_dir in sorted(run_dir.glob("stage-14*/charts"), reverse=True):
                _fa_pngs = list(_fa_dir.glob("fig_*.png"))
                if _fa_pngs:
                    import shutil
                    for _fa_png in _fa_pngs:
                        dest = chart_dir / _fa_png.name
                        shutil.copy2(_fa_png, dest)
                        charts.append(dest)
                    _fa_charts_found = True
                    logger.info(
                        "Stage 22: Copied %d FigureAgent charts from %s",
                        len(_fa_pngs), _fa_dir,
                    )
                    break

            # Generate structured charts from visualize.py
            from researchclaw.experiment.visualize import generate_all_charts
            _metric_dir = getattr(config.experiment, "metric_direction", "minimize")
            _viz_charts = generate_all_charts(
                run_dir,
                chart_dir,
                metric_key=config.experiment.metric_key,
                metric_direction=_metric_dir,
            )
            charts.extend(_viz_charts)

            if charts:
                artifacts.append("charts/")
                logger.info("Stage 22: Generated %d chart(s) total", len(charts))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chart generation failed: %s", exc)

        # BUG-99: Fix \includegraphics paths that don't match actual chart files
        try:
            reconcile_figure_refs(stage_dir / "paper.tex", stage_dir / "charts")
        except Exception as _fig_exc:  # noqa: BLE001
            logger.debug("Stage 22: Figure path validation skipped: %s", _fig_exc)

        # BUG-R41-12: Remove figure blocks referencing files that still don't exist
        try:
            tex_path = stage_dir / "paper.tex"
            if tex_path.exists():
                from researchclaw.templates.compiler import remove_missing_figures
                _tex_text = tex_path.read_text(encoding="utf-8")
                _fixed_tex, _removed_figs = remove_missing_figures(_tex_text, stage_dir)
                if _removed_figs:
                    tex_path.write_text(_fixed_tex, encoding="utf-8")
                    logger.warning(
                        "Stage 22: Removed %d figure block(s) with missing images: %s",
                        len(_removed_figs), _removed_figs,
                    )
        except Exception as _rmf_exc:  # noqa: BLE001
            logger.debug("Stage 22: remove_missing_figures skipped: %s", _rmf_exc)

        # Compile verification
        try:
            from researchclaw.templates.compiler import compile_latex
            _compile_result = compile_latex(stage_dir / "paper.tex", max_attempts=2)
            if _compile_result.success:
                logger.info("Stage 22: LaTeX compilation verification PASSED")
                artifacts.append("paper.pdf")
                # PDF-as-reviewer: LLM-based visual review of compiled PDF
                _pdf_path = stage_dir / "paper.pdf"
                if _pdf_path.exists() and llm is not None:
                    try:
                        _pdf_review = _get_review_compiled_pdf()(
                            _pdf_path, llm, config.research.topic
                        )
                        if _pdf_review:
                            (stage_dir / "pdf_review.json").write_text(
                                json.dumps(_pdf_review, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                            artifacts.append("pdf_review.json")
                            _pdf_score = _pdf_review.get("overall_score", 0)
                            if _pdf_score < 5:
                                logger.warning(
                                    "Stage 22: PDF visual review score %d/10 — %s",
                                    _pdf_score,
                                    _pdf_review.get("summary", ""),
                                )
                            else:
                                logger.info(
                                    "Stage 22: PDF visual review score %d/10",
                                    _pdf_score,
                                )
                    except Exception as _pdf_exc:  # noqa: BLE001
                        logger.debug("Stage 22: PDF review skipped: %s", _pdf_exc)
                # Post-compilation quality checks
                try:
                    from researchclaw.templates.compiler import check_compiled_quality
                    _qc = check_compiled_quality(stage_dir / "paper.tex")
                    if _qc.warnings_summary:
                        logger.warning(
                            "Stage 22: Quality checks: %s",
                            "; ".join(_qc.warnings_summary),
                        )
                    (stage_dir / "compilation_quality.json").write_text(
                        json.dumps({
                            "page_count": _qc.page_count,
                            "unresolved_refs": _qc.unresolved_refs,
                            "unresolved_cites": _qc.unresolved_cites,
                            "overfull_hboxes": len(_qc.overfull_hboxes),
                            "orphan_figures": _qc.orphan_figures,
                            "orphan_labels": _qc.orphan_labels,
                            "warnings": _qc.warnings_summary,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    artifacts.append("compilation_quality.json")
                    # BUG-27: Warn if page count exceeds limit
                    _page_limit = 10
                    if _qc.page_count and _qc.page_count > _page_limit:
                        logger.warning(
                            "BUG-27: Paper is %d pages (limit %d). "
                            "Consider tightening content in revision.",
                            _qc.page_count, _page_limit,
                        )
                except Exception as _qc_exc:  # noqa: BLE001
                    logger.debug("Stage 22: Quality checks skipped: %s", _qc_exc)
            else:
                logger.warning("Stage 22: LaTeX compilation verification FAILED: %s", _compile_result.errors[:3])
                # Add compilation failure comment to .tex
                _tex_path = stage_dir / "paper.tex"
                if _tex_path.exists():
                    _tex_content = _tex_path.read_text(encoding="utf-8")
                    if "% WARNING: Compilation failed" not in _tex_content:
                        _tex_content = (
                            "% WARNING: Compilation failed. Errors:\n"
                            + "".join(f"% {e}\n" for e in _compile_result.errors[:5])
                            + _tex_content
                        )
                        _tex_path.write_text(_tex_content, encoding="utf-8")
        except Exception as _compile_exc:  # noqa: BLE001
            logger.debug("Stage 22: Compile verification skipped: %s", _compile_exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LaTeX generation skipped: %s", exc)

    # (Charts, BUG-99 path fix, and remove_missing_figures are now handled
    #  BEFORE compile_latex() — see "Pre-compilation" block above.)

    # --- Code packaging: multi-file directory or single file ---
    exp_final_dir_path = _read_prior_artifact(run_dir, "experiment_final/")
    if exp_final_dir_path and Path(exp_final_dir_path).is_dir():
        import ast

        code_dir = stage_dir / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        all_code_combined = ""
        code_file_names: list[str] = []
        for src in sorted(Path(exp_final_dir_path).glob("*.py")):
            (code_dir / src.name).write_bytes(src.read_bytes())
            all_code_combined += src.read_text(encoding="utf-8") + "\n"
            code_file_names.append(src.name)

        # Detect dependencies from all files
        detected: set[str] = set()
        known_packages = {
            "numpy": "numpy",
            "torch": "torch",
            "tensorflow": "tensorflow",
            "sklearn": "scikit-learn",
            "scikit-learn": "scikit-learn",
            "scipy": "scipy",
            "pandas": "pandas",
            "matplotlib": "matplotlib",
            "seaborn": "seaborn",
            "transformers": "transformers",
            "datasets": "datasets",
            "jax": "jax",
        }
        try:
            tree = ast.parse(all_code_combined)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in known_packages:
                            detected.add(known_packages[top])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    if top in known_packages:
                        detected.add(known_packages[top])
        except SyntaxError:
            pass

        requirements = sorted(detected)
        (code_dir / "requirements.txt").write_text(
            "\n".join(requirements) + ("\n" if requirements else ""),
            encoding="utf-8",
        )

        paper_title = _extract_paper_title(final_paper)
        file_list_md = "\n".join(f"- `{f}`" for f in code_file_names)
        readme = (
            f"# Code Package for {paper_title}\n\n"
            "## Description\n"
            "This directory contains the experiment project used for the paper.\n\n"
            "## Project Files\n"
            f"{file_list_md}\n\n"
            "## How to Run\n"
            "`python main.py`\n\n"
            "## Dependencies\n"
            "Install dependencies with `pip install -r requirements.txt` if needed.\n"
        )
        (code_dir / "README.md").write_text(readme, encoding="utf-8")
        artifacts.append("code/")
        logger.info(
            "Stage 22: Packaged multi-file code release (%d files, %d deps)",
            len(code_file_names),
            len(requirements),
        )
    else:
        # Backward compat: single-file packaging
        code_payload = _read_prior_artifact(run_dir, "experiment_final.py")
        if not code_payload:
            code_payload = _read_prior_artifact(run_dir, "experiment.py")
        if code_payload:
            import ast

            code_dir = stage_dir / "code"
            code_dir.mkdir(parents=True, exist_ok=True)
            (code_dir / "experiment.py").write_text(code_payload, encoding="utf-8")

            detected_single: set[str] = set()
            known_packages_single = {
                "numpy": "numpy",
                "torch": "torch",
                "tensorflow": "tensorflow",
                "sklearn": "scikit-learn",
                "scikit-learn": "scikit-learn",
                "scipy": "scipy",
                "pandas": "pandas",
                "matplotlib": "matplotlib",
                "seaborn": "seaborn",
                "transformers": "transformers",
                "datasets": "datasets",
                "jax": "jax",
            }
            try:
                tree = ast.parse(code_payload)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            top = alias.name.split(".")[0]
                            if top in known_packages_single:
                                detected_single.add(known_packages_single[top])
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        top = node.module.split(".")[0]
                        if top in known_packages_single:
                            detected_single.add(known_packages_single[top])
            except SyntaxError:
                pass

            requirements = sorted(detected_single)
            (code_dir / "requirements.txt").write_text(
                "\n".join(requirements) + ("\n" if requirements else ""),
                encoding="utf-8",
            )
            paper_title = _extract_paper_title(final_paper)
            readme = (
                f"# Code Package for {paper_title}\n\n"
                "## Description\n"
                "This directory contains the final experiment script used for the paper.\n\n"
                "## How to Run\n"
                "`python experiment.py`\n\n"
                "## Dependencies\n"
                "Install dependencies with `pip install -r requirements.txt` if needed.\n"
            )
            (code_dir / "README.md").write_text(readme, encoding="utf-8")
            artifacts.append("code/")
            logger.info(
                "Stage 22: Packaged single-file code release with %d deps",
                len(requirements),
            )
    # WS-5.5: Generate framework diagram prompt for methodology section
    try:
        _framework_prompt = _generate_framework_diagram_prompt(
            final_paper, config, llm=llm
        )
        if _framework_prompt:
            _chart_dir = stage_dir / "charts"
            _chart_dir.mkdir(parents=True, exist_ok=True)
            (_chart_dir / "framework_diagram_prompt.md").write_text(
                _framework_prompt, encoding="utf-8"
            )
            logger.info("Stage 22: Generated framework diagram prompt → charts/framework_diagram_prompt.md")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Stage 22: Framework diagram prompt generation skipped: %s", exc)

    return StageResult(
        stage=Stage.EXPORT_PUBLISH,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-22/{a}" for a in artifacts),
    )


# ---------------------------------------------------------------------------
# Citation helpers
# ---------------------------------------------------------------------------

def _check_citation_relevance(
    llm: Any,
    topic: str,
    results: list[Any],
) -> dict[str, float | None]:
    """Use LLM to assess relevance of each citation to the research topic.

    Returns a dict mapping cite_key → relevance score (0.0–1.0).
    Processes citations in batches of 30 to handle large bibliographies.
    """
    citation_lines = []
    for cr in results:
        citation_lines.append(f"- [{cr.cite_key}] \"{cr.title}\"")
    if not citation_lines:
        return {}

    all_scores: dict[str, float] = {}
    _BATCH_SIZE = 30

    for batch_start in range(0, len(citation_lines), _BATCH_SIZE):
        batch = citation_lines[batch_start:batch_start + _BATCH_SIZE]
        citations_text = "\n".join(batch)

        prompt = (
            f"Research topic: {topic}\n\n"
            f"Rate the relevance of each citation to the research topic "
            f"on a scale of 0.0 to 1.0.\n"
            f"Return ONLY a JSON object mapping cite_key to relevance score.\n"
            f"Example: {{\"smith2020\": 0.9, \"jones2019\": 0.2}}\n\n"
            f"Citations:\n{citations_text}"
        )

        try:
            resp = llm.chat(
                [{"role": "user", "content": prompt}],
                system="You assess citation relevance. Return only valid JSON.",
                json_mode=True,
            )
            parsed = _safe_json_loads(resp.content, {})
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(v, (int, float)):
                        all_scores[k] = max(0.0, min(1.0, float(v)))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Citation relevance check failed for batch %d–%d, skipping",
                batch_start, batch_start + len(batch),
            )

    return all_scores


def _remove_bibtex_entries(bib_text: str, keys_to_remove: set[str]) -> str:
    """Remove BibTeX entries whose keys are in *keys_to_remove*."""
    kept: list[str] = []
    for m in re.finditer(r"@\w+\{([^,]+),", bib_text):
        key = m.group(1).strip()
        if key in keys_to_remove:
            continue
        # Find the full entry (from @ to the next @ or end)
        start = m.start()
        # Find balanced braces
        depth = 0
        end = start
        for i in range(start, len(bib_text)):
            if bib_text[i] == "{":
                depth += 1
            elif bib_text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            kept.append(bib_text[start:end])
    return "\n\n".join(kept) + "\n" if kept else ""


def _remove_citations_from_text(text: str, keys_to_remove: set[str]) -> str:
    """Remove \\cite{key} and [key] references for specified citation keys."""

    # Handle multi-key LaTeX cites: \cite{a,b,c} → filter keys inside braces
    def _filter_cite(m: re.Match[str]) -> str:
        keys = [k.strip() for k in m.group(1).split(",")]
        kept = [k for k in keys if k not in keys_to_remove]
        if not kept:
            return ""
        return f"\\cite{{{','.join(kept)}}}"

    text = re.sub(r"\\cite\{([^}]+)\}", _filter_cite, text)

    # Markdown: [key]
    for key in keys_to_remove:
        text = re.sub(rf"\[{re.escape(key)}\]", "", text)
    return text


# ---------------------------------------------------------------------------
# Stage 23: Citation Verify
# ---------------------------------------------------------------------------

def _execute_citation_verify(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    from researchclaw.literature.verify import (
        VerifyStatus,
        annotate_paper_hallucinations,
        filter_verified_bibtex,
        verify_citations,
    )

    bib_text = _read_prior_artifact(run_dir, "references.bib") or ""
    paper_text = _read_prior_artifact(run_dir, "paper_final.md") or ""

    if not bib_text.strip():
        report_data = {
            "summary": {
                "total": 0,
                "verified": 0,
                "suspicious": 0,
                "hallucinated": 0,
                "skipped": 0,
                "integrity_score": 1.0,
            },
            "results": [],
            "note": "No references.bib found — nothing to verify.",
        }
        (stage_dir / "verification_report.json").write_text(
            json.dumps(report_data, indent=2), encoding="utf-8"
        )
        (stage_dir / "references_verified.bib").write_text(
            "% No references to verify\n", encoding="utf-8"
        )
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.DONE,
            artifacts=("verification_report.json", "references_verified.bib"),
            evidence_refs=(
                "stage-23/verification_report.json",
                "stage-23/references_verified.bib",
            ),
        )

    s2_api_key = getattr(config.llm, "s2_api_key", "") or ""

    from researchclaw.literature.verify import parse_bibtex_entries
    _n_entries = len(parse_bibtex_entries(bib_text))
    logger.info(
        "[citation-verify] Verifying %d references "
        "(DOI→CrossRef > OpenAlex > arXiv > S2)…",
        _n_entries,
    )
    report = verify_citations(bib_text, s2_api_key=s2_api_key)
    logger.info(
        "[citation-verify] Done: %d verified, %d suspicious, "
        "%d hallucinated, %d skipped (integrity: %.0f%%)",
        report.verified,
        report.suspicious,
        report.hallucinated,
        report.skipped,
        report.integrity_score * 100,
    )

    # --- Relevance check: assess topical relevance of verified citations ---
    if llm is not None and report.results:
        relevance_scores = _check_citation_relevance(
            llm, config.research.topic, report.results
        )
        for cr in report.results:
            score = relevance_scores.get(cr.cite_key)
            if score is not None:
                cr.relevance_score = score

    # FIX-5: Filter low-relevance citations and enforce hard cap
    RELEVANCE_THRESHOLD = 0.5
    MAX_CITATIONS = 60
    low_relevance_keys: set[str] = set()
    for cr in report.results:
        if cr.relevance_score is not None and cr.relevance_score < RELEVANCE_THRESHOLD:
            low_relevance_keys.add(cr.cite_key)

    # Hard cap: if still above MAX_CITATIONS after relevance filter, drop lowest
    # BUG-07 fix: Unscored citations (relevance_score=None) default to 0.7
    # because they passed API verification and are likely relevant.
    # Previously they defaulted to 0.0 which caused mass-deletion.
    _DEFAULT_RELEVANCE = 0.7
    remaining = [
        cr for cr in report.results
        if cr.cite_key not in low_relevance_keys
        and cr.status != VerifyStatus.HALLUCINATED
    ]
    if len(remaining) > MAX_CITATIONS:
        remaining.sort(
            key=lambda c: c.relevance_score if c.relevance_score is not None else _DEFAULT_RELEVANCE,
        )
        overflow = remaining[:len(remaining) - MAX_CITATIONS]
        for cr in overflow:
            low_relevance_keys.add(cr.cite_key)
        logger.info(
            "Stage 23: Hard cap applied, dropping %d additional low-relevance citations",
            len(overflow),
        )

    if low_relevance_keys:
        logger.info(
            "Stage 23: Filtering %d low-relevance citations (threshold=%.1f, cap=%d): %s",
            len(low_relevance_keys),
            RELEVANCE_THRESHOLD,
            MAX_CITATIONS,
            ", ".join(sorted(list(low_relevance_keys)[:20])),
        )

    (stage_dir / "verification_report.json").write_text(
        json.dumps(report.to_dict(), indent=2), encoding="utf-8"
    )

    verified_bib = filter_verified_bibtex(bib_text, report, include_suspicious=True)
    # Remove low-relevance entries from BibTeX
    if low_relevance_keys:
        verified_bib = _remove_bibtex_entries(verified_bib, low_relevance_keys)

    # BUG-26: If verification stripped >50% of entries (e.g. due to rate limiting),
    # fall back to the original bib to avoid breaking the paper's references
    original_count = len(re.findall(r"@\w+\{", bib_text))
    verified_count = len(re.findall(r"@\w+\{", verified_bib))
    if original_count > 0 and verified_count < original_count * 0.5:
        logger.warning(
            "Stage 23: Verification stripped %d→%d entries (>50%% loss). "
            "Keeping original bib to avoid breaking references.",
            original_count, verified_count,
        )
        verified_bib = bib_text

    # IMP-1: Also prune uncited entries from verified bib
    if paper_text.strip():
        _vbib_keys = set(re.findall(r"@\w+\{([^,]+),", verified_bib))
        _cited_in_paper: set[str] = set()
        _cited_in_paper.update(
            re.findall(r"\[([a-zA-Z]+\d{4}[a-zA-Z0-9_-]*)\]", paper_text)
        )
        for _cm in re.finditer(r"\\cite\{([^}]+)\}", paper_text):
            _cited_in_paper.update(
                k.strip() for k in _cm.group(1).split(",")
            )
        _uncited_vbib = _vbib_keys - _cited_in_paper
        if _uncited_vbib:
            verified_bib = _remove_bibtex_entries(verified_bib, _uncited_vbib)
            logger.info(
                "Stage 23: Pruned %d uncited entries from verified bib "
                "(kept %d)",
                len(_uncited_vbib),
                len(_vbib_keys) - len(_uncited_vbib),
            )

    # BUG-100: If all entries were filtered out (low-relevance + uncited pruning),
    # write a comment instead of an empty file to avoid "Missing or empty output" error.
    if not verified_bib.strip():
        verified_bib = "% All citations were filtered out during verification\n"
        logger.warning(
            "Stage 23: All BibTeX entries filtered out — writing placeholder"
        )

    (stage_dir / "references_verified.bib").write_text(verified_bib, encoding="utf-8")

    artifacts = ["verification_report.json", "references_verified.bib"]

    if paper_text.strip():
        annotated = annotate_paper_hallucinations(paper_text, report)
        # Remove \cite{} and [cite_key] references for low-relevance entries
        if low_relevance_keys:
            annotated = _remove_citations_from_text(annotated, low_relevance_keys)
        (stage_dir / "paper_final_verified.md").write_text(annotated, encoding="utf-8")
        artifacts.append("paper_final_verified.md")

    logger.info(
        "Stage 23 citation verify: %d total, %d verified, %d suspicious, "
        "%d hallucinated, %d skipped (integrity=%.1f%%)",
        report.total,
        report.verified,
        report.suspicious,
        report.hallucinated,
        report.skipped,
        report.integrity_score * 100,
    )

    return StageResult(
        stage=Stage.CITATION_VERIFY,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-23/{a}" for a in artifacts),
    )
