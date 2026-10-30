from __future__ import annotations

import html
import json
from pathlib import Path

from .store import ResearchStore


def build_dashboard(
    store: ResearchStore,
    output_path: str | Path,
    train_end: str | None = None,
    validation_end: str | None = None,
) -> Path:
    rows = store.latest_observations()
    observed = [
        row for row in rows
        if row["observation"] is not None and row["product_state"] == "live"
    ]
    states = {}
    for row in observed:
        states[row["state"]] = states.get(row["state"], 0) + 1
    for row in rows:
        if row["product_state"] != "live":
            label = "not_returned" if row["product_state"] == "not_returned" else "not_live"
            states[label] = states.get(label, 0) + 1
    counts = store.event_counts()
    outcomes = store.outcome_records()

    def period(event_time):
        if not train_end or not validation_end:
            return "all observations"
        day = str(event_time)[:10]
        if day <= train_end:
            return "train"
        if day <= validation_end:
            return "validation"
        return "out-of-sample"

    pooled, per_symbol = {}, {}
    for row in outcomes:
        key = (row["variant"], row["horizon_hours"], period(row["event_time"]))
        pooled.setdefault(key, []).append(row)
        symbol_key = (row["symbol"], row["variant"], row["horizon_hours"])
        per_symbol.setdefault(symbol_key, []).append(row)

    def outcome_row(name, variant, horizon, values):
        returns = [row["net_return_pct"] for row in values if row["net_return_pct"] is not None]
        gross = [row["gross_return_pct"] for row in values]
        wins = sum(value > 0 for value in returns)
        win_rate = f"{(100 * wins / len(returns)):.1f}%" if returns else "—"
        mean_net = f"{sum(returns) / len(returns):.3f}%" if returns else "—"
        return (
            f"<tr><td>{cell(name)}</td><td>{cell(variant)}</td><td>{cell(horizon)}h</td>"
            f"<td>{len(values)}</td><td>{win_rate}</td>"
            f"<td>{(sum(gross) / len(gross)):.3f}%</td>"
            f"<td>{mean_net}</td>"
            f"<td>{len(values) - len(returns)}</td></tr>"
            if values else ""
        )

    def cell(value):
        return html.escape("—" if value is None else str(value))

    def row_markup(row):
        observation = row["observation"] or {}
        displayed_state = (
            row["state"] if row["product_state"] == "live"
            else "not_returned" if row["product_state"] == "not_returned"
            else "not_live"
        )
        market = observation.get("market", {})
        execution = observation.get("execution", {})
        detail = html.escape(json.dumps(observation, indent=2, sort_keys=True))
        score = observation.get("sleeping_score")
        confidence = observation.get("data_quality", {}).get("confidence") if observation else None
        spread = market.get("spread_pct")
        score_text = f"{score:.1f}" if score is not None else None
        confidence_text = f"{confidence:.2f}" if confidence is not None else None
        spread_text = f"{spread:.4f}%" if spread is not None else None
        return f"""
        <tr data-state="{cell(displayed_state)}">
          <td>{cell(row['symbol'])}</td><td>{cell(row['product_state'])}</td>
          <td>{cell(displayed_state)}</td><td>{cell(score_text)}</td>
          <td>{cell(confidence_text)}</td>
          <td>{cell(spread_text)}</td>
          <td>{cell(execution.get('eligible') if observation else None)}</td>
          <td><details><summary>Inspect</summary><pre>{detail}</pre></details></td>
        </tr>"""

    def ranked_markup(title, selected, score_fn, label):
        ranked = sorted(selected, key=score_fn, reverse=True)
        body = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                cell(row["symbol"]),
                cell(row["state"]),
                cell(label(row)),
                cell(row["observation"].get("state_reasons", [])),
            )
            for row in ranked[:50]
        )
        empty_candidates = '<tr><td colspan="4">No candidates.</td></tr>'
        return (
            f"<h2>{cell(title)}</h2><table><thead><tr><th>Symbol</th><th>State</th>"
            f"<th>Rank measurement</th><th>Why / state reasons</th></tr></thead>"
            f"<tbody>{body or empty_candidates}</tbody></table>"
        )

    def acceleration(row):
        observation = row["observation"] or {}
        features = observation.get("features", {})
        values = (
            features.get("volume_ratio_30d"),
            features.get("trade_count_change_ratio"),
            None if features.get("range_compression_percentile") is None
            else features.get("range_compression_percentile") / 100,
        )
        return max((float(value) for value in values if value is not None), default=-1)

    sleeping_rows = [row for row in observed if row["state"] == "sleeping"]
    awakening_rows = [row for row in observed if row["state"] in {"awakening", "expansion", "climax"}]
    expansion_rows = [row for row in observed if row["state"] in {"expansion", "climax"}]
    failed_rows = [row for row in observed if row["state"] == "failed_auction"]
    ranked_sections = "".join([
        ranked_markup(
            "Sleeping candidates ranked by per-symbol score",
            sleeping_rows,
            lambda row: row["observation"].get("sleeping_score", -1) or -1,
            lambda row: f"sleeping score {row['observation'].get('sleeping_score')}",
        ),
        ranked_markup(
            "Awakening candidates ranked by activity acceleration",
            awakening_rows,
            acceleration,
            lambda row: f"activity ratio {acceleration(row):.3f}",
        ),
        ranked_markup(
            "Expansion / climax candidates",
            expansion_rows,
            acceleration,
            lambda row: f"activity ratio {acceleration(row):.3f}",
        ),
        ranked_markup(
            "Failed-high / breakdown confirmations",
            failed_rows,
            lambda row: row["observation"].get("time_in_state_hours", 0) or 0,
            lambda row: f"{row['observation'].get('time_in_state_hours', 0):.1f}h in state",
        ),
    ])
    cards = "".join(
        f'<article><b>{cell(count)}</b><span>{cell(name)}</span></article>'
        for name, count in sorted(states.items())
    )
    event_cards = "".join(
        f'<article><b>{cell(count)}</b><span>{cell(name.replace("_", " "))} events</span></article>'
        for name, count in sorted(counts.items())
    )
    table_rows = "".join(row_markup(row) for row in rows)
    pooled_rows = "".join(
        outcome_row(split, variant, horizon, values)
        for (variant, horizon, split), values in sorted(pooled.items())
    )
    symbol_rows = "".join(
        outcome_row(symbol, variant, horizon, values)
        for (symbol, variant, horizon), values in sorted(per_symbol.items())
    )
    split_description = (
        f"Chronological cutoffs: train through {train_end}, validation through "
        f"{validation_end}, out-of-sample after {validation_end} (UTC dates)."
        if train_end and validation_end else
        "Chronological train/validation/out-of-sample cutoffs are not configured; "
        "pooled results are descriptive only."
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Delta Sleeping-Coin Research</title>
<style>
body{{font:14px system-ui,sans-serif;margin:24px;color:#17212b;background:#f5f7fa}}
h1,h2{{margin:0 0 12px}} .note{{padding:12px;background:#fff3cd;border:1px solid #e3c66e}}
.cards{{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 24px}}
article{{background:white;padding:10px 16px;border:1px solid #d5dce3;border-radius:6px;min-width:110px}}
article b,article span{{display:block}} article b{{font-size:20px}}
table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:8px;border:1px solid #d5dce3;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#e8edf2}}pre{{max-width:900px;white-space:pre-wrap;overflow-wrap:anywhere}}
input{{padding:8px;width:min(420px,90%)}} .muted{{color:#586674}}
</style></head><body>
<h1>Delta Exchange India — Market-Wide Sleeping-Coin Research</h1>
<p class="note"><b>Shadow research only:</b> this dashboard does not submit orders or enable live shorts.
Discovery includes illiquid contracts; execution eligibility is reported separately.
All candle-derived notional is an estimate, and unavailable order-flow/OI history is not fabricated.</p>
<p>Products retained in catalog: <b>{len(rows)}</b> · live observations: <b>{len(observed)}</b></p>
<h2>Current states</h2><section class="cards">{cards}</section>
<h2>Logged research events and rejections</h2><section class="cards">{event_cards}</section>
{ranked_sections}
<h2>Prospective shadow-event outcomes</h2>
<p>{cell(split_description)} Outcomes compare a triggered signal's reference-price return with
the first completed hourly close at +1h, +4h, and +24h. Net return subtracts the signal-time
spread, estimated buy/sell impact, and two taker fees only when all cost inputs are available.
Contract PnL is not reconstructed. This is not a fill simulator; missing costs are excluded
from net-return averages and counted.</p>
<table><thead><tr><th>Window</th><th>Variant</th><th>Horizon</th><th>Events</th>
<th>Net win rate</th><th>Mean gross</th><th>Mean net</th><th>Missing cost cases</th></tr></thead>
<tbody>{pooled_rows or '<tr><td colspan="8">No matured signal outcomes yet.</td></tr>'}</tbody></table>
<h2>Per-symbol shadow outcomes</h2>
<table><thead><tr><th>Symbol</th><th>Variant</th><th>Horizon</th><th>Events</th>
<th>Net win rate</th><th>Mean gross</th><th>Mean net</th><th>Missing cost cases</th></tr></thead>
<tbody>{symbol_rows or '<tr><td colspan="8">No matured signal outcomes yet.</td></tr>'}</tbody></table>
<label for="filter">Filter symbols or states</label><br><input id="filter" placeholder="Search..." oninput="filterRows()">
<table><thead><tr><th>Symbol</th><th>Catalog state</th><th>Research state</th>
<th>Sleeping score</th><th>Data confidence</th><th>Spread</th><th>Execution eligible</th><th>Measurements / reasoning</th></tr></thead>
<tbody>{table_rows}</tbody></table>
<p class="muted">Sleeping scores use each symbol's own rolling distributions. BTC and ETH are displayed as ordinary controls.
Inspect a row for raw measurements, signal availability, missing data, and rejection reasons.</p>
<script>function filterRows(){{const q=document.getElementById('filter').value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>{{r.hidden=!r.innerText.toLowerCase().includes(q)}})}}</script>
</body></html>"""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination
