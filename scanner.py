import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import warnings
import os
import requests
from io import StringIO
import threading
import json
import time

warnings.filterwarnings('ignore')

# Konfiguration

LOOKBACK_CANDIDATES = [1, 2, 3, 4, 5, 6, 9, 12]
HOLD_CANDIDATES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
MIN_HISTORY_DAYS = 252 * 5  # ~5 år
TOP_N = 25
TRADING_DAYS_PER_MONTH = 21
SECONDARY_LOOKBACKS = [1, 3, 6, 12]
FAST_MODE = False
FAST_LOOKBACK = 6
FAST_HOLD = 6
SECTOR_CAP_PCT = 0.30
FILTER_OVER_PHASE = True
MOM_CAP_PCT = 150.0
SLOPE_CAP_ANN = 400.0

# Global State for Status Polling
SCAN_STATUS = {
    "is_running": False,
    "message": "Waiting to start..."
}
STATUS_FILE = "status.json"

def get_status():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return SCAN_STATUS.copy()

def set_status(msg, running=True):
    SCAN_STATUS["is_running"] = running
    SCAN_STATUS["message"] = msg
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(SCAN_STATUS, f)
    except:
        pass
    print(msg)

def fetch_nasdaq_history(symbol, from_date, to_date=None, session=None):
    if to_date is None:
        to_date = datetime.now().strftime("%Y-%m-%d")
        
    s = session or requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    
    clean_sym = str(symbol).replace("-", ".")
    if clean_sym in ["BF.B", "BRK.B"]:
        return pd.DataFrame()
        
    url = f"https://api.nasdaq.com/api/quote/{clean_sym}/chart"
    try:
        resp = s.get(url, params={"assetclass": "stocks", "fromdate": from_date, "todate": to_date}, timeout=15)
        if resp.status_code != 200:
            return pd.DataFrame()
        data = resp.json()
        chart_data = data.get("data") or {}
        points = chart_data.get("chart") or []
        if not points:
            return pd.DataFrame()
            
        def sf(val):
            try: return float(val) if val else None
            except: return None
        def si(val):
            try: return int(str(val).replace(",", "")) if val else None
            except: return None
            
        records = []
        for p in points:
            z = p.get("z", {})
            if z:
                records.append({
                    "date": z.get("dateTime"),
                    "close": sf(z.get("close")),
                    "volume": si(z.get("volume"))
                })
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
    except Exception as e:
        return pd.DataFrame()


NORDIC_BASE = "https://api.nasdaq.com/api/nordic"
NORDIC_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def fetch_nordic_instruments(market="STO"):
    """Return list of dicts with symbol, fullName, sector, orderbookId."""
    results = []
    for segment in ["LARGE_CAP", "MID_CAP", "SMALL_CAP"]:
        page = 1
        while True:
            try:
                resp = requests.get(
                    f"{NORDIC_BASE}/screener/shares",
                    params={"market": market, "segment": segment,
                            "category": "MAIN_MARKET", "tableonly": "true",
                            "lang": "en", "size": 200, "page": page},
                    headers=NORDIC_HEADERS, timeout=15
                )
                if resp.status_code != 200:
                    break
                data = resp.json().get("data", {})
                rows = data.get("instrumentListing", {}).get("rows", [])
                if not rows:
                    break
                for r in rows:
                    results.append({
                        "symbol": r.get("symbol", ""),
                        "fullName": r.get("fullName", ""),
                        "sector": r.get("sector", "Unknown"),
                        "orderbookId": r.get("id", ""),
                    })
                pagination = data.get("pagination", {})
                if page >= pagination.get("totalPages", 1):
                    break
                page += 1
            except Exception:
                break
    # deduplicate by symbol
    seen = set()
    unique = []
    for r in results:
        if r["symbol"] not in seen and r["symbol"] and r["orderbookId"]:
            seen.add(r["symbol"])
            unique.append(r)
    return unique


def fetch_nordic_history(orderbook_id, from_date, to_date=None, session=None):
    if to_date is None:
        to_date = datetime.now().strftime("%Y-%m-%d")
    s = session or requests.Session()
    s.headers.update(NORDIC_HEADERS)
    try:
        resp = s.get(
            f"{NORDIC_BASE}/instruments/{orderbook_id}/chart",
            params={"assetClass": "SHARES", "lang": "en",
                    "fromDate": from_date, "toDate": to_date},
            timeout=15
        )
        if resp.status_code != 200:
            return pd.DataFrame()
        points = resp.json().get("data", {}).get("CP", [])
        if not points:
            return pd.DataFrame()
        records = []
        for p in points:
            z = p.get("z", {})
            if z:
                try: close_val = float(z.get("close") or 0) or None
                except: close_val = None
                try: vol_val = int(str(z.get("volume") or "0").replace(",", "")) or None
                except: vol_val = None
                records.append({"date": z.get("dateTime"), "close": close_val, "volume": vol_val})
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

def optimize_params(series, lookback_candidates, hold_candidates, tdpm):
    best_sharpe = -np.inf
    best_lb = lookback_candidates[0]
    best_hold = hold_candidates[0]
    monthly = series.resample('ME').last().dropna()
    for lb in lookback_candidates:
        for hold in hold_candidates:
            if len(monthly) < lb + hold + 24:
                continue
            rets = []
            for start in range(len(monthly) - lb - hold):
                entry_ret = monthly.iloc[start + lb] / monthly.iloc[start] - 1
                if entry_ret > 0:
                    exit_idx = min(start + lb + hold, len(monthly) - 1)
                    trade_ret = monthly.iloc[exit_idx] / monthly.iloc[start + lb] - 1
                else:
                    trade_ret = 0.0
                rets.append(trade_ret)
            rets = np.array(rets)
            if len(rets) < 12 or rets.std() == 0:
                continue
            sharpe = rets.mean() / rets.std() * np.sqrt(12)
            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_lb = lb
                best_hold = hold
    return best_lb, best_hold, best_sharpe

