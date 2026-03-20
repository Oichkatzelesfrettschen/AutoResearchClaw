from __future__ import annotations

import json
import logging
import math
import re
import time as _time
import uuid
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.hardware import HardwareProfile, detect_hardware, ensure_torch_available, is_metric_name
from researchclaw.llm import create_llm_client
from researchclaw.llm.client import LLMClient
from researchclaw.prompts import PromptManager
from researchclaw.literature.domain_queries import (
    get_domain_queries,
    get_experiment_fallback,
    is_null_result_topic,
    is_control_pair as is_ablation_control_pair,
)
from researchclaw.utils.thinking_tags import strip_thinking_tags
from researchclaw.pipeline.stages import (
    NEXT_STAGE,
    Stage,
    StageStatus,
    TransitionEvent,
    TransitionOutcome,
    advance,
    gate_required,
)
from researchclaw.pipeline.contracts import CONTRACTS, StageContract
from researchclaw.experiment.validator import (
    CodeValidation,
    format_issues_for_llm,
    validate_code,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain detection (extracted to _domain.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline._domain import (  # noqa: E402
    _DOMAIN_KEYWORDS,
    _detect_domain,
    _is_ml_domain,
)

# ---------------------------------------------------------------------------
# Shared helpers (extracted to _helpers.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline._helpers import (  # noqa: E402
    StageResult,
    _METACLAW_SKILLS_DIR,
    _SANDBOX_SAFE_PACKAGES,
    _STOP_WORDS,
    # We use our own custom _build_context_preamble
    # _build_context_preamble,
    _build_fallback_queries,
    # We use our own custom _chat_with_prompt
    # _chat_with_prompt,
    _collect_experiment_results,
    _collect_json_context,
    _default_hypotheses,
    _default_paper_outline,
    _default_quality_report,
    _detect_runtime_issues,
    _ensure_sandbox_deps,
    _extract_code_block,
    _extract_multi_file_blocks,
    _extract_paper_title,
    _extract_topic_keywords,
    _extract_yaml_block,
    _find_prior_file,
    _generate_framework_diagram_prompt,
    _generate_neurips_checklist,
    _get_evolution_overlay,
    _load_hardware_profile,
    _multi_perspective_generate,
    _parse_jsonl_rows,
    _parse_metrics_from_stdout,
    _read_prior_artifact,
    _safe_filename,
    _safe_json_loads,
    _synthesize_perspectives,
    _topic_constraint_block,
    _utcnow_iso,
    _write_jsonl,
    _write_stage_meta,
    reconcile_figure_refs,
)

# ---------------------------------------------------------------------------
# Stage implementations (extracted to stage_impls/)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._topic import (  # noqa: E402
    _execute_topic_init,
    _execute_problem_decompose,
)
from researchclaw.pipeline.stage_impls._literature import (  # noqa: E402
    _execute_search_strategy,
    _execute_literature_collect,
    _execute_literature_screen,
    _execute_knowledge_extract,
    _expand_search_queries,
)
from researchclaw.pipeline.stage_impls._synthesis import (  # noqa: E402
    _execute_synthesis,
    _execute_hypothesis_gen,
)
from researchclaw.pipeline.stage_impls._experiment_design import (  # noqa: E402
    _execute_experiment_design,
)
from researchclaw.pipeline.stage_impls._code_generation import (  # noqa: E402
    _execute_code_generation,
)
from researchclaw.pipeline.stage_impls._execution import (  # noqa: E402
    _execute_resource_planning,
    _execute_experiment_run,
    _execute_iterative_refine,
)
from researchclaw.pipeline.stage_impls._analysis import (  # noqa: E402
    _execute_result_analysis,
    _parse_decision,
    _execute_research_decision,
)
from researchclaw.pipeline.stage_impls._paper_writing import (  # noqa: E402
    _execute_paper_outline,
    _execute_paper_draft,
    _collect_raw_experiment_metrics,
    _write_paper_sections,
    _validate_draft_quality,
    _review_compiled_pdf,
    _check_ablation_effectiveness,
    _detect_result_contradictions,
    _BULLET_LENIENT_SECTIONS,
    _BALANCE_SECTIONS,
)
from researchclaw.pipeline.stage_impls._review_publish import (  # noqa: E402
    _execute_peer_review,
    _execute_paper_revision,
    _execute_quality_gate,
    _execute_knowledge_archive,
    _execute_export_publish,
    _execute_citation_verify,
    _sanitize_fabricated_data,
    _collect_experiment_evidence,
    _check_citation_relevance,
    _remove_bibtex_entries,
    _remove_citations_from_text,
)

