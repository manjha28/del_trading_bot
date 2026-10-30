"""Streamlit dashboard for VPVR diagnostics, paper journal and CSV backtests.

Run from the repository root:
    streamlit run vpvr/dashboard.py

This UI only calls the existing public market-data, strategy, backtest and
local paper-state paths. It does not expose authenticated or live order APIs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from vpvr.backtest import BacktestConfig, run_backtest
from vpvr.cli import scan_once
from vpvr.config import VPVRConfig


st.set_page_config(page_title="VPVR Strategy Lab", page_icon="📊", layout="wide")
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.4rem; max-width: 1500px; }
      [data-testid="stMetric"] { border: 1px solid rgba(128,128,128,.22);
        padding: .8rem 1rem; border-radius: .65rem; }
      .small-muted { color: #8b929c; font-size: .85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

cfg = VPVRConfig.from_env()


def _paper_state():
    path = Path(cfg.paper_state_file)
    if not path.exists():
        return {"mode": "PAPER_ONLY", "balance": cfg.initial_paper_balance,
                "positions": {}, "pending": {}, "trades": [], "events": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("mode") != "PAPER_ONLY":
            raise ValueError("Unexpected paper-state mode")
        return payload
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"_error": str(exc)}


def _scan_rows(snapshot):
    rows = []
    for item in snapshot.get("results", []):
        specs = item.get("contract_specs") or {}
        rows.append({
            "Symbol": item.get("symbol"),
            "Status": item.get("status"),
            "Regime": (item.get("regime", {}).get("name")
                       if isinstance(item.get("regime"), dict) else item.get("regime")),
            "Funding": item.get("funding_rate_quote", item.get("funding_rate")),
            "Spread %": item.get("spread_pct"),
            "Contract verified": specs.get("spec_verified", False),
            "Contract multiplier": specs.get("contract_multiplier"),
            "Tick size": specs.get("tick_size"),
            "Min contracts": specs.get("min_qty"),
        })
    return rows


def _signals(snapshot):
    rows = []
    for item in snapshot.get("results", []):
        for entry in item.get("signals", []):
            signal = entry.get("signal", {})
            rows.append({
                "Symbol": item.get("symbol"),
                "Strategy": signal.get("strategy"),
                "Direction": signal.get("direction"),
                "Entry": signal.get("entry"),
                "Stop": signal.get("stop_loss"),
                "Targets": ", ".join(str(x) for x in signal.get("targets", [])),
                "R:R": signal.get("risk_reward"),
                "Decision": entry.get("status"),
                "Reason": entry.get("reason"),
            })
    return rows


def _verified_specs(snapshot, symbol):
    for item in snapshot.get("results", []):
        if item.get("symbol") == symbol:
            spec = item.get("contract_specs") or {}
            if spec.get("spec_verified"):
                return spec
    return None


st.title("VPVR Strategy Lab")
st.caption("Delta Exchange India · Market diagnostics · Strategy backtests · Paper journal")
st.info(
    "Research and paper-only dashboard. It does not submit exchange orders. "
    "Paper scans can update the local virtual journal at the configured paper-state path."
)

with st.sidebar:
    st.header("Market scan")
    symbols = st.multiselect(
        "Perpetual contracts",
        options=["BTCUSD", "ETHUSD"],
        default=["BTCUSD", "ETHUSD"],
    )
    scan_mode = st.radio(
        "Execution mode",
        ["Signal-only scan", "Paper scan"],
        help="Signal-only reads market data and reports setups. Paper scan may queue virtual entries in the local journal.",
    )
    run_scan = st.button("Fetch latest market data", type="primary", use_container_width=True)
    st.divider()
    st.subheader("Risk configuration")
    st.write(f"Risk per trade: **{cfg.risk_per_trade_pct:.2%}**")
    st.write(f"Daily drawdown lockout: **{cfg.daily_loss_limit_pct:.2%}**")
    st.write(f"Leverage: **{cfg.default_leverage:g}x** (max {cfg.max_leverage:g}x)")
    st.write(f"Funding threshold: **±{cfg.max_abs_funding_rate:.3%}**")
    st.caption("These values are read from VPVR environment configuration; the dashboard does not weaken risk limits.")

if run_scan:
    if not symbols:
        st.warning("Select at least one symbol.")
    else:
        with st.spinner("Fetching Delta public market data and evaluating VPVR strategies..."):
            try:
                st.session_state["vpvr_scan_snapshot"] = scan_once(
                    symbols, paper=(scan_mode == "Paper scan"), config=cfg
                )
                st.session_state["vpvr_scan_error"] = None
            except Exception as exc:
                st.session_state["vpvr_scan_error"] = str(exc)

scan_error = st.session_state.get("vpvr_scan_error")
if scan_error:
    st.error(f"Market scan failed: {scan_error}")

snapshot = st.session_state.get("vpvr_scan_snapshot")
overview_tab, detail_tab, journal_tab, backtest_tab, raw_tab = st.tabs(
    ["Overview", "Symbol diagnostics", "Paper journal", "Backtest workbench", "Raw backend data"]
)

with overview_tab:
    if not snapshot:
        st.info("Use **Fetch latest market data** in the sidebar to populate the dashboard.")
    else:
        results = snapshot.get("results", [])
        st.caption(f"Last scan mode: {snapshot.get('mode')} · Exchange: {snapshot.get('exchange')}")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Paper balance", f"{snapshot.get('account_balance', 0):,.2f}")
        k2.metric("Account equity", f"{snapshot.get('account_equity', 0):,.2f}")
        k3.metric("Open paper positions", snapshot.get("open_paper_positions", 0))
        k4.metric("Pending entries", snapshot.get("pending_paper_entries", 0))

        st.subheader("Symbol scan results")
        rows = _scan_rows(snapshot)
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.warning("No symbol results were returned.")

        st.subheader("Strategy signals and decisions")
        signals = _signals(snapshot)
        if signals:
            st.dataframe(pd.DataFrame(signals), use_container_width=True, hide_index=True)
        else:
            st.info("No qualifying signals were generated in this scan. This is different from an execution error.")

        st.subheader("Strategy coverage")
        counts = {}
        for item in results:
            for entry in item.get("signals", []):
                name = (entry.get("signal") or {}).get("strategy", "unknown")
                counts[name] = counts.get(name, 0) + 1
        if counts:
            st.bar_chart(pd.Series(counts, name="Signals").sort_values(ascending=False))
        else:
            st.caption("No signals to chart for this scan.")

with detail_tab:
    if not snapshot:
        st.info("Run a scan to inspect symbol-level diagnostics.")
    else:
        available = [r.get("symbol") for r in snapshot.get("results", []) if r.get("symbol")]
        if available:
            selected = st.selectbox("Inspect contract", available)
            item = next((r for r in snapshot["results"] if r.get("symbol") == selected), {})
            left, right = st.columns([1, 1])
            with left:
                st.subheader("Market state")
                st.write("Status:", item.get("status"))
                st.write("Regime:", item.get("regime"))
                st.write("Funding rate:", item.get("funding_rate_quote", item.get("funding_rate")))
                st.write("Spread (%):", item.get("spread_pct"))
                st.subheader("Contract verification")
                st.json(item.get("contract_specs") or {"status": "No contract metadata returned"})
            with right:
                st.subheader("Indicators and data provenance")
                st.json(item.get("indicators") or {})
            profile = item.get("profile") or {}
            levels = profile.get("price_levels") or []
            volumes = profile.get("volume_at_price") or []
            if levels and len(levels) == len(volumes):
                st.subheader("Volume Profile")
                profile_frame = pd.DataFrame(
                    {"Volume (estimated or trade-based)": volumes},
                    index=pd.Index(levels, name="Price level"),
                )
                st.caption(f"Profile source: {profile.get('source')} · Rows: {profile.get('rows')}")
                st.bar_chart(profile_frame, height=280)
                with st.expander("Full profile price levels and volume"):
                    st.dataframe(profile_frame, use_container_width=True)
            st.subheader("Signal details")
            symbol_signals = [
                x for x in item.get("signals", [])
            ]
            if symbol_signals:
                st.dataframe(pd.json_normalize(symbol_signals), use_container_width=True, hide_index=True)
            else:
                st.info("No strategy signal rows for this symbol.")
            with st.expander("Complete symbol result"):
                st.json(item)

with journal_tab:
    journal = _paper_state()
    if journal.get("_error"):
        st.error(f"Paper journal cannot be read: {journal['_error']}")
    else:
        positions = journal.get("positions", {})
        pending = journal.get("pending", {})
        trades = journal.get("trades", [])
        events = journal.get("events", [])
        j1, j2, j3, j4 = st.columns(4)
        j1.metric("Virtual balance", f"{float(journal.get('balance', cfg.initial_paper_balance)):,.2f}")
        j2.metric("Open positions", len(positions))
        j3.metric("Pending triggers", len(pending))
        j4.metric("Closed trades", len(trades))
        p1, p2 = st.columns(2)
        with p1:
            st.subheader("Open paper positions")
            st.dataframe(pd.DataFrame(list(positions.values())), use_container_width=True, hide_index=True)
        with p2:
            st.subheader("Pending entries")
            st.dataframe(pd.DataFrame(list(pending.values())), use_container_width=True, hide_index=True)
        st.subheader("Closed paper trades")
        if trades:
            trades_frame = pd.DataFrame(trades)
            if "net_pnl" in trades_frame:
                trades_frame = trades_frame.sort_values("closed_at", ascending=False, na_position="last")
            st.dataframe(trades_frame, use_container_width=True, hide_index=True)
        else:
            st.info("No closed paper trades in the journal yet.")
        with st.expander("Paper event log"):
            st.dataframe(pd.DataFrame(events), use_container_width=True, hide_index=True)
        st.caption(f"Journal file: {cfg.paper_state_file}")

with backtest_tab:
    st.subheader("Run a historical multi-timeframe strategy test")
    st.write("Upload aligned OHLCV CSVs with columns: time, open, high, low, close, volume. The existing VPVR backtester evaluates the three strategy detectors and simulates conservative 1-minute execution.")
    bt_symbol = st.selectbox("Backtest symbol", ["BTCUSD", "ETHUSD"], key="bt_symbol")
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        csv_1m = st.file_uploader("1-minute candles CSV", type=["csv"], key="bt_1m")
    with col_b:
        csv_3m = st.file_uploader("3-minute candles CSV", type=["csv"], key="bt_3m")
    with col_c:
        csv_15m = st.file_uploader("15-minute candles CSV", type=["csv"], key="bt_15m")

    verified = _verified_specs(snapshot or {}, bt_symbol) if snapshot else None
    use_manual = False
    manual_values = {}
    if verified:
        st.success("Using exchange-verified contract specifications from the latest market scan.")
        st.json(verified)
    else:
        st.warning("No verified contract specification is available from the latest scan. Backtesting can still run with explicitly manual research assumptions; this does not verify a contract or enable paper entries.")
        use_manual = st.checkbox("Use manual research-only contract assumptions", value=False)
        if use_manual:
            default_multiplier = 0.001 if bt_symbol == "BTCUSD" else 0.01
            m1, m2, m3, m4, m5 = st.columns(5)
            manual_values["contract_multiplier"] = m1.number_input("Multiplier", min_value=0.00000001, value=default_multiplier, format="%.8f")
            manual_values["lot_size"] = m2.number_input("Lot increment (contracts)", min_value=1, value=1, step=1)
            manual_values["min_qty"] = m3.number_input("Minimum (contracts)", min_value=1, value=1, step=1)
            manual_values["tick_size"] = m4.number_input("Tick size (manual)", min_value=0.00000001, value=0.5 if bt_symbol == "BTCUSD" else 0.05, format="%.8f")
            manual_values["position_size_limit"] = m5.number_input("Max order size (0 = none)", min_value=0, value=0, step=1)
            st.caption("Manual assumptions are shown in the result metadata and are never used by the live market scan or paper sizing gate.")

    can_run = bool(csv_1m and csv_3m and csv_15m and (verified or use_manual))
    if st.button("Run backtest", type="primary", disabled=not can_run):
        try:
            frames = [
                pd.read_csv(csv_1m),
                pd.read_csv(csv_3m),
                pd.read_csv(csv_15m),
            ]
            required = {"time", "open", "high", "low", "close", "volume"}
            for label, frame in zip(("1m", "3m", "15m"), frames):
                missing = required - set(frame.columns)
                if missing:
                    raise ValueError(f"{label} CSV is missing columns: {sorted(missing)}")
            spec = verified or manual_values
            position_limit = spec.get("position_size_limit")
            if not verified and position_limit == 0:
                position_limit = None
            backtest_config = BacktestConfig(
                initial_balance=cfg.initial_paper_balance,
                profile_rows=cfg.profile_rows,
                value_area_pct=cfg.value_area_pct,
                min_reward_risk=cfg.min_reward_risk,
                volume_multiple=cfg.min_volume_multiple,
                fee_rate=cfg.fee_rate,
                slippage_bps=cfg.slippage_bps,
                leverage=cfg.default_leverage,
                risk_per_trade_pct=cfg.risk_per_trade_pct,
                trailing_buffer_pct=cfg.stop_buffer_pct,
                max_hold_minutes=cfg.max_hold_minutes,
                profile_lookback_15m=cfg.profile_lookback_15m,
            )
            result = run_backtest(
                bt_symbol, frames[0], frames[1], frames[2], backtest_config,
                contract_multiplier=float(spec["contract_multiplier"]),
                lot_size=float(spec["lot_size"]),
                min_qty=float(spec["min_qty"]),
                tick_size=float(spec["tick_size"]),
                position_size_limit=position_limit,
            )
            result["contract_spec_source"] = (
                "latest_delta_product_metadata_verified" if verified
                else "manual_research_assumptions_not_exchange_verified"
            )
            st.session_state["vpvr_last_backtest"] = result
            st.session_state["vpvr_backtest_error"] = None
        except Exception as exc:
            st.session_state["vpvr_backtest_error"] = str(exc)

    bt_error = st.session_state.get("vpvr_backtest_error")
    if bt_error:
        st.error(f"Backtest failed: {bt_error}")
    result = st.session_state.get("vpvr_last_backtest")
    if result:
        metrics = result.get("metrics", {})
        st.divider()
        st.subheader("Backtest results")
        st.caption(f"{result.get('symbol')} · Contract sizing source: {result.get('contract_spec_source')}")
        a, b, c, d, e = st.columns(5)
        a.metric("Trades", metrics.get("trades", 0))
        b.metric("Win rate", f"{metrics.get('win_rate', 0):.1%}")
        c.metric("Net PnL", f"{metrics.get('net_pnl', 0):,.2f}")
        d.metric("Max drawdown", f"{metrics.get('max_drawdown_pct', 0):.1%}")
        e.metric("Ending equity", f"{metrics.get('ending_equity', 0):,.2f}")
        strategy_rows = [
            {"Strategy": name, **values}
            for name, values in result.get("strategy_metrics", {}).items()
        ]
        if strategy_rows:
            st.subheader("Results by strategy")
            st.dataframe(pd.DataFrame(strategy_rows), use_container_width=True, hide_index=True)
        st.subheader("Equity curve")
        st.line_chart(pd.Series(result.get("equity_curve", []), name="Equity"))
        st.subheader("Simulated trades")
        trade_frame = pd.DataFrame(result.get("trades", []))
        st.dataframe(trade_frame, use_container_width=True, hide_index=True)
        st.subheader("Signal counts and skipped-entry diagnostics")
        st.json({"signals_detected": result.get("signals_detected"),
                 "skipped": result.get("skipped"),
                 "limitations": result.get("limitations")})
        st.download_button(
            "Download complete backtest JSON",
            data=json.dumps(result, indent=2, default=str),
            file_name=f"{result.get('symbol', 'vpvr').lower()}_backtest.json",
            mime="application/json",
        )
        if not trade_frame.empty:
            st.download_button(
                "Download simulated trades CSV",
                data=trade_frame.to_csv(index=False),
                file_name=f"{result.get('symbol', 'vpvr').lower()}_trades.csv",
                mime="text/csv",
            )

with raw_tab:
    st.subheader("Raw scan response")
    st.caption("Exact structured result returned by the VPVR backend for the most recent scan.")
    if snapshot:
        st.json(snapshot)
    else:
        st.info("No scan response is available yet.")