def run_scan(output_dir=".", market="SP500"):
    global SCAN_STATUS
    
    try:
        if market == "STO":
            set_status("Fetching Stockholmsbörsen instrument list (STO)...")
            instruments = fetch_nordic_instruments("STO")
            if not instruments:
                raise ValueError("Failed to fetch instruments from Nasdaq Nordic API.")
            TICKERS      = [r["symbol"] for r in instruments]
            STOCK_NAMES  = {r["symbol"]: r["fullName"] for r in instruments}
            STOCK_SECTORS= {r["symbol"]: r["sector"] for r in instruments}
            ORDERBOOK_IDS= {r["symbol"]: r["orderbookId"] for r in instruments}
            set_status(f"Found {len(TICKERS)} Swedish stocks. Fetching price history...")
        else:
            set_status("Fetching S&P 500 components from Wikipedia...")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            tables = pd.read_html(StringIO(resp.text))
            sp500_table = tables[0]
            sp500_table.columns = sp500_table.columns.str.strip()
            ticker_col = [c for c in sp500_table.columns if 'Symbol' in c or 'Ticker' in c][0]
            name_col   = [c for c in sp500_table.columns if 'Security' in c or 'Name' in c][0]
            sector_col = [c for c in sp500_table.columns if 'Sector' in c][0]
            sp500_table['yf_ticker'] = sp500_table[ticker_col].str.replace('.', '-', regex=False)
            TICKERS      = sp500_table['yf_ticker'].tolist()
            STOCK_NAMES  = dict(zip(sp500_table['yf_ticker'], sp500_table[name_col]))
            STOCK_SECTORS= dict(zip(sp500_table['yf_ticker'], sp500_table[sector_col]))
            ORDERBOOK_IDS= {}
            set_status(f"Found {len(TICKERS)} stocks. Fetching price history...")

        lookback_days = max(LOOKBACK_CANDIDATES) * TRADING_DAYS_PER_MONTH
        opt_start    = (datetime.now() - timedelta(days=252*6)).strftime('%Y-%m-%d')
        recent_start = (datetime.now() - timedelta(days=lookback_days + 60)).strftime('%Y-%m-%d')
        download_start = recent_start if FAST_MODE else opt_start

        set_status(f"Downloading price data since {download_start} using Nasdaq API...")

        all_prices_dict = {}
        all_volumes_dict = {}

        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'})

        from concurrent.futures import ThreadPoolExecutor, as_completed

        if market == "STO":
            def fetch_ticker(ticker):
                oid = ORDERBOOK_IDS.get(ticker)
                if not oid:
                    return ticker, None, None
                df = fetch_nordic_history(oid, download_start, session=session)
                if not df.empty:
                    return ticker, df[['close']], df[['volume']]
                return ticker, None, None
        else:
            def fetch_ticker(ticker):
                df = fetch_nasdaq_history(ticker, download_start, session=session)
                if not df.empty:
                    return ticker, df[['close']], df[['volume']]
                return ticker, None, None

        MAX_WORKERS = 25
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_ticker, t): t for t in TICKERS}
            done_count = 0
            for future in as_completed(futures):
                ticker = futures[future]
                done_count += 1
                if done_count % 50 == 0:
                    set_status(f"Downloading price data: {done_count}/{len(TICKERS)} completed")
                try:
                    t, p, v = future.result()
                    if p is not None and not p.empty:
                        all_prices_dict[t] = p['close']
                        all_volumes_dict[t] = v['volume']
                except Exception:
                    pass

        prices = pd.DataFrame(all_prices_dict)
        volumes = pd.DataFrame(all_volumes_dict)

        if prices.empty or len(prices.columns) < 5:
            raise ValueError(f"Failed to download price data. Check Nasdaq API connectivity.")

        
        set_status("Fetching VIX data...")
        try:
            vix_raw = yf.download("^VIX", start=recent_start, auto_adjust=True, progress=False, session=session)['Close']
            current_vix = float(vix_raw.iloc[-1].iloc[0]) if hasattr(vix_raw.iloc[-1], 'iloc') else float(vix_raw.iloc[-1])
            vix_sma_s = vix_raw.rolling(20).mean().iloc[-1]
            vix_sma = float(vix_sma_s.iloc[0]) if hasattr(vix_sma_s, 'iloc') else float(vix_sma_s)
        except:
            current_vix = 20.0
            vix_sma = 20.0
            
        if current_vix < 15:
            regime = "LOW_VOL"
            regime_label = "🟢 LOW VOLATILITY — Favor aggressive positions"
            regime_adj = 1.2
        elif current_vix < 22:
            regime = "NORMAL"
            regime_label = "🟡 NORMAL — Standard momentum signals reliable"
            regime_adj = 1.0
        elif current_vix < 30:
            regime = "ELEVATED"
            regime_label = "🟠 ELEVATED VOL — Prefer quality momentum, reduce positions"
            regime_adj = 0.8
        else:
            regime = "CRISIS"
            regime_label = "🔴 CRISIS VOLATILITY — Minimize equity exposure"
            regime_adj = 0.5

        if FAST_MODE:
            OPTIMAL_PARAMS = {t: (FAST_LOOKBACK, FAST_HOLD, np.nan) for t in TICKERS if t in prices.columns}
        else:
            set_status("Optimizing (lookback × hold) per stock...")
            OPTIMAL_PARAMS = {}
            valid_tickers = [t for t in TICKERS if t in prices.columns]
            for i, ticker in enumerate(valid_tickers):
                if i % 50 == 0:
                    set_status(f"Optimizing: {i}/{len(valid_tickers)} done...")
                series = prices[ticker].dropna()
                if len(series) < MIN_HISTORY_DAYS:
                    OPTIMAL_PARAMS[ticker] = (6, 6, np.nan)
                    continue
                try:
                    lb, hold, sharpe = optimize_params(series, LOOKBACK_CANDIDATES, HOLD_CANDIDATES, TRADING_DAYS_PER_MONTH)
                    OPTIMAL_PARAMS[ticker] = (lb, hold, round(sharpe, 2))
                except Exception:
                    OPTIMAL_PARAMS[ticker] = (6, 6, np.nan)
            
            opt_df = pd.DataFrame([
                {'Ticker': t, 'Opt_LB': v[0], 'Opt_Hold': v[1], 'Hist_Sharpe': v[2]}
                for t, v in OPTIMAL_PARAMS.items()
            ])
            opt_df.to_csv(os.path.join(output_dir, 'sp500_optimal_params.csv'), index=False)

        set_status("Calculating momentum signals...")
        results = []
        for ticker, (opt_lb, opt_hold, hist_sharpe) in OPTIMAL_PARAMS.items():
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if len(series) < 200:
                continue
            current_price = series.iloc[-1]
            lb_days = opt_lb * TRADING_DAYS_PER_MONTH
            
            if len(series) > lb_days:
                primary_mom = (series.iloc[-1] / series.iloc[-lb_days] - 1) * 100
            else:
                continue
                
            confirmations = 0
            total_checks = 0
            mtf_details = {}
            for lb in SECONDARY_LOOKBACKS:
                d = lb * TRADING_DAYS_PER_MONTH
                if len(series) > d:
                    ret = (series.iloc[-1] / series.iloc[-d] - 1) * 100
                    mtf_details[f"{lb}m"] = ret
                    if ret > 0:
                        confirmations += 1
                    total_checks += 1
            confirmation_pct = confirmations / total_checks * 100 if total_checks > 0 else 0
            
            if len(series) >= 50:
                y = np.log(series.iloc[-50:].values)
                x = np.arange(len(y))
                slope = np.polyfit(x, y, 1)[0]
                ann_slope = slope * 252 * 100
            else:
                ann_slope = 0
                
            if len(series) >= 21:
                daily_ret = series.pct_change().dropna()
                vol_20d = daily_ret.iloc[-20:].std() * np.sqrt(252) * 100
            else:
                vol_20d = 30
                
            if len(series) >= 200:
                high_200 = series.iloc[-200:].max()
                dist_from_high = (current_price / high_200 - 1) * 100
            else:
                dist_from_high = 0
                
            if ticker in volumes.columns:
                vol_series = volumes[ticker].dropna()
                if len(vol_series) >= 50:
                    vol_20 = vol_series.iloc[-20:].mean()
                    vol_50 = vol_series.iloc[-50:].mean()
                    vol_ratio = vol_20 / vol_50 if vol_50 > 0 else 1
                else:
                    vol_ratio = 1
            else:
                vol_ratio = 1
                
            months_in_signal = 0
            for offset_months in range(0, 24):
                offset_days = offset_months * TRADING_DAYS_PER_MONTH
                end_idx = len(series) - 1 - offset_days
                start_idx = end_idx - lb_days
                if start_idx < 0 or end_idx < 0:
                    break
                hist_ret = series.iloc[end_idx] / series.iloc[start_idx] - 1
                if hist_ret > 0:
                    months_in_signal = offset_months + 1
                else:
                    break
                    
            remaining_runway = max(0, opt_hold - months_in_signal)
            pct_consumed = min(months_in_signal / opt_hold, 1.5) if opt_hold > 0 else 1
            
            if pct_consumed <= 0.25:
                trend_phase = "EARLY"; trend_emoji = "🟢"
            elif pct_consumed <= 0.75:
                trend_phase = "MID"; trend_emoji = "🟢"
            elif pct_consumed <= 1.0:
                trend_phase = "LATE"; trend_emoji = "🟡"
            else:
                trend_phase = "OVER"; trend_emoji = "🔴"
                
            mom_flagged = abs(primary_mom) > MOM_CAP_PCT
            slope_flagged = abs(ann_slope) > SLOPE_CAP_ANN
            
            mom_for_score   = np.clip(primary_mom, -MOM_CAP_PCT, MOM_CAP_PCT)
            slope_for_score = np.clip(ann_slope, -SLOPE_CAP_ANN, SLOPE_CAP_ANN)
            
            over_phase_block = FILTER_OVER_PHASE and (trend_phase == "OVER")
            
            hist_sharpe_capped = min(hist_sharpe if not np.isnan(hist_sharpe) else 1.0, 8.0)
            
            mom_score    = np.clip(mom_for_score / 0.5, -100, 100)
            conf_score   = confirmation_pct
            slope_score  = np.clip(slope_for_score / 0.3, -100, 100)
            sharpe_score = np.clip(hist_sharpe_capped / 5 * 100, 0, 100)
            high_score   = np.clip((dist_from_high + 10) * 10, 0, 100)
            vol_score    = np.clip(vol_ratio * 50, 0, 100)
            
            if pct_consumed <= 0.25:
                runway_score = 100
            elif pct_consumed <= 0.75:
                runway_score = 100 - (pct_consumed - 0.25) / 0.50 * 40
            elif pct_consumed <= 1.0:
                runway_score = 60 - (pct_consumed - 0.75) / 0.25 * 40
            else:
                runway_score = 0
                
            composite = (
                0.30 * mom_score +
                0.15 * conf_score +
                0.10 * slope_score +
                0.10 * sharpe_score +
                0.10 * high_score +
                0.05 * vol_score +
                0.20 * runway_score
            ) * regime_adj
            
            buy_signal  = primary_mom > 0 and not over_phase_block
            strong_buy  = buy_signal and confirmation_pct >= 75 and ann_slope > 0
            
            flags = []
            if mom_flagged:   flags.append(f"⚠️MOM>{MOM_CAP_PCT:.0f}%")
            if slope_flagged: flags.append("⚠️SLOPE_EXT")
            if over_phase_block: flags.append("🚫OVER_BLOCKED")
            flag_str = " ".join(flags)
            
            target_vol = 15
            raw_weight = target_vol / vol_20d if vol_20d > 0 else 0.5
            position_pct = np.clip(raw_weight * 10, 0.5, 10)
            
            results.append({
                'Ticker': ticker,
                'Name': STOCK_NAMES.get(ticker, ticker),
                'Sector': STOCK_SECTORS.get(ticker, 'Unknown'),
                'Price': round(current_price, 2),
                'Opt_LB': opt_lb,
                'Opt_Hold': opt_hold,
                'Hist_Sharpe': round(hist_sharpe_capped, 2) if not np.isnan(hist_sharpe_capped) else 0,
                'Mom_Pct': round(primary_mom, 2),
                'Confirmations': f"{confirmations}/{total_checks}",
                'Conf_Pct': round(confirmation_pct, 1),
                'Slope_Ann': round(ann_slope, 1),
                'Vol_20d': round(vol_20d, 1),
                'Dist_High': round(dist_from_high, 1),
                'Vol_Ratio': round(vol_ratio, 2),
                'Score': round(composite, 1),
                'Signal': "🟢 STRONG BUY" if strong_buy else ("🟡 BUY" if buy_signal else "⬛ HOLD/AVOID"),
                'Trend_Age': months_in_signal,
                'Runway': remaining_runway,
                'Pct_Consumed': round(pct_consumed * 100, 1),
                'Trend_Phase': trend_phase,
                'Trend_Emoji': trend_emoji,
                'Pos_Size': round(position_pct, 1),
                'Mom_1m': round(mtf_details.get('1m', np.nan), 1),
                'Mom_3m': round(mtf_details.get('3m', np.nan), 1),
                'Mom_6m': round(mtf_details.get('6m', np.nan), 1),
                'Mom_12m': round(mtf_details.get('12m', np.nan), 1),
                'Flags': flag_str,
                'Mom_Flagged': mom_flagged,
            })
            
        if not results:
            raise ValueError(f"No results generated. Checked {len(OPTIMAL_PARAMS)} tickers, {len(prices.columns)} had price data but 0 had sufficient history (>=200 days).")
            
        df = pd.DataFrame(results).sort_values('Score', ascending=False).reset_index(drop=True)
        df['Rank'] = range(1, len(df) + 1)
        
        buys = df[df['Signal'].str.contains('BUY')]
        strong_buys = df[df['Signal'].str.contains('STRONG')]
        SECTOR_MAX = max(1, int(np.floor(TOP_N * SECTOR_CAP_PCT)))
        
        selected = []
        sector_counts = {}
        for _, row in buys.iterrows():
            if len(selected) >= TOP_N:
                break
            sector = row['Sector']
            cnt = sector_counts.get(sector, 0)
            if cnt >= SECTOR_MAX:
                continue
            selected.append(row)
            sector_counts[sector] = cnt + 1
            
        top_n = pd.DataFrame(selected).reset_index(drop=True)
        df.to_csv(os.path.join(output_dir, 'sp500_scanner_results.csv'), index=False)
        
        set_status("Generating HTML dashboard...")
        
        SECTOR_COLORS = {
            'Information Technology': '#3b82f6',
            'Health Care': '#22c55e',
            'Financials': '#f59e0b',
            'Consumer Discretionary': '#ec4899',
            'Industrials': '#8b5cf6',
            'Communication Services': '#06b6d4',
            'Consumer Staples': '#84cc16',
            'Energy': '#f97316',
            'Utilities': '#14b8a6',
            'Real Estate': '#e11d48',
            'Materials': '#a78bfa',
            'Unknown': '#6b7280',
        }
        
        def make_sparkline(ticker, w=90, h=36):
            if ticker not in prices.columns:
                return ''
            s = prices[ticker].dropna().iloc[-126:]  # ~6 months
            if len(s) < 5:
                return ''
            # Downsample to max 63 points
            step = max(1, len(s) // 63)
            s = s.iloc[::step]
            vals = s.tolist()
            lo, hi = min(vals), max(vals)
            if hi == lo:
                return ''
            pad = 2
            def sx(i): return round(pad + i / (len(vals)-1) * (w - 2*pad), 1)
            def sy(v): return round(pad + (1 - (v - lo)/(hi - lo)) * (h - 2*pad), 1)
            pts = ' '.join(f'{sx(i)},{sy(v)}' for i, v in enumerate(vals))
            color = '#22c55e' if vals[-1] >= vals[0] else '#ef4444'
            return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="display:block">'
                    f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
                    f'</svg>')

        top_rows = ""
        for _, row in top_n.iterrows():
            sig_class = "strong-buy" if "STRONG" in row['Signal'] else "buy"
            sig_text  = "STRONG BUY" if "STRONG" in row['Signal'] else "BUY"
            mom_width = min(abs(row['Mom_Pct']) * 2, 100)
            mom_color = "#22c55e" if row['Mom_Pct'] > 0 else "#ef4444"
            mtf_dots = ""
            for lb_label, val in [("1m", row['Mom_1m']), ("3m", row['Mom_3m']), ("6m", row['Mom_6m']), ("12m", row['Mom_12m'])]:
                color = "#22c55e" if val > 0 else "#ef4444" if val < 0 else "#6b7280"
                mtf_dots += f'<span class="mtf-dot" style="background:{color}" title="{lb_label}: {val:+.1f}%"></span>'
            total_weight = top_n['Pos_Size'].sum()
            norm_w = row['Pos_Size'] / total_weight * 100 if total_weight > 0 else 0
            phase_colors = {"EARLY": "#22c55e", "MID": "#3b82f6", "LATE": "#f59e0b", "OVER": "#ef4444"}
            phase_color = phase_colors.get(row['Trend_Phase'], "#6b7280")
            runway_text = f"{int(row['Trend_Age'])}m → {int(row['Runway'])}m left"
            sector_color = SECTOR_COLORS.get(row['Sector'], '#6b7280')
            
            top_rows += f"""
            <tr class="etf-row {sig_class}">
                <td class="rank">#{int(row['Rank'])}</td>
                <td class="ticker"><a href="https://finance.yahoo.com/chart/{row['Ticker']}" target="_blank" style="color:inherit;text-decoration:none;">{row['Ticker']}</a><br><span class="etf-name">{row['Name'][:35]}</span>
                    <br><span class="sector-badge" style="background:{sector_color}20;color:{sector_color};border:1px solid {sector_color}40">{row['Sector']}</span></td>
                <td style="padding:4px 8px">{make_sparkline(row['Ticker'])}</td>
                <td class="price">${row['Price']:.2f}</td>
                <td class="score">{row['Score']:.1f}</td>
                <td class="signal"><span class="signal-badge {sig_class}">{sig_text}</span></td>
                <td class="momentum">
                    <div class="mom-container">
                        <span class="mom-value" style="color:{mom_color}">{row['Mom_Pct']:+.1f}%</span>
                        <div class="mom-bar-bg"><div class="mom-bar" style="width:{mom_width}%;background:{mom_color}"></div></div>
                    </div>
                </td>
                <td class="mtf">{mtf_dots}</td>
                <td class="trend-phase">
                    <span class="phase-badge" style="background:{phase_color}18;color:{phase_color};border:1px solid {phase_color}40">{row['Trend_Phase']}</span>
                    <span class="runway-detail">{runway_text}</span>
                </td>
                <td class="vol">{row['Vol_20d']:.1f}%</td>
                <td class="hold">{int(row['Opt_Hold'])}m</td>
                <td class="weight">{norm_w:.1f}%</td>
            </tr>"""
            
        all_rows = ""
        for _, row in df.iterrows():
            sig_class = "strong-buy" if "STRONG" in row['Signal'] else "buy" if "BUY" in row['Signal'] else "avoid"
            sig_text  = "STRONG" if "STRONG" in row['Signal'] else "BUY" if "BUY" in row['Signal'] else "AVOID"
            mom_color = "#22c55e" if row['Mom_Pct'] > 0 else "#ef4444"
            phase_colors_full = {"EARLY": "#22c55e", "MID": "#3b82f6", "LATE": "#f59e0b", "OVER": "#ef4444"}
            pc = phase_colors_full.get(row['Trend_Phase'], "#6b7280")
            sc = SECTOR_COLORS.get(row['Sector'], '#6b7280')
            
            all_rows += f"""
            <tr class="full-row {sig_class}" data-sector="{row['Sector'].lower()}">
                <td>{int(row['Rank'])}</td>
                <td class="ticker"><a href="https://finance.yahoo.com/chart/{row['Ticker']}" target="_blank" style="color:inherit;text-decoration:none;">{row['Ticker']}</a><br><span class="etf-name">{row['Name'][:30]}</span></td>
                <td><span style="color:{sc};font-size:11px;display:inline-block;max-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="{row['Sector']}">{row['Sector']}</span></td>
                <td>{row['Score']:.1f}</td>
                <td style="color:{mom_color}">{row['Mom_Pct']:+.1f}%</td>
                <td>{row['Confirmations']}</td>
                <td><span style="color:{pc};font-weight:600">{row['Trend_Phase']}</span><br><span class="etf-name">{int(row['Trend_Age'])}m→{int(row['Runway'])}m</span></td>
                <td>{row['Vol_20d']:.1f}%</td>
                <td>{row['Dist_High']:+.1f}%</td>
                <td><span class="signal-badge-sm {sig_class}">{sig_text}</span></td>
            </tr>"""
            
        sector_rows = ""
        buy_sectors_sorted = buys.groupby('Sector').agg(
            Count=('Ticker', 'count'),
            Avg_Score=('Score', 'mean'),
            Avg_Mom=('Mom_Pct', 'mean')
        ).sort_values('Count', ascending=False).reset_index()
        for _, row in buy_sectors_sorted.iterrows():
            sc = SECTOR_COLORS.get(row['Sector'], '#6b7280')
            sector_rows += f"""
            <tr>
                <td><span style="color:{sc};font-weight:600">{row['Sector']}</span></td>
                <td style="text-align:center">{int(row['Count'])}</td>
                <td style="text-align:right">{row['Avg_Score']:.1f}</td>
                <td style="text-align:right;color:{'#22c55e' if row['Avg_Mom']>0 else '#ef4444'}">{row['Avg_Mom']:+.1f}%</td>
            </tr>"""
            
        regime_colors = {"LOW_VOL": "#22c55e", "NORMAL": "#eab308", "ELEVATED": "#f97316", "CRISIS": "#ef4444"}
        regime_color = regime_colors.get(regime, "#6b7280")

        # Market display labels
        market_title = "Stockholmsbörsen (STO)" if market == "STO" else "S&amp;P 500"
        market_flag  = "🇸🇪" if market == "STO" else "🇺🇸"

        # Compute Swedish time (CET/CEST) cleanly
        try:
            from zoneinfo import ZoneInfo
            stockholm_now = datetime.now(ZoneInfo('Europe/Stockholm'))
        except Exception:
            stockholm_now = datetime.now(timezone.utc) + timedelta(hours=1)
        
        html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>S&amp;P 500 Scanner — {stockholm_now.strftime('%Y-%m-%d')}</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {{
    --bg-primary: #0a0e17; --bg-card: #111827; --bg-card-hover: #1a2332; --border: #1e293b; --text-primary: #e2e8f0; --text-secondary: #94a3b8; --text-muted: #64748b;
    --accent-green: #22c55e; --accent-red: #ef4444; --accent-gold: #f59e0b; --accent-blue: #3b82f6; --accent-purple: #a855f7;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg-primary); color: var(--text-primary); font-family: 'Outfit', sans-serif; min-height: 100vh; overflow-x: hidden; }}
.noise {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.03'/%3E%3C/svg%3E");
    pointer-events: none; z-index: 0; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 40px 24px; position: relative; z-index: 1; }}
.header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 40px; padding-bottom: 24px; border-bottom: 1px solid var(--border); }}
.header-left h1 {{ font-size: 28px; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #e2e8f0 0%, #94a3b8 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
.header-left .subtitle {{ font-family: 'JetBrains Mono', monospace; font-size: 13px; color: var(--text-muted); margin-top: 4px; }}
.header-right {{ text-align: right; }}
.scan-date {{ font-family: 'JetBrains Mono', monospace; font-size: 14px; color: var(--text-secondary); }}
.regime-badge {{ display: inline-block; margin-top: 8px; padding: 6px 14px; border-radius: 6px; font-size: 13px; font-weight: 600;
    background: {regime_color}18; color: {regime_color}; border: 1px solid {regime_color}40; }}
.vix-info {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-muted); margin-top: 4px; }}
.stats-bar {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-bottom: 32px; }}
.stat-card {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 20px; }}
.stat-card .label {{ font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
.stat-card .value {{ font-size: 28px; font-weight: 700; margin-top: 4px; font-family: 'JetBrains Mono', monospace; }}
.stat-card .sub {{ font-size: 12px; color: var(--text-muted); margin-top: 2px; }}
.section-title {{ display: flex; justify-content: space-between; align-items: center; font-size: 18px; font-weight: 700; margin-bottom: 16px; padding-left: 12px; border-left: 3px solid var(--accent-gold); }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); padding: 10px 12px;
    text-align: left; border-bottom: 1px solid var(--border); font-family: 'JetBrains Mono', monospace; }}