# ---------------------------------------------------------------------------
# Helper overrides (wiring in custom logic to modular infrastructure)
# ---------------------------------------------------------------------------

def _build_context_preamble(
    config: RCConfig,
    run_dir: Path,
    *,
    include_goal: bool = False,
    include_hypotheses: bool = False,
    include_synthesis: bool = False,
    include_exp_plan: bool = False,
    include_analysis: bool = False,
    include_decision: bool = False,
    include_experiment_data: bool = False,
    include_kb: bool = False,
) -> str:
    parts = [
        "## Research Context",
        f"**Topic**: {config.research.topic}",
        f"**Domains**: {', '.join(config.research.domains) if config.research.domains else 'general'}",
    ]
    if include_goal:
        goal = _read_prior_artifact(run_dir, "goal.md")
        if goal:
            parts.append(f"\n### Goal\n{goal[:2200]}")
    if include_hypotheses:
        hyp = _read_prior_artifact(run_dir, "hypotheses.md")
        if hyp:
            parts.append(f"\n### Hypotheses\n{hyp[:2200]}")
    if include_synthesis:
        synthesis = _read_prior_artifact(run_dir, "synthesis.md")
        if synthesis:
            parts.append(f"\n### Synthesis\n{synthesis[:2200]}")
    if include_exp_plan:
        plan = _read_prior_artifact(run_dir, "exp_plan.yaml")
        if plan:
            parts.append(f"\n### Experiment Plan\n{plan[:2000]}")
    if include_analysis:
        analysis = _read_prior_artifact(run_dir, "analysis.md")
        if analysis:
            parts.append(f"\n### Result Analysis\n{analysis[:2500]}")
    if include_decision:
        decision = _read_prior_artifact(run_dir, "decision.md")
        if decision:
            parts.append(f"\n### Research Decision\n{decision[:1500]}")
    if include_experiment_data:
        hw_profile = _load_hardware_profile(run_dir)
        if hw_profile:
            hw_lines = ["### Hardware Environment"]
            for hk, hv in hw_profile.items():
                hw_lines.append(f"- **{hk}**: {hv}")
            parts.append("\n" + "\n".join(hw_lines))
        exp_summary = _read_prior_artifact(run_dir, "experiment_summary.json")
        if exp_summary:
            summary = _safe_json_loads(exp_summary, {})
            if isinstance(summary, dict) and summary.get("metrics_summary"):
                parts.append("\n### Experiment Results (Quantitative)")
                ms = summary["metrics_summary"]
                for mk, mv in ms.items():
                    if isinstance(mv, dict):
                        parts.append(
                            f"- **{mk}**: mean={mv.get('mean', '?')}, "
                            f"min={mv.get('min', '?')}, max={mv.get('max', '?')}, n={mv.get('count', '?')}"
                        )
                if summary.get("latex_table"):
                    parts.append(
                        f"\n### LaTeX Table\n```latex\n{summary['latex_table']}\n```"
                    )
    if include_kb:
        # Inject pre-existing knowledge base into the context preamble.
        # Priority: if a merged monograph exists in docs/, use it as the
        # single authoritative source (higher char cap).  Otherwise fall
        # back to the five individual KB files.
        _kb_root = getattr(config.knowledge_base, "root", "")
        _monograph_path = Path(_kb_root).parent / "MERGED_MONOGRAPH.md" if _kb_root else None
        _monograph_text = ""
        if _monograph_path and _monograph_path.is_file():
            try:
                _monograph_text = _monograph_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if _monograph_text:
            parts.append("\n## Knowledge Base (merged monograph -- single authoritative source)")
            parts.append(
                "IMPORTANT: The following monograph integrates ALL experimental results, "
                "claims, insights, formal proofs, and analysis from this project. Use "
                "these results directly rather than generating synthetic data or "
                "placeholder values."
            )
            parts.append(f"\n### MERGED_MONOGRAPH.md\n{_monograph_text[:12000]}")
        else:
            _kb_files = _read_kb_files(
                _kb_root,
                [
                    "experiments_summary.md",
                    "data_inventory.md",
                    "compute_inventory.md",
                    "claims_summary.md",
                    "insights_summary.md",
                ],
            )
            if _kb_files:
                parts.append("\n## Knowledge Base (pre-existing project results)")
                parts.append(
                    "IMPORTANT: The following files document REAL experimental results "
                    "and REAL data that already exist in this project. Use these results "
                    "directly rather than generating synthetic data or placeholder values."
                )
                for _kb_fname, _kb_text in _kb_files:
                    parts.append(f"\n### {_kb_fname}\n{_kb_text}")
    return "\n".join(parts)


