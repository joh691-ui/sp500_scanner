"""
backtest.py - Momentum Top-10 Backtest (US S&P 500 & Stockholmsborsen)
=======================================================================
Strategy:
  • Each month: rank all stocks by 6-month momentum (skipping most recent month)
  • Buy the top 10 equal-weight
  • Hold until next rebalance, then repeat

Benchmark: equal-weight portfolio of ALL stocks with data that month

Data source: the scanner's price_cache/ directory (Parquet files).
If the cache is missing, the script downloads fresh data from Nasdaq API
and saves it for future use.

Run:
    python backtest.py
"""

import os
import sys
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.ticker as mtick

# -- Configuration ------------------------------------------------------------
CACHE_DIR            = "price_cache"
TOP_N                = 10
MOMENTUM_LB_MONTHS   = 6    # lookback for momentum signal
SKIP_MONTHS          = 1    # skip most-recent month (avoid reversal noise)
REBALANCE_FREQ       = "ME" # monthly-end rebalancing
DOWNLOAD_YEARS       = 11   # years of history to download if no cache
MAX_WORKERS          = 25
COURTAGE             = 0.0015  # 0.15% per trade (buy + sell = 0.30% per rebalance)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# -- Nasdaq API helpers (same as scanner.py) ----------------------------------
def fetch_us_history(symbol, from_date, to_date=None):
    if to_date is None:
        to_date = datetime.now().strftime("%Y-%m-%d")
    clean_sym = str(symbol).replace("-", ".")
    if clean_sym in {"BF.B", "BRK.B"}:
        return pd.DataFrame()
    url = f"https://api.nasdaq.com/api/quote/{clean_sym}/chart"
    try:
        r = requests.get(url, params={"assetclass": "stocks", "fromdate": from_date, "todate": to_date},
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return pd.DataFrame()
        points = (r.json().get("data") or {}).get("chart") or []
        if not points:
            return pd.DataFrame()
        records = []
        for p in points:
            z = p.get("z", {})
            try:    close_val = float(z.get("close") or 0) or None
            except: close_val = None
            records.append({"date": z.get("dateTime"), "close": close_val})
        df = pd.DataFrame(records)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=False)
        df = df.set_index("date").sort_index()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
        df = df.groupby(df.index).last()
        return df.dropna(subset=["close"])
    except Exception:
        return pd.DataFrame()


def fetch_sto_instruments():
    """Return list of {symbol, fullName, sector, orderbookId} for Stockholmsborsen."""
    results = []
    base = "https://api.nasdaq.com/api/nordic"
    for segment in ["LARGE_CAP", "MID_CAP", "SMALL_CAP"]:
        page = 1
        while True:
            try:
                r = requests.get(f"{base}/screener/shares",
                                 params={"market": "STO", "segment": segment,
                                         "category": "MAIN_MARKET", "tableonly": "true",
                                         "lang": "en", "size": 200, "page": page},
                                 headers=HEADERS, timeout=15)
                if r.status_code != 200:
                    break
                data  = r.json().get("data", {})
                rows  = data.get("instrumentListing", {}).get("rows", [])
                if not rows:
                    break
                for row in rows:
                    oid = row.get("orderbookId", "")
                    sym = row.get("symbol", "")
                    if sym and oid:
                        results.append({"symbol": sym, "fullName": row.get("fullName", sym),
                                        "sector": row.get("sector", "Unknown"), "orderbookId": oid})
                pagination = data.get("pagination", {})
                if page >= pagination.get("totalPages", 1):
                    break
                page += 1
            except Exception:
                break
    seen, unique = set(), []
    for r in results:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            unique.append(r)
    return unique


def fetch_sto_history(orderbook_id, from_date, to_date=None):
    if to_date is None:
        to_date = datetime.now().strftime("%Y-%m-%d")
    try:
        r = requests.get(f"https://api.nasdaq.com/api/nordic/instruments/{orderbook_id}/chart",
                         params={"assetClass": "SHARES", "lang": "en",
                                 "fromDate": from_date, "toDate": to_date},
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return pd.DataFrame()
        points = r.json().get("data", {}).get("CP", [])
        if not points:
            return pd.DataFrame()
        records = []
        for p in points:
            z = p.get("z", {})
            try:    close_val = float(z.get("close") or 0) or None
            except: close_val = None
            records.append({"date": z.get("dateTime"), "close": close_val})
        df = pd.DataFrame(records)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=False)
        df = df.set_index("date").sort_index()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
        df = df.groupby(df.index).last()
        return df.dropna(subset=["close"])
    except Exception:
        return pd.DataFrame()