td {{ padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--border); }}
.etf-row, .full-row {{ transition: background 0.15s; }}
.etf-row:hover, .full-row:hover {{ background: var(--bg-card-hover); }}
.rank {{ font-weight: 700; color: var(--accent-gold); font-family: 'JetBrains Mono', monospace; }}
.ticker {{ font-weight: 600; font-family: 'JetBrains Mono', monospace; font-size: 13px; }}
.etf-name {{ font-family: 'Outfit', sans-serif; font-weight: 400; font-size: 11px; color: var(--text-muted); display: block; margin-top: 2px; }}
.sector-badge {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; margin-top: 3px; font-family: 'JetBrains Mono', monospace; }}
.price {{ font-family: 'JetBrains Mono', monospace; color: var(--text-secondary); }}
.score {{ font-weight: 700; font-family: 'JetBrains Mono', monospace; }}
.signal-badge {{ display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; font-family: 'JetBrains Mono', monospace; }}
.signal-badge.strong-buy {{ background: #22c55e20; color: #22c55e; border: 1px solid #22c55e40; }}
.signal-badge.buy {{ background: #3b82f620; color: #3b82f6; border: 1px solid #3b82f640; }}
.signal-badge-sm {{ display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; font-family: 'JetBrains Mono', monospace; }}
.signal-badge-sm.strong-buy {{ background: #22c55e18; color: #22c55e; }}
.signal-badge-sm.buy {{ background: #3b82f618; color: #3b82f6; }}
.signal-badge-sm.avoid {{ background: #ef444418; color: #ef4444; }}
.mom-container {{ display: flex; align-items: center; gap: 8px; }}
.mom-value {{ font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600; min-width: 55px; }}
.mom-bar-bg {{ flex: 1; height: 4px; background: #1e293b; border-radius: 2px; min-width: 40px; }}
.mom-bar {{ height: 100%; border-radius: 2px; }}
.mtf-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin: 0 2px; }}
.top10-table {{ background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border); overflow: hidden; margin-bottom: 40px; }}
.full-table {{ background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border); overflow: hidden; margin-bottom: 40px; }}
.sector-table {{ background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border); overflow: hidden; margin-bottom: 40px; }}
.full-row.avoid {{ opacity: 0.4; }}
.hold {{ font-family: 'JetBrains Mono', monospace; color: var(--accent-purple); }}
.weight {{ font-family: 'JetBrains Mono', monospace; color: var(--accent-gold); font-weight: 600; }}
.trend-phase {{ white-space: nowrap; }}
.phase-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; font-family: 'JetBrains Mono', monospace; }}
.runway-detail {{ display: block; font-size: 11px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; margin-top: 2px; }}
.sector-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 40px; }}
.method {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; padding: 20px 24px; margin-bottom: 40px; font-size: 13px; color: var(--text-secondary); line-height: 1.7; }}
.method h3 {{ color: var(--text-primary); font-size: 14px; margin-bottom: 8px; }}
.method code {{ font-family: 'JetBrains Mono', monospace; color: var(--accent-blue); font-size: 12px; }}
.footer {{ text-align: center; padding: 24px; color: var(--text-muted); font-size: 12px; font-family: 'JetBrains Mono', monospace; border-top: 1px solid var(--border); margin-top: 40px; }}
#searchInput {{ background: var(--bg-card); border: 1px solid var(--border); color: var(--text-primary);
    padding: 8px 14px; border-radius: 6px; font-size: 13px; width: 100%; margin-bottom: 16px;
    font-family: 'JetBrains Mono', monospace; outline: none; }}