def _chat_with_prompt(
    llm: LLMClient,
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    retries: int = 0,
    strip_thinking: bool = True,
) -> Any:
    """Send a chat request with optional retry on timeout/transient errors."""
    import time

    messages = [{"role": "user", "content": user}]
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            if json_mode:
                return llm.chat(messages, system=system, json_mode=True, max_tokens=max_tokens, strip_thinking=strip_thinking)
            return llm.chat(messages, system=system, max_tokens=max_tokens, strip_thinking=strip_thinking)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                delay = 2 ** (attempt + 1)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1,
                    1 + retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                raise last_exc from None
    raise last_exc  # type: ignore[misc]

# ---------------------------------------------------------------------------
# Executor mapping
# ---------------------------------------------------------------------------

_STAGE_EXECUTORS: dict[Stage, Callable[..., StageResult]] = {
    Stage.TOPIC_INIT: _execute_topic_init,
    Stage.PROBLEM_DECOMPOSE: _execute_problem_decompose,
    Stage.SEARCH_STRATEGY: _execute_search_strategy,
    Stage.LITERATURE_COLLECT: _execute_literature_collect,
    Stage.LITERATURE_SCREEN: _execute_literature_screen,
    Stage.KNOWLEDGE_EXTRACT: _execute_knowledge_extract,
    Stage.SYNTHESIS: _execute_synthesis,
    Stage.HYPOTHESIS_GEN: _execute_hypothesis_gen,
    Stage.EXPERIMENT_DESIGN: _execute_experiment_design,
    Stage.CODE_GENERATION: _execute_code_generation,
    Stage.RESOURCE_PLANNING: _execute_resource_planning,
    Stage.EXPERIMENT_RUN: _execute_experiment_run,
    Stage.ITERATIVE_REFINE: _execute_iterative_refine,
    Stage.RESULT_ANALYSIS: _execute_result_analysis,
    Stage.RESEARCH_DECISION: _execute_research_decision,
    Stage.PAPER_OUTLINE: _execute_paper_outline,
    Stage.PAPER_DRAFT: _execute_paper_draft,
    Stage.PEER_REVIEW: _execute_peer_review,
    Stage.PAPER_REVISION: _execute_paper_revision,
    Stage.QUALITY_GATE: _execute_quality_gate,
    Stage.KNOWLEDGE_ARCHIVE: _execute_knowledge_archive,
    Stage.EXPORT_PUBLISH: _execute_export_publish,
    Stage.CITATION_VERIFY: _execute_citation_verify,
}


