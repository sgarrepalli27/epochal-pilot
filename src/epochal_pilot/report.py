"""Reports: self-contained HTML (inline SVG, no JS, no network) + markdown, a cross-run leaderboard,
and a metrics-only ``--share`` export.

Nothing in a report comes from Epochal Results: no URLs, titles, snippets, page text or model
rationales (which may quote them). Epochal's Time Portal terms (§3.3-3.4) forbid publishing Results
or evaluation datasets built from them, so reports are safe to keep next to your code, and the
``share`` folder holds only metrics, event ids and the choices models made.
"""

from __future__ import annotations

import csv
import html
import json
import shutil
from pathlib import Path

from epochal_pilot.scoring import analyse, baseline_forecasts, norm_rows, paired_compare
from epochal_pilot.stats import small_n_warning

SHARE_COLUMNS = [
    "model",
    "mode",
    "context",
    "event",
    "event_date",
    "as_of",
    "outcome",
    "answer",
    "correct",
    "p_outcome",
    "brier",
    "abstained",
    "probabilities",
]
PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#4d7c0f", "#be185d"]
NOTE = (
    "Lower Brier and log loss are better (Brier 0 = perfect, 2 = confidently wrong). Intervals are 95% "
    "percentile bootstrap (seeded). BSS = Brier skill score vs the best non-hindsight baseline on the same "
    "events (>0 beats it). Hindsight baselines use the scored outcomes and are shown for context only. "
    "The leak score (gated accuracy above chance) is an upper bound, not proof. Not investment advice."
)


# ------------------------------------------------------------------------------------------------ loading