def _inject_today(prices: pd.DataFrame, market: str, last_date) -> pd.DataFrame:
    """Fetch live/latest prices for today and append as today's row if not already present."""
    today = pd.Timestamp(datetime.now().date())
    if last_date >= today:
        return prices  # already have today

    if market != "SP500":
        # Nordic real-time via chart endpoint - skip (chart API updates live for STO)
        return prices

    print(f"[{market}] Fetching live today prices ({today.date()}) from Nasdaq quote API ...")
    tickers     = prices.columns.tolist()
    today_dict  = fetch_today_quotes(tickers)
    n           = len(today_dict)
    if n == 0:
        print(f"[{market}] No live quotes returned (market may be closed or pre-market).")
        return prices

    today_row = pd.Series(today_dict, name=today)
    # Reindex to match existing columns (NaN for missing)
    today_row = today_row.reindex(prices.columns)
    updated   = pd.concat([prices, today_row.to_frame().T])
    updated   = updated.groupby(level=0).last()
    print(f"[{market}] Injected today's prices for {n}/{len(tickers)} stocks "
          f"(last: {updated.index.max().date()}).")
    return updated


def fetch_today_quotes(tickers: list) -> dict:
    """
    Fetch the latest/live last-sale price for each ticker via Nasdaq quote info API.
    Returns dict of {ticker: float_price} for tickers that return data.
    Works intraday (live) and after close.
    """
    today_prices = {}

    def _get(t):
        clean = t.replace("-", ".")
        url = f"https://api.nasdaq.com/api/quote/{clean}/info"
        try:
            r = requests.get(url, params={"assetclass": "stocks"},
                             headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return t, None
            data = r.json().get("data", {})
            primary = data.get("primaryData", {})
            raw = primary.get("lastSalePrice", "")        # e.g. "$123.45"
            raw = raw.replace("$", "").replace(",", "").strip()
            price = float(raw) if raw else None
            return t, price
        except Exception:
            return t, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_get, t): t for t in tickers}
        done = 0
        for f in as_completed(futures):
            done += 1
            if done % 100 == 0:
                print(f"  Today quotes: {done}/{len(tickers)} done...")
            t, price = f.result()
            if price:
                today_prices[t] = price

    return today_prices


# -- Data loading / downloading ------------------------------------------------------------
def _fetch_incremental(market: str, items, fetch_from: str) -> pd.DataFrame:
    """Download close prices from fetch_from to today. items = list of tickers or instruments."""
    today  = datetime.now().strftime("%Y-%m-%d")
    all_px = {}
    if market == "SP500":
        def _fetch(t):
            df = fetch_us_history(t, fetch_from, today)
            return t, df["close"] if not df.empty else None
    else:
        oid_map = {r["symbol"]: r["orderbookId"] for r in items}
        items   = list(oid_map.keys())
        def _fetch(sym):
            oid = oid_map.get(sym)
            if not oid:
                return sym, None
            df = fetch_sto_history(oid, fetch_from, today)
            return sym, df["close"] if not df.empty else None
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch, item): item for item in items}
        done = 0
        for f in as_completed(futures):
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(items)} done...")
            key, s = f.result()
            if s is not None:
                all_px[key] = s
    return pd.DataFrame(all_px)


