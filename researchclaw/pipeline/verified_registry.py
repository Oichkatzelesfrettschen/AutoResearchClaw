"""Verified Value Registry — ground truth for all experiment-sourced numbers.

Builds a whitelist of numeric values, condition names, and training config
from ``experiment_summary.json`` and ``refinement_log.json``.  Used by
``paper_verifier.py`` and ``results_table_builder.py`` to ensure that
generated papers contain ONLY numbers grounded in real experiment data.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Infrastructure metric keys — allowed in paper without verification
_INFRA_KEYS: set[str] = {
    "elapsed_sec",
    "total_elapsed_seconds",
    "TIME_ESTIMATE",
    "SEED_COUNT",
    "time_budget_sec",
    "condition_count",
    "total_runs",
    "total_conditions",
    "total_metric_keys",
    "stopped_early",
}

# Metric key patterns for per-seed results (e.g. "DQN/0/metric")
_PER_SEED_PATTERN = re.compile(r"^(.+)/(\d+)/(.+)$")


@dataclass
class ConditionResult:
    """Aggregated results for one experimental condition."""

    name: str
    per_seed_values: dict[int, float] = field(default_factory=dict)
    mean: float | None = None
    std: float | None = None
    n_seeds: int = 0
    aggregate_metric: float | None = None  # The condition-level metric

    def compute_stats(self) -> None:
        """Compute mean and std from per-seed values."""
        vals = [v for v in self.per_seed_values.values() if _is_finite(v)]
        self.n_seeds = len(vals)
        if not vals:
            return
        self.mean = sum(vals) / len(vals)
        if len(vals) >= 2:
            variance = sum((v - self.mean) ** 2 for v in vals) / (len(vals) - 1)
            self.std = math.sqrt(variance)
        else:
            self.std = 0.0


@dataclass
class VerifiedRegistry:
    """Registry of all numbers grounded in experiment data."""

    values: dict[float, str] = field(default_factory=dict)
    condition_names: set[str] = field(default_factory=set)
    conditions: dict[str, ConditionResult] = field(default_factory=dict)
    primary_metric: float | None = None
    primary_metric_std: float | None = None
    metric_direction: str = "maximize"  # "maximize" or "minimize"
    training_config: dict[str, Any] = field(default_factory=dict)

    def add_value(self, value: float, source: str) -> None:
        """Register a verified numeric value with its provenance."""
        if not _is_finite(value):
            return
        self.values[value] = source
        # Also register common transformations
        self._add_variants(value, source)

    def _add_variants(self, value: float, source: str) -> None:
        """Register rounding variants and percentage conversions."""
        # Rounded variants (2, 3, 4 decimal places)
        for dp in (1, 2, 3, 4):
            rounded = round(value, dp)
            if rounded != value and rounded not in self.values:
                self.values[rounded] = f"{source} (rounded to {dp}dp)"

        # Percentage conversion: if value is in [0, 1], also register value*100
        if 0.0 < abs(value) <= 1.0:
            pct = value * 100.0
            if pct not in self.values:
                self.values[pct] = f"{source} (×100)"
                for dp in (1, 2, 3, 4):
                    pct_r = round(pct, dp)
                    if pct_r not in self.values:
                        self.values[pct_r] = f"{source} (×100, {dp}dp)"

        # If value > 1 and could be a percentage, also register value/100
        if abs(value) > 1.0:
            frac = value / 100.0
            if frac not in self.values:
                self.values[frac] = f"{source} (÷100)"

    def is_verified(self, number: float, tolerance: float = 0.01) -> bool:
        """Check if *number* matches any verified value within relative tolerance."""
        if not _is_finite(number):
            return False
        for v in self.values:
            if v == 0.0:
                if abs(number) < 1e-6:
                    return True
            elif abs(number - v) / max(abs(v), 1e-9) <= tolerance:
                return True
        return False

    def lookup(self, number: float, tolerance: float = 0.01) -> str | None:
        """Return the source description if *number* is verified, else None."""
        if not _is_finite(number):
            return None
        for v, src in self.values.items():
            if v == 0.0:
                if abs(number) < 1e-6:
                    return src
            elif abs(number - v) / max(abs(v), 1e-9) <= tolerance:
                return src
        return None

    def verify_condition(self, name: str) -> bool:
        """Check if condition name was actually run."""
        return name in self.condition_names

    @classmethod
    def from_experiment(
        cls,
        experiment_summary: dict,
        refinement_log: dict | None = None,
        *,
        metric_direction: str = "maximize",
    ) -> VerifiedRegistry:
        """Build registry from experiment artifacts.

        Parameters
        ----------
        experiment_summary:
            Parsed ``experiment_summary.json``.
        refinement_log:
            Parsed ``refinement_log.json`` (optional, provides richer per-seed data).
        metric_direction:
            ``"maximize"`` or ``"minimize"`` — used for best-result detection.
        """
        reg = cls(metric_direction=metric_direction)

        # --- 1. Extract condition-level and per-seed metrics ---
        best_run = experiment_summary.get("best_run", {})
        metrics = best_run.get("metrics", {})

        # Parse per-seed structure: "CondName/seed/metric_key" → value
        for key, value in metrics.items():
            if not isinstance(value, (int, float)) or not _is_finite(value):
                continue
            if key in _INFRA_KEYS:
                reg.training_config[key] = value
                continue

            reg.add_value(value, f"best_run.metrics.{key}")

            m = _PER_SEED_PATTERN.match(key)
            if m:
                cond_name, seed_str, _metric_name = m.group(1), m.group(2), m.group(3)
                seed_idx = int(seed_str)
                if cond_name not in reg.conditions:
                    reg.conditions[cond_name] = ConditionResult(name=cond_name)
                reg.conditions[cond_name].per_seed_values[seed_idx] = value
                reg.condition_names.add(cond_name)

        # --- 2. Extract condition_summaries ---
        for cond_name, cond_data in experiment_summary.get("condition_summaries", {}).items():
            reg.condition_names.add(cond_name)
            if cond_name not in reg.conditions:
                reg.conditions[cond_name] = ConditionResult(name=cond_name)
            cond_metrics = cond_data.get("metrics", {})
            for mk, mv in cond_metrics.items():
                if isinstance(mv, (int, float)) and _is_finite(mv):
                    reg.add_value(mv, f"condition_summaries.{cond_name}.{mk}")
                    reg.conditions[cond_name].aggregate_metric = mv

        # --- 3. Extract metrics_summary (min/max/mean per key) ---
        for key, stats in experiment_summary.get("metrics_summary", {}).items():
            if key in _INFRA_KEYS:
                continue
            for stat_name in ("min", "max", "mean"):
                v = stats.get(stat_name)
                if isinstance(v, (int, float)) and _is_finite(v):
                    reg.add_value(v, f"metrics_summary.{key}.{stat_name}")

        # --- 4. Extract primary_metric ---
        pm = _extract_primary_metric(metrics)
        if pm is not None:
            reg.primary_metric = pm
            reg.add_value(pm, "primary_metric")
        pm_std = metrics.get("primary_metric_std")
        if isinstance(pm_std, (int, float)) and _is_finite(pm_std):
            reg.primary_metric_std = pm_std
            reg.add_value(pm_std, "primary_metric_std")

        # --- 5. Compute per-condition stats ---
        for cond in reg.conditions.values():
            cond.compute_stats()
            if cond.mean is not None:
                reg.add_value(cond.mean, f"{cond.name}.mean")
            if cond.std is not None and cond.std > 0:
                reg.add_value(cond.std, f"{cond.name}.std")

        # --- 6. Compute pairwise differences (for comparative claims) ---
        cond_list = [c for c in reg.conditions.values() if c.mean is not None]
        for i, c1 in enumerate(cond_list):
            for c2 in cond_list[i + 1 :]:
                diff = c1.mean - c2.mean  # type: ignore[operator]
                if _is_finite(diff):
                    reg.add_value(diff, f"diff({c1.name}-{c2.name})")
                    reg.add_value(abs(diff), f"|diff({c1.name},{c2.name})|")
                # Relative improvement
                if c2.mean and abs(c2.mean) > 1e-9:  # type: ignore[operator]
                    rel = (c1.mean - c2.mean) / abs(c2.mean) * 100.0  # type: ignore[operator]
                    if _is_finite(rel):
                        reg.add_value(rel, f"rel_improve({c1.name} vs {c2.name})")
                        reg.add_value(abs(rel), f"|rel_improve({c1.name},{c2.name})|")

        # --- 7. Enrich from refinement_log (best iteration only) ---
        if refinement_log:
            _enrich_from_refinement_log(reg, refinement_log)

        logger.info(
            "VerifiedRegistry: %d values, %d conditions (%s), primary_metric=%s",
            len(reg.values),
            len(reg.condition_names),
            ", ".join(sorted(reg.condition_names)),
            reg.primary_metric,
        )
        return reg

    @classmethod
    def from_files(
        cls,
        experiment_summary_path: Path,
        refinement_log_path: Path | None = None,
        *,
        metric_direction: str = "maximize",
    ) -> VerifiedRegistry:
        """Convenience: build registry from file paths."""
        import json

        exp_data = json.loads(experiment_summary_path.read_text(encoding="utf-8"))
        ref_data = None
        if refinement_log_path and refinement_log_path.exists():
            ref_data = json.loads(refinement_log_path.read_text(encoding="utf-8"))
        return cls.from_experiment(exp_data, ref_data, metric_direction=metric_direction)


def _enrich_from_refinement_log(reg: VerifiedRegistry, refinement_log: dict) -> None:
    """Add values from the best refinement iteration."""
    best_metric = refinement_log.get("best_metric")
    if isinstance(best_metric, (int, float)) and _is_finite(best_metric):
        reg.add_value(best_metric, "refinement_log.best_metric")

    best_version = refinement_log.get("best_version", "")
    iterations = refinement_log.get("iterations", [])

    for it in iterations:
        ver = it.get("version_dir", "")
        metric = it.get("metric")
        if isinstance(metric, (int, float)) and _is_finite(metric):
            reg.add_value(metric, f"refinement_log.iteration.{ver}")

        # Extract per-seed values from sandbox stdout if available
        for sandbox_key in ("sandbox", "sandbox_after_fix"):
            sandbox = it.get(sandbox_key, {})
            if not isinstance(sandbox, dict):
                continue
            sb_metrics = sandbox.get("metrics", {})
            if isinstance(sb_metrics, dict):
                for mk, mv in sb_metrics.items():
                    if isinstance(mv, (int, float)) and _is_finite(mv) and mk not in _INFRA_KEYS:
                        reg.add_value(mv, f"refinement.{ver}.{sandbox_key}.{mk}")

                        # Parse per-seed keys here too
                        m = _PER_SEED_PATTERN.match(mk)
                        if m:
                            cond_name = m.group(1)
                            seed_idx = int(m.group(2))
                            reg.condition_names.add(cond_name)
                            if cond_name not in reg.conditions:
                                reg.conditions[cond_name] = ConditionResult(name=cond_name)
                            # Only update per_seed if this is the best version
                            if ver == best_version or best_version in ver:
                                reg.conditions[cond_name].per_seed_values[seed_idx] = mv


def _extract_primary_metric(metrics: dict) -> float | None:
    """Extract primary_metric from metrics dict."""
    pm = metrics.get("primary_metric")
    if isinstance(pm, (int, float)) and _is_finite(pm):
        return float(pm)
    return None


def _is_finite(value: Any) -> bool:
    """Check if value is a finite number (not NaN, not Inf, not bool)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)