#searchInput:focus {{ border-color: var(--accent-blue); }}
#sectorFilter {{ background: var(--bg-card); border: 1px solid var(--border); color: var(--text-primary);
    padding: 8px 14px; border-radius: 6px; font-size: 13px; margin-bottom: 16px; margin-left: 12px;
    font-family: 'JetBrains Mono', monospace; outline: none; cursor: pointer; }}
.btn {{ background-color: var(--accent-blue); color: white; border: none; padding: 8px 16px; font-size: 14px; border-radius: 6px; cursor: pointer; font-family: 'JetBrains Mono', monospace; font-weight: 600; }}
.btn:hover {{ background-color: #2563eb; }}
.btn:disabled {{ background-color: #1e3a8a; cursor: not-allowed; }}
#statusMsg {{ font-size: 12px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; margin-left: 10px; }}
</style>
</head>
<body>
<div class="noise"></div>
<div class="container">

    <div class="header">
    <div class="header-left">
        <h1>STOCK MOMENTUM SCANNER</h1>
        <div class="subtitle">{market_flag} {market_title} — Per-stock optimal lookback × hold | {len(df)} stocks scanned</div>
    </div>
    <div class="header-right">
        <div class="scan-date">{stockholm_now.strftime('%Y-%m-%d %H:%M')} Stockholm</div>
        <div class="regime-badge" title="{regime_label}">{regime.replace('_',' ')}</div>
        <div class="vix-info">VIX {current_vix:.1f} | 20d avg {vix_sma:.1f}</div>
        <div class="vix-info" style="font-size:11px;color:{regime_color};margin-top:2px">{regime_label}</div>
    </div>
</div>

<div class="stats-bar">
    <div class="stat-card">
        <div class="label">BUY Signals</div>
        <div class="value" style="color:var(--accent-green)">{len(buys)}</div>
        <div class="sub">{len(strong_buys)} strong buy</div>
    </div>
    <div class="stat-card">
        <div class="label">Hold/Avoid</div>
        <div class="value" style="color:var(--accent-red)">{len(df)-len(buys)}</div>
        <div class="sub">out of {len(df)} total</div>
    </div>
    <div class="stat-card">
        <div class="label">Top {TOP_N} Avg Score</div>
        <div class="value">{top_n['Score'].mean():.0f}</div>
        <div class="sub">composite score</div>
    </div>
    <div class="stat-card">
        <div class="label">Top {TOP_N} Avg Mom</div>
        <div class="value" style="color:var(--accent-green)">{top_n['Mom_Pct'].mean():+.1f}%</div>
        <div class="sub">optimal lookback</div>
    </div>
    <div class="stat-card">
        <div class="label">Stocks scanned</div>
        <div class="value">{len(df)}</div>
        <div class="sub">S&amp;P 500</div>
    </div>
</div>

<div class="section-title">
    <div>TOP {TOP_N} — STRONGEST BUY SIGNALS</div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:12px;color:var(--text-muted);font-family:monospace">Market:</span>
        <button id="mktUS" onclick="setMarket('SP500')" class="btn" style="padding:5px 12px;font-size:12px;background:#1e3a5f">🇺🇸 S&amp;P 500</button>
        <button id="mktSE" onclick="setMarket('STO')" class="btn" style="padding:5px 12px;font-size:12px;background:#1a3020">🇸🇪 Stockholm</button>
        <button id="updateBtn" class="btn" onclick="startUpdate()" style="margin-left:8px">Update Data</button>
        <span id="statusMsg"></span>
    </div>
</div>
<div class="top10-table">
<table>
<thead>
    <tr>
        <th title="Ranking by composite score">Rank</th><th title="Stock ticker symbol and company name">Ticker</th><th title="6-month price chart">Chart</th><th title="Latest closing price (USD)">Price</th><th title="Composite score (0–100+): weighted sum of momentum, confirmation, slope, Sharpe, distance from high, volume and trend runway. Adjusted by VIX regime.">Score</th><th title="BUY = positive momentum in optimal lookback. STRONG BUY = also confirmed by trend slope and multi-timeframe.">Signal</th>
        <th title="Price return over the stock-specific optimal lookback period (the lookback that historically gave best Sharpe ratio).">Momentum (opt. LB)</th><th title="Multi-Timeframe momentum dots (1m/3m/6m/12m). Green = positive, Red = negative.">MTF ●</th><th title="Phase of the momentum cycle based on how much of the optimal hold period has been consumed. EARLY = fresh signal, MID = halfway, LATE = nearing end, OVER = signal expired.">Trend Phase</th><th title="Annualised 20-day historical volatility (standard deviation of daily returns × √252).">Vol 20d</th><th title="Historically optimal holding period in months for this stock (found by Sharpe optimisation).">Hold</th><th title="Suggested portfolio weight (%), calculated as inverse-volatility weighted, capped at 10% per stock.">Weight</th>
    </tr>
</thead>
<tbody>{top_rows}</tbody>
</table>
</div>

<div class="sector-grid">
<div>
<div class="section-title">SECTOR DISTRIBUTION (BUY Signals)</div>
<div class="sector-table">
<table>
<thead><tr><th>Sector</th><th style="text-align:center">Count</th><th style="text-align:right">Avg Score</th><th style="text-align:right">Avg Mom%</th></tr></thead>
<tbody>{sector_rows}</tbody>
</table>
</div>
</div>
</div>

<div class="section-title">FULL RANKING — ALL {len(df)} STOCKS</div>
<div>
    <input type="text" id="searchInput" placeholder="Search ticker or name..." oninput="filterTable()">
    <select id="sectorFilter" onchange="filterTable()">
        <option value="">All sectors</option>
        {''.join(f'<option value="{s}">{s}</option>' for s in sorted(df["Sector"].unique()))}
    </select>
</div>
<div class="full-table">
<table id="fullTable">
<thead>
    <tr><th title="Ranking by composite score">#</th><th title="Stock ticker symbol and company name">Ticker</th><th title="GICS sector">Sector</th><th title="Composite score (0–100+): weighted sum of momentum, confirmation, slope, Sharpe, distance from high, volume and trend runway.">Score</th><th title="Price return over the stock-specific optimal lookback period.">Mom%</th><th title="Multi-timeframe confirmation: how many of the 4 lookback windows (1m/3m/6m/12m) show positive momentum.">Conf</th><th title="Trend phase (EARLY/MID/LATE/OVER) and months in signal → months remaining.">Trend</th><th title="Annualised 20-day historical volatility.">Vol%</th><th title="Distance from 200-day high in percent. Negative = below peak.">High%</th><th title="BUY = positive momentum. STRONG = also confirmed by slope and multi-timeframe.">Signal</th></tr>
</thead>
<tbody>{all_rows}</tbody>
</table>
</div>

<div class="method">
    <h3>Methodology</h3>
    Per-stock optimal lookback is determined by maximizing the Sharpe ratio across all (lookback × hold period)
    combinations on historical monthly data. A buy signal is generated when the stock-specific optimal lookback return &gt; 0.
    Composite score: <code>30% primary momentum + 20% trend runway + 15% MTF confirmation +
    10% trend slope + 10% historical Sharpe + 10% distance from 200d high + 5% volume</code>.
    VIX regime adjusts scores ×{regime_adj:.1f}. Position sizing: inverse volatility, max 10% per stock.
    MTF dots show momentum for 1m/3m/6m/12m (green=positive, red=negative).
</div>

<div class="footer">
    S&amp;P 500 Scanner v1.0 — {stockholm_now.strftime('%Y-%m-%d')}<br>
    This is not investment advice. Historical performance does not guarantee future results.
</div>

</div>

<script>
function filterTable() {{
    const search = document.getElementById('searchInput').value.toLowerCase();
    const sector = document.getElementById('sectorFilter').value.toLowerCase();
    const rows = document.querySelectorAll('#fullTable tbody tr');
    rows.forEach(row => {{
        const text = row.textContent.toLowerCase();
        const matchSearch = !search || text.includes(search);
        const matchSector = !sector || (row.dataset.sector || '').includes(sector);
        row.style.display = matchSearch && matchSector ? '' : 'none';
    }});
}}

function parseProgress(msg) {{
    if (!msg) return {{pct:1, label:'Starting…'}};
    let m = msg.match(/(\d+)\/(\d+)/);
    if (msg.includes('Downloading') && m) {{
        let p = Math.round(5 + (parseInt(m[1])/parseInt(m[2]))*40);
        return {{pct:p, label:'⬇️ Downloading… '+m[1]+'/'+m[2]}};
    }}
    if (msg.includes('Downloading')) return {{pct:5, label:'⬇️ Downloading price data…'}};
    if (msg.includes('VIX')) return {{pct:46, label:'📊 Fetching VIX…'}};
    if (msg.includes('Optimizing') && m) {{
        let p = Math.round(50 + (parseInt(m[1])/parseInt(m[2]))*30);
        return {{pct:p, label:'🔬 Optimising… '+m[1]+'/'+m[2]}};
    }}
    if (msg.includes('Optimizing')) return {{pct:50, label:'🔬 Sharpe optimisation…'}};
    if (msg.includes('Calculating')) return {{pct:82, label:'⚡ Calculating signals…'}};
    if (msg.includes('Generating')) return {{pct:94, label:'🖥️ Generating dashboard…'}};
    if (msg.includes('Done')) return {{pct:100, label:'✅ Done!'}};
    return {{pct:2, label:msg}};
}}

function ensureProgressBar() {{
    if (!document.getElementById('updateBar')) {{
        const s = document.getElementById('statusMsg');
        const wrap = document.createElement('span');
        wrap.id = 'progressWrap';
        wrap.style.cssText = 'display:inline-flex;align-items:center;gap:8px;margin-left:8px;';
        wrap.innerHTML = '<span style="background:#1e293b;border-radius:99px;width:120px;height:6px;display:inline-block;overflow:hidden;"><span id="updateBar" style="display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#2563eb,#3b82f6);width:0%;transition:width 0.5s ease;"></span></span><span id="updatePct" style="font-size:11px;color:#3b82f6;font-family:monospace;">0%</span>';
        s.parentNode.insertBefore(wrap, s);
    }}
}}

let selectedMarket = '{market}';  // pre-fill with the market of this dashboard

function setMarket(mkt) {{
    selectedMarket = mkt;
    document.getElementById('mktUS').style.opacity = mkt === 'SP500' ? '1' : '0.5';
    document.getElementById('mktSE').style.opacity = mkt === 'STO'   ? '1' : '0.5';
    document.getElementById('mktUS').style.border = mkt === 'SP500' ? '2px solid #3b82f6' : '2px solid transparent';
    document.getElementById('mktSE').style.border = mkt === 'STO'   ? '2px solid #22c55e' : '2px solid transparent';
}}
setMarket(selectedMarket);  // highlight current on load

function startUpdate() {{
    const btn = document.getElementById('updateBtn');
    const statusMsg = document.getElementById('statusMsg');
    btn.disabled = true;
    btn.innerText = "Starting...";
    ensureProgressBar();
    
    fetch('/api/update', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{market: selectedMarket}})}})
        .then(res => res.json())
        .then(data => {{
            if (data.status === 'started' || data.status === 'already_running') {{
                pollStatus();
            }} else {{
                statusMsg.innerText = "Error starting.";
                btn.disabled = false;
                btn.innerText = "Update Data";
            }}
        }})
        .catch(() => {{
            statusMsg.innerText = "Cannot reach server.";
            btn.disabled = false;
            btn.innerText = "Update Data";
        }});
}}

