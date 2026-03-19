"""Domain-specific query expansion and experiment fallback registry.

This module is the single place to extend AutoResearchClaw with
domain-specific knowledge: pre-built search queries, experiment
design defaults, and null-result detection heuristics.

executor.py imports thin helpers from here; all domain logic lives here,
keeping executor.py close to upstream.

To add a new domain:
1. Add an entry to _DOMAIN_QUERY_MAP with 1-2 high-value search queries.
2. Optionally add an entry to _EXPERIMENT_FALLBACKS with dataset/baseline/
   method/ablation/metric defaults for experiment_design fallback plans.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Domain expansion registry
#
# Maps a domain keyword (as it would appear in config.research.domains or
# the topic string) to 1-2 high-value search queries for that domain.
# Keys are lowercase; matching is substring-based (e.g. "manga" matches
# "MaNGA", "ifu-manga", etc.).  Users extend this via config.research.domains.
# ---------------------------------------------------------------------------

_DOMAIN_QUERY_MAP: dict[str, list[str]] = {
    # Observational galaxy surveys
    "manga":          ["MaNGA integral field spectroscopy rotation curve",
                       "MaNGA IFU galaxy kinematics dark matter"],
    "ifu":            ["IFU spectroscopy galaxy kinematics",
                       "integral field unit survey velocity dispersion"],
    "lotss":          ["LoTSS LOFAR low-frequency radio survey",
                       "LoTSS DR2 radio continuum galaxy"],
    # Dark matter and halo models
    "rotation-curve": ["rotation curve NFW profile dark matter constraint",
                       "galaxy rotation curve systematic uncertainty"],
    "nfw":            ["NFW halo profile dark matter rotation curve",
                       "Navarro-Frenk-White profile fitting"],
    "dark-matter":    ["dark matter detection null result upper limit",
                       "dark matter halo galaxy kinematics constraint"],
    "null-result":    ["null result dark matter detection galaxy survey",
                       "non-detection upper limit dark matter signal"],
    # Algebraic / mathematical physics
    "cayley-dickson": ["Cayley-Dickson algebra hypercomplex numbers physics",
                       "octonion sedenion particle physics application"],
    "octonion":       ["octonion algebra exceptional Lie group physics",
                       "G2 octonion gauge theory"],
    "sedenion":       ["sedenion algebra zero divisor physics",
                       "16-dimensional hypercomplex number physics"],
    # Formal methods
    "formal-verification": ["formal verification proof assistant scientific computing",
                             "Coq Rocq theorem prover physics mathematics"],
    "rocq":           ["Rocq Coq proof assistant formal verification",
                       "interactive theorem proving mathematics"],
    # Cosmology
    "cosmology":      ["dark energy equation of state observational constraint",
                       "Pantheon supernova cosmological parameter"],
    # Fluid simulation / LBM
    "lbm":            ["lattice Boltzmann method fluid simulation turbulence",
                       "LBM GPU acceleration computational fluid dynamics"],
    # Machine learning (generic)
    "neural":         ["neural network deep learning benchmark reproducibility",
                       "machine learning scientific discovery"],
}


def get_domain_queries(
    topic: str,
    domains: tuple[str, ...] = (),
) -> list[str]:
    """Return high-value pre-built queries for the given topic and domains.

    Checks each entry in *domains* against _DOMAIN_QUERY_MAP, and also
    auto-detects domains by substring-matching the topic against all map
    keys.  Returns a deduplicated list (order: explicit domains first, then
    auto-detected).

    Parameters
    ----------
    topic:
        The raw research topic string.
    domains:
        Domain keywords from ``config.research.domains``.
    """
    active: set[str] = {d.lower() for d in domains}
    topic_lower = topic.lower()
    for d in _DOMAIN_QUERY_MAP:
        core_kw = d.split("-")[0]  # "cayley-dickson" -> "cayley"
        if core_kw in topic_lower:
            active.add(d)

    queries: list[str] = []
    for d in active:
        for q in _DOMAIN_QUERY_MAP.get(d, []):
            if q not in queries:
                queries.append(q)
    return queries


# ---------------------------------------------------------------------------
# Experiment design fallback registry
#
# Maps a domain keyword (first match wins) to default experiment plan fields.
# Used when the LLM fails to produce a valid YAML experiment plan.
# ---------------------------------------------------------------------------

_EXPERIMENT_FALLBACKS: list[tuple[list[str], dict[str, list[str]]]] = [
    # (topic keywords, fallback fields)
    (
        ["manga", "rotation curve"],
        {
            "datasets":         ["MaNGA_DR17_rotcurves", "manga_stack_D16"],
            "baselines":        ["NFW_profile_only", "baryonic_model"],
            "proposed_methods": ["harmonic_halo_stacking", "multi_algebra_DFT"],
            "ablations":        ["without_inclination_correction", "single_CD_dimension"],
            "metrics":          ["SNR", "RMS_residual", "detection_threshold"],
        },
    ),
    (
        ["cayley", "sedenion"],
        {
            "datasets":         ["CD_dimension_sweep", "partner_graph_spectrum"],
            "baselines":        ["quaternion_baseline", "octonion_baseline"],
            "proposed_methods": ["sedenion_analysis", "dim_tower_sweep"],
            "ablations":        ["single_dimension", "without_zero_divisors"],
            "metrics":          ["eigenvalue_degeneracy", "flat_band_fraction"],
        },
    ),
    (
        ["lotss", "radio"],
        {
            "datasets":         ["LoTSS_DR2", "MaNGA_crossmatch"],
            "baselines":        ["flux_threshold_only"],
            "proposed_methods": ["kinematic_bisection", "ultrametric_clustering"],
            "ablations":        ["without_radio_data", "single_frequency"],
            "metrics":          ["detection_fraction", "correlation_coefficient"],
        },
    ),
]

_EXPERIMENT_DEFAULTS: dict[str, list[str]] = {
    "datasets":         ["primary_dataset"],
    "baselines":        ["standard_baseline"],
    "proposed_methods": ["proposed_method"],
    "ablations":        ["without_key_component"],
    "metrics":          [],  # caller fills in metric_key
}


def get_experiment_fallback(topic: str, metric_key: str) -> dict[str, list[str]]:
    """Return domain-specific experiment plan defaults for a given topic.

    Returns a dict with keys: datasets, baselines, proposed_methods,
    ablations, metrics.  Falls back to generic defaults when no domain
    matches.

    Parameters
    ----------
    topic:
        The raw research topic string.
    metric_key:
        The configured primary metric key (from config.experiment.metric_key).
    """
    topic_lower = topic.lower()
    for keywords, fields in _EXPERIMENT_FALLBACKS:
        if any(kw in topic_lower for kw in keywords):
            return dict(fields)
    defaults = dict(_EXPERIMENT_DEFAULTS)
    defaults["metrics"] = [metric_key]
    return defaults


# ---------------------------------------------------------------------------
# Null-result and control-pair detection
#
# Used by the ablation checker in executor.py to suppress false-positive
# ablation warnings for experiments where identical outputs are expected.
# ---------------------------------------------------------------------------

_NULL_RESULT_KEYWORDS: tuple[str, ...] = (
    "null result", "null detection", "upper limit",
    "non-detection", "negative result", "no signal",
    "falsif", "no evidence",
)

_CONTROL_PAIR_KEYWORDS: tuple[str, ...] = (
    "ablation", "control", "baseline", "null",
    "no_harmonic", "single_mode",
)


def is_null_result_topic(topic: str) -> bool:
    """Return True if the topic describes a null-result experiment."""
    t = topic.lower()
    return any(kw in t for kw in _NULL_RESULT_KEYWORDS)


def is_control_pair(c1: str, c2: str) -> bool:
    """Return True if either condition name suggests an ablation-as-control pair."""
    low = c1.lower() + " " + c2.lower()
    return any(kw in low for kw in _CONTROL_PAIR_KEYWORDS)