def load_or_download(market: str) -> pd.DataFrame:
    """Load prices from cache. If stale, fetch only the missing days and update cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"prices_{market}.parquet")
    from_date  = (datetime.now() - timedelta(days=365 * DOWNLOAD_YEARS)).strftime("%Y-%m-%d")
    today_str  = datetime.now().strftime("%Y-%m-%d")

    # -- Cache exists: check freshness ----------------------------------------------
    if os.path.exists(cache_file):
        cached = pd.read_parquet(cache_file)
        cached.index = pd.to_datetime(cached.index)
        last_date   = cached.index.max()
        years_avail = (last_date - cached.index.min()).days / 365.25
        print(f"[{market}] Cache: {len(cached.columns)} stocks, "
              f"{years_avail:.1f} years (last: {last_date.date()})")

        fetch_from = (last_date + timedelta(days=1)).strftime("%Y-%m-%d")
        if fetch_from >= today_str:
            print(f"[{market}] Cache is up-to-date.")
            cached = _inject_today(cached, market, last_date)
            return cached

        days_old = (datetime.now() - last_date).days
        print(f"[{market}] Cache is {days_old}d old - fetching new data since {fetch_from} ...")

        items  = cached.columns.tolist() if market == "SP500" else fetch_sto_instruments()
        new_df = _fetch_incremental(market, items, fetch_from)

        if not new_df.empty:
            merged = pd.concat([cached, new_df]).groupby(level=0).last()
            merged.to_parquet(cache_file)
            print(f"[{market}] Updated - last date now {merged.index.max().date()}")
            merged = _inject_today(merged, market, merged.index.max())
            return merged
        else:
            print(f"[{market}] No new data returned (market may not have closed yet). Using cache.")
            cached = _inject_today(cached, market, last_date)
            return cached

    # -- No cache: full download --------------------------------------------------
    print(f"[{market}] No cache. Downloading {DOWNLOAD_YEARS} years from Nasdaq API ...")
    print(f"         (One-time download; cached in {cache_file})")
    if market == "SP500":
        print("[SP500]  Fetching S&P 500 component list from Wikipedia...")
        from io import StringIO
        resp = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                            headers=HEADERS, timeout=15)
        tables     = pd.read_html(StringIO(resp.text))
        sp_table   = tables[0]
        sp_table.columns = sp_table.columns.str.strip()
        ticker_col = [c for c in sp_table.columns if "Symbol" in c or "Ticker" in c][0]
        items      = sp_table[ticker_col].str.replace(".", "-", regex=False).tolist()
        print(f"[SP500]  Downloading {len(items)} tickers with {MAX_WORKERS} workers ...")
    else:
        print("[STO]    Fetching Stockholmsborsen instrument list...")
        items = fetch_sto_instruments()
        print(f"[STO]    Downloading {len(items)} stocks with {MAX_WORKERS} workers ...")

    prices = _fetch_incremental(market, items, from_date)
    if prices.empty:
        print(f"ERROR: No data for {market}. Check network.")
        import sys; sys.exit(1)

    prices.to_parquet(cache_file)
    print(f"[{market}] Saved {len(prices.columns)} stocks to {cache_file}")
    return prices



# -- Momentum calculation -------------------------------------------------------
def momentum_score(prices_up_to_date: pd.DataFrame,
                   lb_months: int = MOMENTUM_LB_MONTHS,
                   skip_months: int = SKIP_MONTHS) -> pd.Series:
    """6-month momentum (1-month skipped) for all stocks in the slice."""
    lb_days   = lb_months   * 21
    skip_days = skip_months * 21
    needed    = lb_days + skip_days + 5
    if len(prices_up_to_date) < needed:
        return pd.Series(dtype=float)
    recent_px = prices_up_to_date.iloc[-skip_days - 1] if skip_days > 0 else prices_up_to_date.iloc[-1]
    old_px    = prices_up_to_date.iloc[-lb_days - skip_days - 1]
    mom       = (recent_px / old_px - 1).dropna()
    return mom


def calc_volatility(prices_slice: pd.DataFrame, days: int = 20) -> pd.Series:
    """Calculate annualized volatility over the last N trading days."""
    if len(prices_slice) < days + 1:
        return pd.Series(np.nan, index=prices_slice.columns)
    
    recent = prices_slice.iloc[-(days+1):]
    rets = recent.pct_change().dropna(how='all')
    vol = rets.std() * np.sqrt(252)
    return vol



# -- Core backtest engine -------------------------------------------------------
def run_backtest(prices: pd.DataFrame, label: str, top_n: int = TOP_N,
                 lb_months: int = MOMENTUM_LB_MONTHS, skip_months: int = SKIP_MONTHS,
                 low_vol_filter: bool = False, vol_pool_multiplier: int = 3, vol_days: int = 20,
                 ma_filter: bool = False, ma_months: int = 10):
    """
    Returns (portfolio equity series, benchmark equity series, list of monthly holdings).
    """
    prices = prices.sort_index()
    # Only keep stocks with enough history
    min_rows = (lb_months + skip_months + 2) * 21
    prices   = prices.loc[:, prices.count() > min_rows]

    rebal_dates = pd.date_range(start=prices.index.min(), end=prices.index.max(), freq=REBALANCE_FREQ)
    rebal_dates = rebal_dates[rebal_dates >= prices.index.min() + pd.DateOffset(months=lb_months + skip_months + 1)]

    print(f"\n[{label}] Running backtest over {len(rebal_dates)} rebalance dates "
          f"({rebal_dates[0].date()} -> {rebal_dates[-1].date()}) ...")

    port_value  = 1.0
    bench_value = 1.0
    port_curve  = []
    bench_curve = []
    dates_out   = []
    holdings_log= []

    holdings = None

    for i, rebal_date in enumerate(rebal_dates):
        slice_prices = prices[prices.index <= rebal_date]

        # Score stocks and pick top N
        mom = momentum_score(slice_prices, lb_months=lb_months, skip_months=skip_months)
        if mom.empty or len(mom) < top_n:
            continue
        if low_vol_filter:
            pool_size = top_n * vol_pool_multiplier
            top_mom_pool = mom.nlargest(pool_size).index.tolist()
            
            pool_prices = slice_prices[top_mom_pool]
            vol = calc_volatility(pool_prices, days=vol_days)
            
            if not vol.empty and not vol.isna().all():
                new_holdings = vol.nsmallest(top_n).index.tolist()
            else:
                new_holdings = mom.nlargest(top_n).index.tolist()
        else:
            new_holdings = mom.nlargest(top_n).index.tolist()

        if holdings is not None and i > 0:
            prev_date   = rebal_dates[i - 1]
            period      = prices[(prices.index > prev_date) & (prices.index <= rebal_date)]

            # Portfolio return (equal weight in previous holdings)
            port_rets = []
            for t in holdings:
                if t in period.columns:
                    s = period[t].dropna()
                    if len(s) >= 2:
                        port_rets.append(s.iloc[-1] / s.iloc[0] - 1)
            port_ret = float(np.mean(port_rets)) if port_rets else 0.0

            # Benchmark: equal weight of ALL stocks
            bench_rets = []
            for t in period.columns:
                s = period[t].dropna()
                if len(s) >= 2:
                    bench_rets.append(s.iloc[-1] / s.iloc[0] - 1)
            bench_ret = float(np.mean(bench_rets)) if bench_rets else 0.0

            # Determine if we should hold cash this month based on MA filter
            # We calculate this using the benchmark equity curve *up to* prev_date
            is_invested = True
            if ma_filter and len(bench_curve) >= ma_months:
                # Get the benchmark index values for the last 'ma_months' rebalance dates
                # bench_curve holds the equity curve points
                recent_bench = bench_curve[-ma_months:]
                ma_val = np.mean(recent_bench)
                if bench_curve[-1] < ma_val:
                    is_invested = False
                    
            if not is_invested:
                # If we go to cash, our return is 0 for this month (we still pay courtage to sell)
                port_ret = 0.0
                # If we just went to cash this month, we pay to sell
                # if we were already in cash, we pay nothing
                # We'll approximate: if holdings was not empty, we pay 1x courtage to sell, and 0 to buy
            
            # Apply courtage (sell all old + buy all new = 2 × COURTAGE per position)
            courtage_cost = COURTAGE * 2 if is_invested else (COURTAGE if holdings else 0)
            port_value  *= (1 + port_ret) * (1 - courtage_cost)
            bench_value *= (1 + bench_ret)

            port_curve.append(port_value)
            bench_curve.append(bench_value)
            dates_out.append(rebal_date)
            holdings_log.append((rebal_date, holdings, round(port_ret * 100, 2)))

        holdings = new_holdings

    port_eq  = pd.Series(port_curve,  index=dates_out)
    bench_eq = pd.Series(bench_curve, index=dates_out)
    return port_eq, bench_eq, holdings_log


# -- Parameter Optimization ------------------------------------------------------
OPT_LOOKBACKS  = [1, 2, 3, 4, 5, 6, 9, 12]
OPT_SKIPS      = [0, 1, 2]
OPT_TOP_NS     = [5, 10, 15, 20]

def run_optimization(prices: pd.DataFrame, label: str) -> pd.DataFrame:
    """
    Grid search over (lookback, skip, top_n) and return a DataFrame of results
    sorted by Sharpe ratio. Prints the top 15 combinations and plots a heatmap.
    """
    print(f"\n{'='*60}")
    print(f"  OPTIMIZING PARAMETERS FOR: {label}")
    print(f"  Grid: {len(OPT_LOOKBACKS)} lookbacks x {len(OPT_SKIPS)} skips x {len(OPT_TOP_NS)} top-N")
    print(f"  = {len(OPT_LOOKBACKS)*len(OPT_SKIPS)*len(OPT_TOP_NS)} combinations")
    print(f"{'='*60}")

    records = []
    total = len(OPT_LOOKBACKS) * len(OPT_SKIPS) * len(OPT_TOP_NS)
    done  = 0

    for lb in OPT_LOOKBACKS:
        for skip in OPT_SKIPS:
            for n in OPT_TOP_NS:
                done += 1
                port_eq, bench_eq, _ = run_backtest(
                    prices, label, top_n=n, lb_months=lb, skip_months=skip
                )
                if port_eq.empty or len(port_eq) < 6:
                    continue
                s = stats(port_eq, "")
                if not s:
                    continue
                records.append({
                    "Lookback_m":  lb,
                    "Skip_m":      skip,
                    "Top_N":       n,
                    "CAGR_%":      round(s["CAGR"]*100, 1),
                    "Sharpe":      round(s["Sharpe"], 2),
                    "MaxDD_%":     round(s["MaxDD"]*100, 1),
                    "WinRate_%":   round(s["WinRate"]*100, 1),
                    "TotalRet_%":  round(s["TotalReturn"]*100, 1),
                })
                if done % 10 == 0:
                    print(f"  Progress: {done}/{total} combos tested...")

    if not records:
        print("No valid results found.")
        return pd.DataFrame()

    df = pd.DataFrame(records).sort_values("Sharpe", ascending=False)
    df = df.reset_index(drop=True)
    df.index += 1  # 1-based ranking

    # Print top 15
    print(f"\n  TOP 15 COMBINATIONS (sorted by Sharpe):")
    print(f"  {'#':<4} {'LB':>4} {'Skip':>5} {'N':>4}  {'CAGR%':>7} {'Sharpe':>7} {'MaxDD%':>7} {'Win%':>6}")
    print(f"  {'-'*52}")
    for rank, row in df.head(15).iterrows():
        print(f"  {rank:<4} {int(row.Lookback_m):>3}m {int(row.Skip_m):>4}m {int(row.Top_N):>4}  "
              f"{row['CAGR_%']:>7.1f} {row.Sharpe:>7.2f} {row['MaxDD_%']:>7.1f} {row['WinRate_%']:>6.1f}")

    best = df.iloc[0]
    print(f"\n  BEST: LB={int(best.Lookback_m)}m, skip={int(best.Skip_m)}m, top {int(best.Top_N)}")
    print(f"        CAGR {best['CAGR_%']:.1f}%  Sharpe {best.Sharpe:.2f}  MaxDD {best['MaxDD_%']:.1f}%")

    # Plot Sharpe heatmap (for best top_n)
    best_n    = int(best.Top_N)
    hm_data   = df[df.Top_N == best_n].pivot_table(
        index="Lookback_m", columns="Skip_m", values="Sharpe", aggfunc="max"
    )

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), facecolor="#0a0e17")
    fig.suptitle(f"Parameter Optimization - {label} (Top {best_n})",
                 color="#e2e8f0", fontsize=13, fontweight="bold")

    # Heatmap
    ax1 = axes[0]
    ax1.set_facecolor("#0f172a")
    if not hm_data.empty:
        import matplotlib.colors as mcolors
        cmap = plt.cm.RdYlGn
        im = ax1.imshow(hm_data.values, cmap=cmap, aspect="auto",
                        vmin=max(0, hm_data.values.min()), vmax=hm_data.values.max())
        ax1.set_xticks(range(len(hm_data.columns)))
        ax1.set_xticklabels([f"skip {int(c)}m" for c in hm_data.columns],
                            color="#94a3b8", fontsize=9)
        ax1.set_yticks(range(len(hm_data.index)))
        ax1.set_yticklabels([f"LB {int(r)}m" for r in hm_data.index],
                            color="#94a3b8", fontsize=9)
        ax1.set_title(f"Sharpe Heatmap (Top {best_n})", color="#e2e8f0", fontsize=10)
        for i in range(len(hm_data.index)):
            for j in range(len(hm_data.columns)):
                val = hm_data.values[i, j]
                if not np.isnan(val):
                    ax1.text(j, i, f"{val:.2f}", ha="center", va="center",
                             fontsize=8, color="black" if val > 0.4 else "white", fontweight="bold")
        plt.colorbar(im, ax=ax1, shrink=0.8).ax.tick_params(colors="#94a3b8")

    # Bar chart: top 20 by Sharpe
    ax2 = axes[1]
    ax2.set_facecolor("#0f172a")
    for spine in ax2.spines.values():
        spine.set_color("#1e293b")
    top20 = df.head(20)
    colors_bar = ["#22c55e" if sh >= 0.6 else "#eab308" if sh >= 0.4 else "#ef4444"
                  for sh in top20.Sharpe]
    labels_bar = [f"LB{int(r.Lookback_m)}s{int(r.Skip_m)}n{int(r.Top_N)}"
                  for _, r in top20.iterrows()]
    ax2.barh(range(len(top20)), top20.Sharpe.values, color=colors_bar, alpha=0.85)
    ax2.set_yticks(range(len(top20)))
    ax2.set_yticklabels(labels_bar, fontsize=7.5, color="#94a3b8")
    ax2.invert_yaxis()
    ax2.set_xlabel("Sharpe Ratio", color="#64748b", fontsize=9)
    ax2.set_title("Top 20 Combinations by Sharpe", color="#e2e8f0", fontsize=10)
    ax2.tick_params(colors="#94a3b8")
    ax2.grid(True, color="#1e293b", lw=0.5, axis="x")
    ax2.axvline(0.5, color="#3b82f6", lw=1, ls="--", alpha=0.6, label="Sharpe 0.5")
    ax2.legend(fontsize=8, facecolor="#0f172a", edgecolor="#1e293b", labelcolor="#e2e8f0")

    plt.tight_layout()
    out = "optimization_results.png"
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"\n  Heatmap saved to {out}")
    plt.show()

    return df



# -- STO Strategy Comparison (Raw vs MTF-filtered) ------------------------
def run_comparison_sto(prices: pd.DataFrame,
                       lb_months: int = 9, skip_months: int = 2, top_n: int = 15):
    """
    Compares STO strategies on one chart:
      A) Raw Momentum - baseline
      B) Low Volatility Momentum (Top 45 by Mom -> Pick 15 lowest vol)
      C) Low Volatility Momentum (Top 75 by Mom -> Pick 15 lowest vol)
    """
    print(f"\n{'='*60}")
    print("  STO STRATEGY COMPARISON: Low Volatility Momentum")
    print(f"  LB={lb_months}m, skip={skip_months}m, Final Top {top_n}")
    print(f"{'='*60}")

    variants = [
        ("Low Vol Mom (Pool: 3x)", True, 3, False, 10),
        ("Low Vol Mom + 5m MA filter", True, 3, True, 5),
        ("Low Vol Mom + 10m MA filter", True, 3, True, 10),
    ]
    COLORS = ["#64748b", "#3b82f6", "#22c55e"]
    results = {}

    for label, min_conf, tfs, ma, ma_len in variants:
        eq, bench, _ = run_backtest(
            prices, label, top_n=top_n,
            lb_months=lb_months, skip_months=skip_months,
            low_vol_filter=min_conf, vol_pool_multiplier=tfs,
            ma_filter=ma, ma_months=ma_len
        )
        s   = stats(eq, label)
        results[label] = (eq, bench, s)
        print_stats(s)

    # -- Plot comparison ------------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), facecolor="#0a0e17")
    fig.suptitle("STO Momentum: Low Volatility + MA Filters",
                 color="#e2e8f0", fontsize=13, fontweight="bold")

    ax1, ax2 = axes

    for ax in axes:
        ax.set_facecolor("#0f172a")
        for spine in ax.spines.values():
            spine.set_color("#1e293b")
        ax.tick_params(colors="#94a3b8", labelsize=8)
        ax.grid(True, color="#1e293b", lw=0.5)

    # Equity curves (log scale)
    ax1.set_title("Equity Curve (log scale)", color="#e2e8f0", fontsize=10, fontweight="bold")
    ax1.set_ylabel("Portfolio value", color="#64748b", fontsize=8)

    bench_plotted = False
    for (label, _, __, ___, ____), color in zip(variants, COLORS):
        eq, bench, s = results[label]
        if not eq.empty:
            cagr = s.get("CAGR", 0) * 100 if s else 0
            sharpe = s.get("Sharpe", 0) if s else 0
            ax1.plot(eq.index, eq, color=color, lw=2,
                     label=f"{label}  (CAGR {cagr:.1f}%, Sharpe {sharpe:.2f})")
        if not bench_plotted and not bench.empty:
            ax1.plot(bench.index, bench, color="#475569", lw=1.2, ls="--", alpha=0.7,
                     label="Equal-weight benchmark")
            bench_plotted = True

    ax1.set_yscale("log")
    import matplotlib.ticker as mtick
    ax1.yaxis.set_major_formatter(mtick.FuncFormatter(lambda x, _: f"{x:.1f}x"))
    ax1.yaxis.set_minor_formatter(mtick.NullFormatter())
    ax1.legend(fontsize=8, facecolor="#0f172a", edgecolor="#1e293b", labelcolor="#e2e8f0")

    # Drawdowns
    ax2.set_title("Drawdown", color="#e2e8f0", fontsize=10, fontweight="bold")
    ax2.set_ylabel("Drawdown", color="#64748b", fontsize=8)
    for (label, _, __, ___, ____), color in zip(variants, COLORS):
        eq, _, __ = results[label]
        if not eq.empty:
            dd = eq / eq.cummax() - 1
            ax2.plot(dd.index, dd.values, color=color, lw=1.5, label=label)
            ax2.fill_between(dd.index, dd.values, 0, color=color, alpha=0.12)
    ax2.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax2.legend(fontsize=8, facecolor="#0f172a", edgecolor="#1e293b", labelcolor="#e2e8f0")

    # Stats table at bottom
    rows = []
    for label, _, __, ___, ____ in variants:
        s = results[label][2]
        if s:
            rows.append([label,
                         f"{s['CAGR']*100:.1f}%",
                         f"{s['Sharpe']:.2f}",
                         f"{s['MaxDD']*100:.1f}%",
                         f"{s['WinRate']*100:.0f}%"])
    if rows:
        col_labels = ["Strategy", "CAGR", "Sharpe", "MaxDD", "Win%"]
        tbl = fig.add_axes([0.15, -0.08, 0.7, 0.12])
        tbl.axis("off")
        t = tbl.table(cellText=rows, colLabels=col_labels,
                      cellLoc="center", loc="center")
        t.auto_set_font_size(False)
        t.set_fontsize(8.5)
        for (r, c), cell in t.get_celld().items():
            cell.set_facecolor("#1e293b" if r == 0 else "#0f172a")
            cell.set_text_props(color="#e2e8f0")
            cell.set_edgecolor("#334155")

    plt.tight_layout()
    out = "sto_comparison.png"
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"\n  Comparison chart saved to {out}")
    plt.show()

# -- Statistics ----------------------------------------------------------------
def stats(eq: pd.Series, label: str):
    if eq.empty or len(eq) < 2:
        print(f"{label}: insufficient data")
        return {}
    rets  = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr  = (eq.iloc[-1]) ** (1 / years) - 1
    sharpe= (rets.mean() / rets.std()) * np.sqrt(12) if rets.std() > 0 else 0
    dd    = (eq / eq.cummax() - 1)
    max_dd= dd.min()
    win   = (rets > 0).mean()
    best  = rets.max()
    worst = rets.min()
    return {"CAGR": cagr, "Sharpe": sharpe, "MaxDD": max_dd, "WinRate": win,
            "BestMonth": best, "WorstMonth": worst, "TotalReturn": eq.iloc[-1] - 1,
            "Years": years, "Label": label}


def print_stats(s: dict):
    if not s:
        return
    print(f"\n{'-'*46}")
    print(f"  {s['Label']}")
    print(f"{'-'*46}")
    print(f"  Period        : {s['Years']:.1f} years")
    print(f"  Total return  : {s['TotalReturn']*100:+.1f}%")
    print(f"  CAGR          : {s['CAGR']*100:.1f}%")
    print(f"  Sharpe (mon.) : {s['Sharpe']:.2f}")
    print(f"  Max drawdown  : {s['MaxDD']*100:.1f}%")
    print(f"  Win rate      : {s['WinRate']*100:.1f}%")
    print(f"  Best month    : {s['BestMonth']*100:+.1f}%")
    print(f"  Worst month   : {s['WorstMonth']*100:+.1f}%")


# -- Plotting ------------------------------------------------------------------
def plot_results(us_port, us_bench, sto_port, sto_bench):
    fig = plt.figure(figsize=(15, 10), facecolor="#0a0e17")
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.45, wspace=0.3)

    COLORS = {
        "port_us":  "#3b82f6",
        "bench_us": "#64748b",
        "port_sto": "#22c55e",
        "bench_sto": "#64748b",
    }

    def _ax(pos, title, port, bench, pc, bc, ylabel="Portfolio value (log scale)"):
        ax = fig.add_subplot(pos)
        ax.set_facecolor("#0f172a")
        for spine in ax.spines.values():
            spine.set_color("#1e293b")
        ax.tick_params(colors="#94a3b8", labelsize=8)
        ax.set_title(title, color="#e2e8f0", fontsize=11, fontweight="bold", pad=8)
        ax.set_ylabel(ylabel, color="#64748b", fontsize=8)

        if not port.empty:
            ax.plot(port.index,  port,  color=pc, lw=2,   label=f"Top {TOP_N} + courtage")
        if not bench.empty:
            ax.plot(bench.index, bench, color=bc, lw=1.2, ls="--", alpha=0.7, label="Equal-weight benchmark")
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda x, _: f"{x:.1f}x"))
        ax.yaxis.set_minor_formatter(mtick.NullFormatter())
        ax.grid(True, color="#1e293b", lw=0.5, which="both")
        ax.legend(fontsize=8, facecolor="#0f172a", edgecolor="#1e293b", labelcolor="#e2e8f0")
        return ax

    _ax(gs[0, 0], f"[US] S&P 500 - Top {TOP_N} Momentum vs Benchmark",
        us_port, us_bench, COLORS["port_us"], COLORS["bench_us"])

    _ax(gs[0, 1], f"[SE] Stockholmsborsen - Top {TOP_N} Momentum vs Benchmark",
        sto_port, sto_bench, COLORS["port_sto"], COLORS["bench_sto"])

    # Drawdown charts
    def _dd_ax(pos, title, port, bench, pc):
        ax = fig.add_subplot(pos)
        ax.set_facecolor("#0f172a")
        for spine in ax.spines.values():
            spine.set_color("#1e293b")
        ax.tick_params(colors="#94a3b8", labelsize=8)
        ax.set_title(title, color="#e2e8f0", fontsize=10, fontweight="bold", pad=8)
        ax.set_ylabel("Drawdown", color="#64748b", fontsize=8)
        if not port.empty:
            dd_p = port / port.cummax() - 1
            ax.fill_between(dd_p.index, dd_p.values, 0, color=pc, alpha=0.35, label="Strategy")
            ax.plot(dd_p.index,  dd_p.values,  color=pc, lw=1)
        if not bench.empty:
            dd_b = bench / bench.cummax() - 1
            ax.plot(dd_b.index, dd_b.values, color="#64748b", lw=1, ls="--", alpha=0.7, label="Benchmark")
        ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
        ax.grid(True, color="#1e293b", lw=0.5)
        ax.legend(fontsize=8, facecolor="#0f172a", edgecolor="#1e293b", labelcolor="#e2e8f0")

    _dd_ax(gs[1, 0], "[US] Drawdown - S&P 500 Strategy",  us_port,  us_bench,  COLORS["port_us"])
    _dd_ax(gs[1, 1], "[SE] Drawdown - Stockholmsborsen", sto_port, sto_bench, COLORS["port_sto"])

    fig.suptitle("Stock Momentum Scanner - Top 10 Backtest",
                 color="#e2e8f0", fontsize=14, fontweight="bold", y=0.98)

    out_path = "backtest_results.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"\nChart saved to {out_path}")
    plt.show()


# -- Main ----------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("  STOCK MOMENTUM SCANNER - TOP 10 BACKTEST")
    print(f"  Strategy: Top {TOP_N} by {MOMENTUM_LB_MONTHS}m momentum, monthly rebalance")
    print(f"  Benchmark: equal-weight all available stocks")
    print("=" * 60)

    # Load / download prices
    us_prices  = load_or_download("SP500")
    sto_prices = load_or_download("STO")

    # Run backtests
    us_port,  us_bench,  us_log  = run_backtest(us_prices,  "S&P 500")
    sto_port, sto_bench, sto_log = run_backtest(sto_prices, "Stockholmsborsen")

    # Print statistics
    us_st   = stats(us_port,   f"S&P 500 - Top {TOP_N} Momentum")
    usb_st  = stats(us_bench,  f"S&P 500 - Equal-weight Benchmark")
    sto_st  = stats(sto_port,  f"STO    - Top {TOP_N} Momentum")
    stob_st = stats(sto_bench, f"STO    - Equal-weight Benchmark")

    print_stats(us_st)
    print_stats(usb_st)
    print_stats(sto_st)
    print_stats(stob_st)

    # -- TODAY'S HOLDINGS ------------------------------------------------------
    print(f"\n{'='*60}")
    print("  CURRENT HOLDINGS - BUY THESE TODAY")
    print(f"  Courtage: {COURTAGE*100:.2f}% per trade | Rebalance: monthly")
    print(f"{'='*60}")

    def current_top(prices, market_name):
        mom = momentum_score(prices.sort_index())
        if mom.empty:
            print(f"  [{market_name}] Insufficient data for current rankings.")
            return
        top = mom.nlargest(TOP_N)
        print(f"\n  * {market_name} - Top {TOP_N} (as of {prices.index.max().date()})")
        print(f"  {'#':<4} {'Ticker':<12} {'6m Momentum':>12}")
        print(f"  {'-'*30}")
        for rank, (ticker, val) in enumerate(top.items(), 1):
            bar = '#' * int(abs(val) * 10)
            print(f"  {rank:<4} {ticker:<12} {val*100:>+10.1f}%  {bar}")

    current_top(us_prices,  "[US] S&P 500")
    current_top(sto_prices, "[SE] Stockholmsborsen")

    # Optimize STO parameters
    print("\nRunning STO parameter optimization (this takes ~1-2 min)...")
    opt_df = run_optimization(sto_prices, "Stockholmsborsen")

    # MTF comparison for STO (use best params from opt)
    best_lb   = int(opt_df.iloc[0]["Lookback_m"]) if not opt_df.empty else 9
    best_skip = int(opt_df.iloc[0]["Skip_m"])     if not opt_df.empty else 2
    best_n    = 15
    run_comparison_sto(sto_prices, lb_months=best_lb, skip_months=best_skip, top_n=best_n)

    # Plot main backtest
    plot_results(us_port, us_bench, sto_port, sto_bench)
