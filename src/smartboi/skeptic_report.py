"""Skeptic-effect readout -- does the adversarial pass earn its 2x per-item
cost? Pure analysis, no network or LLM.

Every accepted evidence record already stores the updater's PRE-skeptic
proposed_confidence/proposed_magnitude next to the post-skeptic
confidence/magnitude (see dossier.EvidenceRecord), and the engine logs every
skeptic REFUTATION to logs/skeptic_refutations.jsonl. Together those answer the
question the prior audits kept raising and nothing read: how often does the
skeptic refute, how much does it re-scale what it accepts, and does that differ
for direct vs propagated evidence and by model -- the inputs to deciding
whether the trade-gating skeptic belongs on a cheaper or pricier model tier."""
from __future__ import annotations


def _blank() -> dict:
    return {
        "n_accepted": 0,
        "n_refuted": 0,
        "refutation_rate": None,
        "n_scaled_down": 0,
        "n_scaled_up": 0,
        "n_unchanged": 0,
        "mean_confidence_delta": None,
        "mean_magnitude_delta": None,
    }


def _finalize(bucket: dict, conf_deltas: list[float], mag_deltas: list[float]) -> dict:
    total_judged = bucket["n_accepted"] + bucket["n_refuted"]
    if total_judged:
        bucket["refutation_rate"] = bucket["n_refuted"] / total_judged
    if conf_deltas:
        bucket["mean_confidence_delta"] = sum(conf_deltas) / len(conf_deltas)
    if mag_deltas:
        bucket["mean_magnitude_delta"] = sum(mag_deltas) / len(mag_deltas)
    return bucket


def analyze_skeptic_effect(accepted: list[dict], refutations: list[dict]) -> dict:
    """`accepted`: one row per merged evidence record, with is_propagated,
    model, proposed_confidence, proposed_magnitude, confidence, magnitude.
    `refutations`: one row per skeptic-refuted item, with is_propagated, model.
    Returns overall stats plus splits by direct/propagated and by model. A
    record whose proposed_* is missing (legacy, pre-instrumentation) still
    counts toward n_accepted but not the adjustment distribution."""
    groups: dict[str, dict] = {"overall": _blank()}
    deltas: dict[str, tuple[list, list]] = {"overall": ([], [])}

    def bucket(name: str) -> str:
        groups.setdefault(name, _blank())
        deltas.setdefault(name, ([], []))
        return name

    def record_accepted(row: dict, names: list[str]) -> None:
        pc, pm = row.get("proposed_confidence"), row.get("proposed_magnitude")
        c, m = row.get("confidence"), row.get("magnitude")
        has_deltas = None not in (pc, pm, c, m)
        cd = (c - pc) if has_deltas else None
        md = (m - pm) if has_deltas else None
        # "Scaled" classified on the joint score direction, tie-broken toward
        # "down": the skeptic is a cap-leaning pass and a mixed up-one/down-
        # other should not read as a lift.
        cls = None
        if has_deltas:
            before, after = pc * pm, c * m
            cls = "n_unchanged" if abs(after - before) < 1e-9 else ("n_scaled_up" if after > before else "n_scaled_down")
        for name in names:
            g = groups[bucket(name)]
            g["n_accepted"] += 1
            if has_deltas:
                g[cls] += 1
                deltas[name][0].append(cd)
                deltas[name][1].append(md)

    for row in accepted:
        model = row.get("model") or "unknown"
        kind = "propagated" if row.get("is_propagated") else "direct"
        record_accepted(row, ["overall", f"type:{kind}", f"model:{model}"])

    for row in refutations:
        model = row.get("model") or "unknown"
        kind = "propagated" if row.get("is_propagated") else "direct"
        for name in ("overall", f"type:{kind}", f"model:{model}"):
            groups[bucket(name)]["n_refuted"] += 1

    return {name: _finalize(groups[name], deltas[name][0], deltas[name][1]) for name in groups}


def _fmt(x, pct: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.0f}%" if pct else f"{x:+.3f}"


def _row(label: str, s: dict) -> str:
    return (
        f"{label:<22}{s['n_accepted']:<9}{s['n_refuted']:<9}"
        f"{_fmt(s['refutation_rate'], pct=True):<12}"
        f"{s['n_scaled_down']:<7}{s['n_scaled_up']:<7}{s['n_unchanged']:<7}"
        f"{_fmt(s['mean_confidence_delta']):<10}{_fmt(s['mean_magnitude_delta']):<10}"
    )


def format_skeptic_report(analysis: dict) -> str:
    """Plain-text table: accepted/refuted counts, refutation rate, the
    up/down/unchanged re-scaling distribution, and mean confidence/magnitude
    deltas -- overall, then split by evidence type, then by model."""
    overall = analysis.get("overall", _blank())
    if not overall["n_accepted"] and not overall["n_refuted"]:
        return ("No skeptic activity recorded yet -- accepted records carry the pre/post numbers "
                "and refutations accrue to logs/skeptic_refutations.jsonl as evidence is judged.")
    header = (
        f"{'Group':<22}{'Accept':<9}{'Refute':<9}{'Refute%':<12}"
        f"{'Down':<7}{'Up':<7}{'Same':<7}{'dConf':<10}{'dMag':<10}"
    )
    lines = ["=== Skeptic effect (does the adversarial pass earn its cost?) ===", "", header, "-" * len(header)]
    lines.append(_row("overall", overall))

    type_names = sorted(n for n in analysis if n.startswith("type:"))
    if type_names:
        lines.append("")
        lines.append("-- by evidence type --")
        for n in type_names:
            lines.append(_row(n.split(":", 1)[1], analysis[n]))

    model_names = sorted(n for n in analysis if n.startswith("model:"))
    if model_names:
        lines.append("")
        lines.append("-- by skeptic model --")
        for n in model_names:
            lines.append(_row(n.split(":", 1)[1], analysis[n]))

    lines.append("")
    lines.append("dConf/dMag are mean(post-skeptic - proposed): negative = the skeptic trimmed on "
                 "average. A near-zero refutation rate AND near-zero deltas would mean the pass is "
                 "paying 2x per item to rubber-stamp -- the case for a cheaper tier; a high "
                 "propagated-refutation rate with real deltas is the case for a pricier one.")
    return "\n".join(lines)