def execute_stage(
    stage: Stage,
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    auto_approve_gates: bool = False,
) -> StageResult:
    """Execute one pipeline stage, validate outputs, and apply gate logic."""

    stage_dir = run_dir / f"stage-{int(stage):02d}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    _t_health_start = _time.monotonic()
    contract: StageContract = CONTRACTS[stage]

    if contract.input_files:
        for input_file in contract.input_files:
            found = _read_prior_artifact(run_dir, input_file)
            if found is None:
                result = StageResult(
                    stage=stage,
                    status=StageStatus.FAILED,
                    artifacts=(),
                    error=f"Missing input: {input_file} (required by {stage.name})",
                    decision="retry",
                )
                _write_stage_meta(stage_dir, stage, run_id, result)
                return result

    bridge = config.openclaw_bridge
    if bridge.use_message and config.notifications.on_stage_start:
        adapters.message.notify(
            config.notifications.channel,
            f"stage-{int(stage):02d}-start",
            f"Starting {stage.name}",
        )
    if bridge.use_memory:
        adapters.memory.append("stages", f"{run_id}:{int(stage)}:running")

    llm = None
    try:
        if config.llm.provider == "acp":
            llm = create_llm_client(config)
        else:
            candidate = LLMClient.from_rc_config(config)
            if candidate.config.base_url and candidate.config.api_key:
                llm = candidate
    except Exception as _llm_exc:  # noqa: BLE001
        logger.warning("LLM client creation failed: %s", _llm_exc)
        llm = None

    try:
        _ = advance(stage, StageStatus.PENDING, TransitionEvent.START)
        executor = _STAGE_EXECUTORS[stage]
        prompts = PromptManager(config.prompts.custom_file or None)  # type: ignore[attr-defined]
        try:
            result = executor(
                stage_dir, run_dir, config, adapters, llm=llm, prompts=prompts
            )
        except TypeError as exc:
            if "unexpected keyword argument 'prompts'" not in str(exc):
                raise
            result = executor(stage_dir, run_dir, config, adapters, llm=llm)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stage %s failed", stage.name)
        result = StageResult(
            stage=stage,
            status=StageStatus.FAILED,
            artifacts=(),
            error=str(exc),
            decision="retry",
        )

    if result.status == StageStatus.DONE:
        for output_file in contract.output_files:
            if output_file.endswith("/"):
                path = stage_dir / output_file.rstrip("/")
                if not path.is_dir() or not any(path.iterdir()):
                    result = StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error=f"Missing output directory: {output_file}",
                        decision="retry",
                        evidence_refs=result.evidence_refs,
                    )
                    break
            else:
                path = stage_dir / output_file
                if not path.exists() or path.stat().st_size == 0:
                    result = StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error=f"Missing or empty output: {output_file}",
                        decision="retry",
                        evidence_refs=result.evidence_refs,
                    )
                    break

    # --- MetaClaw PRM quality gate evaluation ---
    try:
        mc_bridge = getattr(config, "metaclaw_bridge", None)
        if (
            mc_bridge
            and getattr(mc_bridge, "enabled", False)
            and result.status == StageStatus.DONE
        ):
            mc_prm = getattr(mc_bridge, "prm", None)
            if mc_prm and getattr(mc_prm, "enabled", False):
                prm_stages = getattr(mc_prm, "gate_stages", (5, 9, 15, 20))
                if int(stage) in prm_stages:
                    from researchclaw.metaclaw_bridge.prm_gate import ResearchPRMGate

                    prm_gate = ResearchPRMGate.from_bridge_config(mc_prm)
                    if prm_gate is not None:
                        # Read stage output for PRM evaluation
                        output_text = ""
                        for art in result.artifacts:
                            art_path = stage_dir / art
                            if art_path.exists() and art_path.is_file():
                                try:
                                    output_text += art_path.read_text(encoding="utf-8")[:4000]
                                except (UnicodeDecodeError, OSError):
                                    pass
                        if output_text:
                            prm_score = prm_gate.evaluate_stage(int(stage), output_text)
                            logger.info(
                                "MetaClaw PRM score for stage %d: %.1f",
                                int(stage),
                                prm_score,
                            )
                            # Write PRM score to stage health
                            import json as _prm_json

                            prm_report = {
                                "stage": int(stage),
                                "prm_score": prm_score,
                                "model": prm_gate.model,
                                "votes": prm_gate.votes,
                            }
                            (stage_dir / "prm_score.json").write_text(
                                _prm_json.dumps(prm_report, indent=2),
                                encoding="utf-8",
                            )
                            # If PRM score is -1 (fail), mark stage as failed
                            if prm_score == -1.0:
                                logger.warning(
                                    "MetaClaw PRM rejected stage %d output",
                                    int(stage),
                                )
                                result = StageResult(
                                    stage=result.stage,
                                    status=StageStatus.FAILED,
                                    artifacts=result.artifacts,
                                    error="PRM quality gate: output below quality threshold",
                                    decision="retry",
                                    evidence_refs=result.evidence_refs,
                                )
    except Exception:  # noqa: BLE001
        logger.warning("MetaClaw PRM evaluation failed (non-blocking)")

    if gate_required(stage, config.security.hitl_required_stages):
        if auto_approve_gates:
            if bridge.use_memory:
                adapters.memory.append("gates", f"{run_id}:{int(stage)}:auto-approved")
        else:
            result = StageResult(
                stage=result.stage,
                status=StageStatus.BLOCKED_APPROVAL,
                artifacts=result.artifacts,
                error=result.error,
                decision="block",
                evidence_refs=result.evidence_refs,
            )
            if bridge.use_message and config.notifications.on_gate_required:
                adapters.message.notify(
                    config.notifications.channel,
                    f"gate-{int(stage):02d}",
                    f"Approval required for {stage.name}",
                )

    if bridge.use_memory:
        adapters.memory.append("stages", f"{run_id}:{int(stage)}:{result.status.value}")

    _write_stage_meta(stage_dir, stage, run_id, result)

    _t_health_end = _time.monotonic()
    stage_health = {
        "stage_id": f"{int(stage):02d}-{stage.name.lower()}",
        "run_id": run_id,
        "duration_sec": round(_t_health_end - _t_health_start, 2),
        "status": result.status.value,
        "artifacts_count": len(result.artifacts),
        "error": result.error,
        "timestamp": _utcnow_iso(),
    }
    try:
        (stage_dir / "stage_health.json").write_text(
            json.dumps(stage_health, indent=2), encoding="utf-8"
        )
    except OSError:
        pass

    return result