def read_csv(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_run(run_dir: Path, scenarios_dir: Path | None = None) -> dict:
    """Load a run folder and re-score it (works offline on any saved run, including v0.1 runs)."""
    run_dir = Path(run_dir)
    if not (run_dir / "predictions.csv").is_file():
        raise FileNotFoundError(f"{run_dir}: no predictions.csv (is this a run folder?)")
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").is_file() else {}
    rows = read_csv(run_dir / "predictions.csv")
    base = read_csv(run_dir / "baseline_predictions.csv")
    if not base and scenarios_dir and meta.get("scenario"):  # v0.1 runs: rebuild baselines from the scenario
        from epochal_pilot.scenario import load_scenario

        try:
            sc = load_scenario(Path(scenarios_dir), meta["scenario"])
            ids = list(dict.fromkeys(r["event"] for r in rows))
            by = {e.id: e for e in sc.events}
            base = baseline_forecasts([by[i] for i in ids if i in by], sc.baselines)
        except (FileNotFoundError, KeyError, ValueError):
            base = []
    scores, detail = analyse(rows, base, meta.get("cutoff_overrides") or {}, seed=int(meta.get("seed", 0) or 0))
    return {"dir": run_dir, "meta": meta, "rows": rows, "base": base, "scores": scores, "detail": detail}


# ------------------------------------------------------------------------------------------------ helpers


def _f(x, nd: int = 3) -> str:
    if x is None or x == "":
        return ""
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _ci(s: dict, k: str) -> str:
    lo, hi = s.get(f"{k}_ci_low"), s.get(f"{k}_ci_high")
    return "" if lo in (None, "") else f"[{_f(lo, 2)}, {_f(hi, 2)}]"


def _label(s: dict) -> str:
    if s["mode"] == "baseline":
        return s["model"] + (" (hindsight)" if s.get("hindsight") and "hindsight" not in s["model"] else "")
    return f"{s['model']} · {s['mode']}" + (f"[{s['context']}]" if s["mode"] == "gated" else "")


def _model_rows(scores: list[dict]) -> list[dict]:
    return [s for s in scores if s["mode"] != "baseline"]


def _warning(scores: list[dict]) -> str:
    ns = [s["n"] for s in _model_rows(scores)]
    return small_n_warning(min(ns)) if ns else ""


def _ledger(meta: dict) -> list[tuple[str, str]]:
    led = meta.get("epochal_ledger") or {}
    items = [("context", str(meta.get("context", "epochal" if "gated" in meta.get("modes", []) else "none")))]
    if meta.get("dry_run"):
        items.append(("dry run", "yes: offline fixtures, nothing spent"))
    items.append(("Epochal requests this run (2xx, billed)", str(meta.get("epochal_requests_this_run", 0))))
    if led:
        items += [
            ("new searches", str(led.get("searches_new", 0))),
            ("new fetches", str(led.get("fetches_new", 0))),
            ("fetches with no archived copy (still billed)", str(led.get("fetch_page_errors", 0))),
            ("cache hits (free)", str(led.get("cache_hits", 0))),
            ("errors refunded (not billed)", str(led.get("errors_refunded", 0))),
        ]
    if meta.get("epochal_lifetime_used") is not None:
        items.append(("lifetime used (this cache)", str(meta["epochal_lifetime_used"])))
    return items


def _per_event(rows: list[dict]) -> tuple[list[str], list[dict]]:
    nr = norm_rows(rows) if rows else []
    groups = list(dict.fromkeys(f"{r['model']} · {r['mode']}" for r in nr))
    events: dict[str, dict] = {}
    for r in nr:
        e = events.setdefault(r["event"], {"event": r["event"], "date": r["event_date"], "outcome": r["outcome"]})
        e["as_of"] = e.get("as_of") or r.get("as_of", "")
        e[f"{r['model']} · {r['mode']}"] = (r.get("answer") or "(abstain)", r["p_true"], int(r["correct"] >= 1))
    return groups, sorted(events.values(), key=lambda e: e["date"])


# --------------------------------------------------------------------------------------------- markdown


def markdown(meta: dict, scores: list[dict], detail: dict, rows: list[dict]) -> str:
    L = [f"# {meta.get('scenario', 'run')} backtest", ""]
    L += [
        f"Models: {', '.join(meta.get('models', []))} · modes: {', '.join(meta.get('modes', []))} · "
        f"context: {meta.get('context', '')} · as-of = event - {meta.get('lead_days', '')} days · "
        f"seed {meta.get('seed', 0)} · events: {meta.get('events', '')}",
        "",
    ]
    w = _warning(scores)
    if w:
        L += [f"> **Warning:** {w}", ""]
    L += [
        "| forecaster | n | accuracy [95% CI] | Brier [95% CI] | log loss | BSS [95% CI] | p vs ref "
        "| pre/post cutoff acc |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        prepost = ""
        if s.get("n_pre_cutoff") is not None:
            prepost = (
                f"{_f(s.get('acc_pre_cutoff'), 2)} (n={s['n_pre_cutoff']}) / "
                f"{_f(s.get('acc_post_cutoff'), 2)} (n={s['n_post_cutoff']})"
            )
        bss = f"{_f(s.get('brier_skill'))} {_ci(s, 'brier_skill')}" if s.get("brier_skill") is not None else ""
        L.append(
            f"| {_label(s)} | {s['n']} | {_f(s.get('accuracy'))} {_ci(s, 'accuracy')} | {_f(s.get('brier'))} "
            f"{_ci(s, 'brier')} | {_f(s.get('log_loss'))} | {bss} | {_f(s.get('p_vs_reference'))} | {prepost} |"
        )
    refs = sorted({s["reference"] for s in scores if s.get("reference")})
    if refs:
        L += ["", f"Skill reference (best non-hindsight baseline): {', '.join(refs)}."]
    if detail.get("pairs"):
        L += [
            "",
            "## Paired tests",
            "",
            "| A | B | n | Brier A - B [95% CI] | p (bootstrap) | McNemar p |",
            "|---|---|---|---|---|---|",
        ]
        for p in detail["pairs"]:
            d = p["brier_diff"]
            L.append(
                f"| {p['a']} | {p['b']} | {p['n']} | {_f(d['mean_diff'])} "
                f"[{_f(d['ci_low'], 2)}, {_f(d['ci_high'], 2)}] "
                f"| {_f(d['p_value'])} | {_f((p.get('mcnemar') or {}).get('p_value'))} |"
            )
    L += ["", "## Calibration (top-choice confidence vs hit rate)", ""]
    for key, g in detail.get("groups", {}).items():
        L += [f"**{key}**", "", "| bin | n | mean confidence | hit rate |", "|---|---|---|---|"]
        L += [f"| {b['bin']} | {b['n']} | {_f(b['mean_confidence'])} | {_f(b['hit_rate'])} |" for b in g["calibration"]]
        L.append("")
    groups, evs = _per_event(rows)
    if evs:
        L += [
            "## Per event",
            "",
            "| event | date | outcome | " + " | ".join(groups) + " |",
            "|---" * (3 + len(groups)) + "|",
        ]
        for e in evs:
            cells = []
            for g in groups:
                a = e.get(g)
                cells.append("" if not a else f"{'✓' if a[2] else '✗'} {a[0]} ({a[1]:.2f})")
            L.append(f"| {e['event']} | {e['date']} | {e['outcome']} | " + " | ".join(cells) + " |")
    L += ["", "## Cost ledger", ""] + [f"- {k}: {v}" for k, v in _ledger(meta)]
    git = meta.get("git") or {}
    if git.get("sha"):
        L.append(f"- code: {git['sha'][:12]}{' (uncommitted changes)' if git.get('dirty') else ''}")
    L += ["", NOTE, ""]
    return "\n".join(L)


# ------------------------------------------------------------------------------------------------- html


def reliability_svg(detail: dict, width: int = 360, height: int = 360) -> str:
    pad = 40
    W, H = width - 2 * pad, height - 2 * pad

    def xy(x: float, y: float) -> tuple[float, float]:
        return pad + x * W, pad + (1 - y) * H

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        'role="img" aria-label="Reliability diagram">',
        f'<rect x="{pad}" y="{pad}" width="{W}" height="{H}" fill="#fafafa" stroke="#999"/>',
    ]
    for t in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        x, _ = xy(t, 0)
        _, y = xy(0, t)
        parts.append(f'<line x1="{x:.1f}" y1="{pad}" x2="{x:.1f}" y2="{pad + H}" stroke="#eee"/>')
        parts.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{pad + W}" y2="{y:.1f}" stroke="#eee"/>')
        parts.append(f'<text x="{x:.1f}" y="{pad + H + 14}" font-size="10" text-anchor="middle">{t:.1f}</text>')
        parts.append(f'<text x="{pad - 6}" y="{y + 3:.1f}" font-size="10" text-anchor="end">{t:.1f}</text>')
    x0, y0 = xy(0, 0)
    x1, y1 = xy(1, 1)
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="#888" stroke-dasharray="4 3"/>')
    parts.append(
        f'<text x="{pad + W / 2}" y="{height - 6}" font-size="11" text-anchor="middle">'
        "confidence in chosen option</text>"
    )
    parts.append(
        f'<text x="12" y="{pad + H / 2}" font-size="11" text-anchor="middle" '
        f'transform="rotate(-90 12 {pad + H / 2})">observed hit rate</text>'
    )
    legend_y = pad + 12
    for i, (key, g) in enumerate(detail.get("groups", {}).items()):
        color = PALETTE[i % len(PALETTE)]
        pts = [b for b in g["calibration"] if b["n"]]
        coords = [xy(b["mean_confidence"], b["hit_rate"]) for b in pts]
        if len(coords) > 1:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        for (x, y), b in zip(coords, pts, strict=True):
            r = 3 + min(6, b["n"])
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" fill-opacity="0.55">'
                f"<title>{html.escape(key)} {b['bin']}: n={b['n']}</title></circle>"
            )
        parts.append(
            f'<text x="{pad + 6}" y="{legend_y + 13 * i}" font-size="10" fill="{color}">{html.escape(key[:48])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


CSS = """body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem auto;max-width:1100px;
padding:0 1rem;color:#111}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem}table{border-collapse:collapse;
width:100%;margin:.5rem 0}th,td{border-bottom:1px solid #e5e5e5;padding:.3rem .5rem;text-align:left;
vertical-align:top}th{background:#f5f5f5}td.num{text-align:right;font-variant-numeric:tabular-nums}
.warn{background:#fff7e6;border-left:4px solid #f59e0b;padding:.5rem .8rem}.muted{color:#666;font-size:.9em}
.ok{color:#15803d}.bad{color:#b91c1c}.hind{color:#888;font-style:italic}code{background:#f3f3f3;padding:0 .2rem}"""


def _table(head: list[str], body: list[list[str]], num_from: int = 1) -> str:
    h = "".join(f"<th>{html.escape(x)}</th>" for x in head)
    rows = []
    for r in body:
        cells = []
        for i, c in enumerate(r):
            cls = ' class="num"' if i >= num_from else ""
            cells.append(f"<td{cls}>{c}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{h}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def html_report(meta: dict, scores: list[dict], detail: dict, rows: list[dict], title: str | None = None) -> str:
    e = html.escape
    title = title or f"{meta.get('scenario', 'run')} backtest"
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{e(title)}</title>"]
    out.append(f"<meta name='viewport' content='width=device-width, initial-scale=1'><style>{CSS}</style></head><body>")
    out.append(f"<h1>{e(title)}</h1>")
    out.append(
        "<p class='muted'>"
        + e(
            f"models: {', '.join(meta.get('models', []))} · modes: {', '.join(meta.get('modes', []))} · "
            f"context: {meta.get('context', '')} · as-of = event - {meta.get('lead_days', '')} days · "
            f"seed {meta.get('seed', 0)} · events: {meta.get('events', '')} · created {meta.get('created', '')}"
        )
        + "</p>"
    )
    w = _warning(scores)
    if w:
        out.append(f"<p class='warn'><b>Small sample.</b> {e(w)}</p>")
    out.append("<h2>Scores</h2>")
    body = []
    for s in scores:
        lab = e(_label(s))
        if s.get("hindsight"):
            lab = f"<span class='hind'>{lab}</span>"
        prepost = ""
        if s.get("n_pre_cutoff") is not None:
            prepost = (
                f"{_f(s.get('acc_pre_cutoff'), 2)} (n={s['n_pre_cutoff']}) / "
                f"{_f(s.get('acc_post_cutoff'), 2)} (n={s['n_post_cutoff']})"
            )
        body.append(
            [
                lab,
                str(s["n"]),
                f"{_f(s.get('accuracy'))} <span class='muted'>{_ci(s, 'accuracy')}</span>",
                f"{_f(s.get('brier'))} <span class='muted'>{_ci(s, 'brier')}</span>",
                _f(s.get("log_loss")),
                (f"{_f(s.get('brier_skill'))} <span class='muted'>{_ci(s, 'brier_skill')}</span>")
                if s.get("brier_skill") is not None
                else "",
                _f(s.get("p_vs_reference")),
                _f(s.get("ece")),
                prepost,
            ]
        )
    out.append(
        _table(
            [
                "forecaster",
                "n",
                "accuracy [95% CI]",
                "Brier [95% CI]",
                "log loss",
                "BSS [95% CI]",
                "p vs ref",
                "ECE",
                "acc pre / post cutoff",
            ],
            body,
        )
    )
    refs = sorted({s["reference"] for s in scores if s.get("reference")})
    if refs:
        out.append(f"<p class='muted'>Skill reference (best non-hindsight baseline): {e(', '.join(refs))}.</p>")
    if detail.get("pairs"):
        out.append("<h2>Paired tests</h2>")
        pb = []
        for p in detail["pairs"]:
            d = p["brier_diff"]
            pb.append(
                [
                    e(p["a"]),
                    e(p["b"]),
                    str(p["n"]),
                    f"{_f(d['mean_diff'])} [{_f(d['ci_low'], 2)}, {_f(d['ci_high'], 2)}]",
                    _f(d["p_value"]),
                    _f((p.get("mcnemar") or {}).get("p_value")),
                ]
            )
        out.append(_table(["A", "B", "n", "Brier A - B [95% CI]", "p (bootstrap)", "McNemar p"], pb, num_from=2))
    out.append("<h2>Calibration</h2><div style='display:flex;gap:2rem;flex-wrap:wrap;align-items:flex-start'>")
    out.append(reliability_svg(detail))
    cal = []
    for key, g in detail.get("groups", {}).items():
        for b in g["calibration"]:
            if b["n"]:
                cal.append([e(key), b["bin"], str(b["n"]), _f(b["mean_confidence"]), _f(b["hit_rate"])])
    out.append(
        "<div style='flex:1;min-width:320px'>" + _table(["forecaster", "bin", "n", "confidence", "hit rate"], cal, 2)
    )
    out.append("</div></div>")
    groups, evs = _per_event(rows)
    if evs:
        out.append("<h2>Per event</h2>")
        eb = []
        for ev in evs:
            cells = [e(ev["event"]), e(ev["date"]), e(ev.get("as_of", "")[:10]), e(ev["outcome"])]
            for g in groups:
                a = ev.get(g)
                cells.append(
                    ""
                    if not a
                    else f"<span class='{'ok' if a[2] else 'bad'}'>{'✓' if a[2] else '✗'} {e(a[0])}</span> "
                    f"<span class='muted'>p={a[1]:.2f}</span>"
                )
            eb.append(cells)
        out.append(_table(["event", "date", "as-of", "outcome", *groups], eb, num_from=99))
    out.append("<h2>Cost ledger</h2>")
    out.append(_table(["item", "value"], [[e(k), e(v)] for k, v in _ledger(meta)], num_from=1))
    git = meta.get("git") or {}
    env = meta.get("environment") or {}
    out.append(
        "<p class='muted'>"
        + e(
            f"epochal-pilot {env.get('version', '')} · code {str(git.get('sha') or 'unknown')[:12]}"
            f"{' (uncommitted changes)' if git.get('dirty') else ''} · events sha256 "
            f"{str((meta.get('hashes') or {}).get('events_sha256') or '')[:12]}"
        )
        + "</p>"
    )
    out.append(f"<p class='muted'>{e(NOTE)} This report holds metrics only: no Epochal URLs, titles or text.</p>")
    out.append("</body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------------------------------- commands


def write_report(run: dict, out_dir: Path | None = None) -> tuple[Path, Path]:
    out_dir = Path(out_dir or run["dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    h = out_dir / "report.html"
    m = out_dir / "report.md"
    h.write_text(html_report(run["meta"], run["scores"], run["detail"], run["rows"]), encoding="utf-8")
    m.write_text(markdown(run["meta"], run["scores"], run["detail"], run["rows"]), encoding="utf-8")
    return h, m


def share_meta(meta: dict) -> dict:
    """run.json without Epochal request/copy ids (Epochal metadata) or anything machine-specific."""
    m = json.loads(json.dumps(meta))
    led = m.get("epochal_ledger")
    if isinstance(led, dict):
        led.pop("request_ids", None)
        led.pop("copy_ids", None)
    m.pop("environment", None)
    return m


def export_share(run: dict, dest: Path | None = None) -> Path:
    """Metrics-only folder you may publish: no Epochal URLs, titles, snippets, page text or rationales."""
    dest = Path(dest or Path(run["dir"]) / "share")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    safe_rows = [{k: r.get(k, "") for k in SHARE_COLUMNS} for r in run["rows"]]
    with open(dest / "predictions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHARE_COLUMNS)
        w.writeheader()
        w.writerows(safe_rows)
    with open(dest / "scores.csv", "w", newline="", encoding="utf-8") as f:
        fields = list(dict.fromkeys(k for s in run["scores"] for k in s))
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(run["scores"])
    (dest / "stats.json").write_text(json.dumps(run["detail"], indent=1), encoding="utf-8")
    meta = share_meta(run["meta"])
    (dest / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (dest / "report.html").write_text(html_report(meta, run["scores"], run["detail"], safe_rows), encoding="utf-8")
    (dest / "report.md").write_text(markdown(meta, run["scores"], run["detail"], safe_rows), encoding="utf-8")
    (dest / "NOTICE.md").write_text(
        "# About this folder\n\n"
        "Metrics only, made with epochal-pilot. It contains event ids, outcomes, the option each model chose, "
        "its probabilities and scores. It contains no Epochal Time Portal Results: no URLs, titles, snippets, "
        "page text, request ids or model rationales. Forecasts were generated by AI models.\n\n"
        f"Context source: {meta.get('context', '')}. As-of dates are in predictions.csv.\n",
        encoding="utf-8",
    )
    return dest


def cmd_report(args) -> int:
    run = load_run(Path(args.run_dir), Path(args.scenarios_dir))
    h, m = write_report(run)
    print(f"Wrote {h} and {m}")
    if args.share:
        d = export_share(run)
        print(f"Wrote metrics-only share folder {d} (no Epochal URLs, titles, snippets or rationales)")
    return 0


def leaderboard(runs: list[dict]) -> tuple[list[dict], list[dict]]:
    board = []
    for i, run in enumerate(runs):
        for s in run["scores"]:
            if s["mode"] == "baseline" and i > 0:
                continue
            board.append({"run": run["dir"].name, **s})
    board.sort(key=lambda s: (s["brier"] is None, s["brier"] if s["brier"] is not None else 9))
    pairs = []
    groups = []
    for run in runs:
        nr = norm_rows(run["rows"])
        for key in dict.fromkeys((r["model"], r["mode"], r["context"]) for r in nr):
            groups.append(
                (run["dir"].name, key, {r["event"]: r for r in nr if (r["model"], r["mode"], r["context"]) == key})
            )
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            (ra, ka, ea), (rb, kb, eb) = groups[i], groups[j]
            same_model = ka[0] == kb[0] and ka[1] == kb[1]
            if ra == rb or not (same_model or (len(runs) == 2 and len(groups) == 2)):
                continue
            common = sorted(set(ea) & set(eb))
            if common:
                pairs.append(
                    paired_compare(
                        f"{ra}: {ka[0]} {ka[1]}[{ka[2]}]",
                        [ea[e] for e in common],
                        f"{rb}: {kb[0]} {kb[1]}[{kb[2]}]",
                        [eb[e] for e in common],
                    )
                )
    return board, pairs


def compare_markdown(board: list[dict], pairs: list[dict]) -> str:
    L = [
        "# Leaderboard",
        "",
        "| # | run | forecaster | n | accuracy [95% CI] | Brier [95% CI] | BSS | log loss |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(board, 1):
        L.append(
            f"| {i} | {s['run']} | {_label(s)} | {s['n']} | {_f(s.get('accuracy'))} {_ci(s, 'accuracy')} | "
            f"{_f(s.get('brier'))} {_ci(s, 'brier')} | {_f(s.get('brier_skill'))} | {_f(s.get('log_loss'))} |"
        )
    if pairs:
        L += [
            "",
            "## Paired comparisons (shared events)",
            "",
            "| A | B | n | acc A / B | Brier A - B [95% CI] | p | McNemar p |",
            "|---|---|---|---|---|---|---|",
        ]
        for p in pairs:
            d = p["brier_diff"]
            L.append(
                f"| {p['a']} | {p['b']} | {p['n']} | {_f(p['acc_a'], 2)} / {_f(p['acc_b'], 2)} | {_f(d['mean_diff'])} "
                f"[{_f(d['ci_low'], 2)}, {_f(d['ci_high'], 2)}] | {_f(d['p_value'])} | "
                f"{_f((p.get('mcnemar') or {}).get('p_value'))} |"
            )
    ns = [s["n"] for s in board if s["mode"] != "baseline"]
    if ns and small_n_warning(min(ns)):
        L += ["", f"> **Warning:** {small_n_warning(min(ns))}"]
    L += ["", NOTE, ""]
    return "\n".join(L)


def cmd_compare(args) -> int:
    runs = [load_run(Path(d), Path(args.scenarios_dir)) for d in args.run_dirs]
    ev_sets = {r["dir"].name: frozenset(x["event"] for x in r["rows"]) for r in runs}
    board, pairs = leaderboard(runs)
    md = compare_markdown(board, pairs)
    print(md)
    if len(set(ev_sets.values())) > 1:
        print(
            "Note: runs cover different events; leaderboard rows are not directly comparable. "
            "Paired comparisons use shared events only."
        )
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "compare.md").write_text(md, encoding="utf-8")
        body = "".join(
            f"<tr><td>{i}</td><td>{html.escape(s['run'])}</td><td>{html.escape(_label(s))}</td>"
            f"<td class='num'>{s['n']}</td>"
            f"<td class='num'>{_f(s.get('accuracy'))} {_ci(s, 'accuracy')}</td><td class='num'>{_f(s.get('brier'))} "
            f"{_ci(s, 'brier')}</td><td class='num'>{_f(s.get('brier_skill'))}</td></tr>"
            for i, s in enumerate(board, 1)
        )
        pr = "".join(
            f"<tr><td>{html.escape(p['a'])}</td><td>{html.escape(p['b'])}</td><td class='num'>{p['n']}</td>"
            f"<td class='num'>{_f(p['brier_diff']['mean_diff'])} [{_f(p['brier_diff']['ci_low'], 2)}, "
            f"{_f(p['brier_diff']['ci_high'], 2)}]</td><td class='num'>{_f(p['brier_diff']['p_value'])}</td>"
            f"<td class='num'>{_f((p.get('mcnemar') or {}).get('p_value'))}</td></tr>"
            for p in pairs
        )
        doc = (
            f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>epochal-pilot leaderboard</title>"
            f"<style>{CSS}</style></head><body><h1>Leaderboard</h1><table><thead><tr><th>#</th><th>run</th>"
            f"<th>forecaster</th><th>n</th><th>accuracy [95% CI]</th><th>Brier [95% CI]</th><th>BSS</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
            + (
                "<h2>Paired comparisons (shared events)</h2><table><thead><tr><th>A</th><th>B</th><th>n</th>"
                f"<th>Brier A - B [95% CI]</th><th>p</th><th>McNemar p</th></tr></thead><tbody>{pr}</tbody></table>"
                if pairs
                else ""
            )
            + f"<p class='muted'>{html.escape(NOTE)}</p></body></html>"
        )
        (out / "compare.html").write_text(doc, encoding="utf-8")
        print(f"Wrote {out / 'compare.html'} and compare.md")
    return 0