function pollStatus() {{
    const btn = document.getElementById('updateBtn');
    const statusMsg = document.getElementById('statusMsg');
    
    fetch('/api/status')
        .then(res => res.json())
        .then(data => {{
            if (data.is_running) {{
                btn.innerText = "Updating...";
                let {{pct, label}} = parseProgress(data.message || '');
                statusMsg.innerText = label;
                let bar = document.getElementById('updateBar');
                let pctEl = document.getElementById('updatePct');
                if (bar) bar.style.width = pct + '%';
                if (pctEl) pctEl.innerText = pct + '%';
                setTimeout(pollStatus, 1000);
            }} else {{
                btn.innerText = "Update Data";
                btn.disabled = false;
                let bar = document.getElementById('updateBar');
                if (bar) bar.style.width = '100%';
                if (data.message && data.message.startsWith("Error")) {{
                    statusMsg.innerText = data.message;
                    statusMsg.style.color = "#ef4444";
                }} else {{
                    statusMsg.innerText = "✅ Update complete. Reloading...";
                    setTimeout(() => location.reload(), 2000);
                }}
            }}
        }});
}}

document.addEventListener("DOMContentLoaded", () => {{
    fetch('/api/status')
        .then(res => res.json())
        .then(data => {{
            if (data.is_running) {{
                document.getElementById('updateBtn').disabled = true;
                pollStatus();
            }}
        }});
}});
</script>

</body>
</html>"""
        
        with open(os.path.join(output_dir, 'sp500_scanner_dashboard.html'), 'w', encoding='utf-8') as f:
            f.write(html)
            
        set_status("Done!", running=False)
        
    except Exception as e:
        import traceback
        set_status(f"Error: {str(e)}", running=False)
        traceback.print_exc()

def scan_in_background(market="SP500"):
    thread = threading.Thread(target=run_scan, args=(".", market))
    thread.daemon = True
    thread.start()
