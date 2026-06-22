"""
ng_option_chain.py - MCX Natural Gas Option Chain Fetcher
============================================================
Builds on a working script that already proved the login + getMarketData
FULL-mode approach works against Angel One SmartAPI.

Fetches the live option chain for BOTH NATURALGAS (institutional, 1250
mmBtu lot) and NATGASMINI (retail, 250 mmBtu lot) at the nearest expiry.

Computes:
  - Per-strike CE/PE OI, OI change, LTP, volume
  - Put-Call Ratio (PCR) by OI
  - Max OI strikes (support/resistance proxy)
  - OI buildup classification (Long/Short Buildup, Long/Short Covering)
    based on price direction + OI direction since last snapshot

Outputs:
  - live_snapshot.json   -- overwritten every cycle, dashboard reads this
  - history/EOD_<date>.json -- saved once at/after market close, one per day
  - history/intraday_<date>.jsonl -- appended every cycle, full day's tape

Credentials read from .env file (never hardcoded, never committed to git).

Run:
    python ng_option_chain.py            # single snapshot
    python ng_option_chain.py --loop     # continuous, market-hours aware
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, date, time as dtime
from pathlib import Path

import math
import requests
import pandas as pd
import pyotp
try:
    import truststore
    truststore.inject_into_ssl()  # use Windows/macOS/Linux OS trust store
except ImportError:
    pass
from SmartApi import SmartConnect

try:
    from scipy.optimize import brentq
    from scipy.stats import norm
    SCIPY_OK = True
except ImportError:
    SCIPY_OK = False

RISK_FREE_RATE = 0.065   # ~6.5% India risk-free rate

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).parent
ENV_FILE     = SCRIPT_DIR / '.env'
OUTPUT_DIR   = SCRIPT_DIR / 'output'
HISTORY_DIR  = OUTPUT_DIR / 'history'
LIVE_FILE    = OUTPUT_DIR / 'live_snapshot.json'

SCRIP_MASTER_URL = 'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json'

CONTRACTS = ['NATURALGAS', 'NATGASMINI']   # institutional + retail
CONTRACT_LOT_SIZES = {'NATURALGAS': 1250, 'NATGASMINI': 250}  # mmBtu per lot; API returns OI in units not lots
RADAR_STRIKES = 12     # ATM +/- N strikes per contract
BATCH_SIZE    = 25     # tokens per getMarketData call (API limit safety)
POLL_SECONDS  = 30     # loop interval when --loop is used

# MCX trading hours (IST) - energy segment
MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(23, 30)

OUTPUT_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)


# -----------------------------------------------------------------------------
# CREDENTIALS
# -----------------------------------------------------------------------------

def load_env() -> dict:
    """Load credentials from environment variables (GitHub Actions) or .env file (local)."""
    import os
    env = {}

    # First: read .env file if present
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

    # Then: OS environment variables override (used by GitHub Actions secrets)
    for key in ['ANGEL_API_KEY', 'ANGEL_CLIENT_CODE', 'ANGEL_MPIN', 'ANGEL_PIN', 'ANGEL_TOTP_SECRET']:
        if os.environ.get(key):
            env[key] = os.environ[key]

    # ANGEL_PIN is the same as ANGEL_MPIN (Actions secret uses PIN name)
    if 'ANGEL_PIN' in env and 'ANGEL_MPIN' not in env:
        env['ANGEL_MPIN'] = env['ANGEL_PIN']

    required = ['ANGEL_API_KEY', 'ANGEL_CLIENT_CODE', 'ANGEL_MPIN', 'ANGEL_TOTP_SECRET']
    missing = [k for k in required if k not in env]
    if missing:
        print(f"ERROR: Missing credentials: {missing}")
        print(f"Set them in .env file (local) or GitHub Actions secrets (CI).")
        sys.exit(1)

    return env


def login(env: dict) -> SmartConnect:
    """Authenticate with SmartAPI using TOTP. Session valid until midnight IST."""
    totp = pyotp.TOTP(env['ANGEL_TOTP_SECRET']).now()
    obj = SmartConnect(api_key=env['ANGEL_API_KEY'])
    data = obj.generateSession(env['ANGEL_CLIENT_CODE'], env['ANGEL_MPIN'], totp)

    if not data or not data.get('status') or not data.get('data'):
        print(f"Login FAILED: {data}")
        sys.exit(1)

    print(f"[OK] Login successful - client {env['ANGEL_CLIENT_CODE']}")
    return obj


# -----------------------------------------------------------------------------
# SCRIP MASTER - fetched fresh every run, never cached to disk long-term
# -----------------------------------------------------------------------------

def fetch_scrip_master() -> pd.DataFrame:
    """Download the full instrument master. ~40MB, updates daily ~8:30 AM IST."""
    print("Fetching Scrip Master (this can take 10-20s, ~40MB)...")
    resp = requests.get(SCRIP_MASTER_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    df = pd.DataFrame(data)
    print(f"  [OK] {len(df):,} total instruments loaded")
    return df


def get_option_chain_universe(master: pd.DataFrame, contract_name: str) -> pd.DataFrame:
    """Filter scrip master to one contract's options at the nearest FUTURE expiry."""
    opts = master[
        (master['name'] == contract_name) &
        (master['exch_seg'] == 'MCX') &
        (master['instrumenttype'] == 'OPTFUT')
    ].copy()

    if opts.empty:
        raise ValueError(f"No options found for {contract_name} - check scrip master / name spelling")

    opts['expiry_dt'] = pd.to_datetime(opts['expiry'], format='%d%b%Y', errors='coerce')
    today = pd.Timestamp(date.today())
    opts = opts[opts['expiry_dt'] >= today]

    if opts.empty:
        raise ValueError(f"No FUTURE expiries found for {contract_name} - scrip master may be stale")

    nearest = opts['expiry_dt'].min()
    chain = opts[opts['expiry_dt'] == nearest].copy()
    chain['strike'] = chain['strike'].astype(float) / 100.0

    # Parse CE/PE from symbol suffix (e.g. NATGASMINI24JUN26250CE)
    chain['option_type'] = chain['symbol'].str.extract(r'(CE|PE)$')

    print(f"  {contract_name}: nearest expiry {nearest.date()}, "
          f"{len(chain)} contracts ({chain['option_type'].value_counts().to_dict()})")

    return chain, nearest.date()


def get_futures_token(master: pd.DataFrame, contract_name: str) -> tuple:
    """Find the nearest-expiry futures token, used to get ATM reference price."""
    fut = master[
        (master['name'] == contract_name) &
        (master['exch_seg'] == 'MCX') &
        (master['instrumenttype'] == 'FUTCOM')
    ].copy()
    fut['expiry_dt'] = pd.to_datetime(fut['expiry'], format='%d%b%Y', errors='coerce')
    today = pd.Timestamp(date.today())
    fut = fut[fut['expiry_dt'] >= today]
    if fut.empty:
        raise ValueError(f"No future FUTCOM expiry for {contract_name}")
    nearest = fut.loc[fut['expiry_dt'].idxmin()]
    return str(nearest['token']), nearest['symbol']


# -----------------------------------------------------------------------------
# MARKET DATA FETCH
# -----------------------------------------------------------------------------

def batch_quote(obj: SmartConnect, tokens: list, exch: str = 'MCX') -> list:
    """Fetch FULL-mode quotes in safe batches. Returns flat list of dicts."""
    results = []
    for i in range(0, len(tokens), BATCH_SIZE):
        batch = tokens[i:i + BATCH_SIZE]
        try:
            resp = obj.getMarketData(mode="FULL", exchangeTokens={exch: batch})
            fetched = resp.get('data', {}).get('fetched', [])
            results.extend(fetched)
            if resp.get('data', {}).get('unfetched'):
                print(f"    Warning: {len(resp['data']['unfetched'])} tokens unfetched in batch {i//BATCH_SIZE+1}")
        except Exception as e:
            print(f"    Batch {i//BATCH_SIZE+1} error: {e}")
        time.sleep(0.3)   # gentle pacing between batches
    return results


def get_atm_strike(price: float, strike_step: float) -> float:
    return round(price / strike_step) * strike_step


def detect_strike_step(chain: pd.DataFrame) -> float:
    """Infer strike spacing from the available strikes."""
    strikes = sorted(chain['strike'].unique())
    if len(strikes) < 2:
        return 5.0
    diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    return min(diffs) if diffs else 5.0


# -----------------------------------------------------------------------------
# OI BUILDUP CLASSIFICATION
# -----------------------------------------------------------------------------

def classify_buildup(price_chg: float, oi_chg: float) -> str:
    """
    Standard options-market OI buildup classification:
      Price up, OI up   -> Long Buildup    (fresh longs entering)
      Price down, OI up -> Short Buildup   (fresh shorts entering)
      Price up, OI down -> Short Covering  (shorts exiting, price relief)
      Price down, OI dn -> Long Unwinding  (longs exiting)
    """
    if oi_chg == 0 or price_chg == 0:
        return 'Neutral'
    if price_chg > 0 and oi_chg > 0:
        return 'Long Buildup'
    if price_chg < 0 and oi_chg > 0:
        return 'Short Buildup'
    if price_chg > 0 and oi_chg < 0:
        return 'Short Covering'
    if price_chg < 0 and oi_chg < 0:
        return 'Long Unwinding'
    return 'Neutral'


# -----------------------------------------------------------------------------
# BLACK-76 IV + GREEKS  (futures options model)
# -----------------------------------------------------------------------------

def black76_price(F, K, T, sigma, opt_type):
    """Black-76 theoretical price for a futures option."""
    if T <= 0 or sigma <= 0:
        return max(0.0, (F - K) if opt_type == 'CE' else (K - F))
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    df = math.exp(-RISK_FREE_RATE * T)
    if opt_type == 'CE':
        return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
    else:
        return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def calc_iv(F, K, T, price, opt_type):
    """Implied volatility via bisection (Black-76). Returns IV% or None."""
    if not SCIPY_OK or price is None or price <= 0 or T <= 0 or F <= 0 or K <= 0:
        return None
    intrinsic = max(0.0, (F - K) if opt_type == 'CE' else (K - F))
    if price <= intrinsic + 1e-6:
        return None
    try:
        iv = brentq(lambda s: black76_price(F, K, T, s, opt_type) - price,
                    0.001, 20.0, xtol=1e-6, maxiter=100)
        return round(iv * 100, 2)   # as %
    except Exception:
        return None


def calc_greeks(F, K, T, iv_pct):
    """
    Black-76 Greeks for both CE and PE at a given strike.
    iv_pct is IV in percent (e.g. 45.2 means 45.2%).
    Returns dict with ce_ and pe_ prefixed keys.
    """
    if iv_pct is None or T <= 0:
        return {}
    s = iv_pct / 100.0
    d1 = (math.log(F / K) + 0.5 * s**2 * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    df = math.exp(-RISK_FREE_RATE * T)
    sqrt_T = math.sqrt(T)
    pdf_d1 = norm.pdf(d1)

    ce_delta = df * norm.cdf(d1)
    pe_delta = -df * norm.cdf(-d1)
    gamma    = (df * pdf_d1) / (F * s * sqrt_T)
    vega     = F * df * pdf_d1 * sqrt_T / 100   # per 1% IV move
    ce_theta = (-(F * pdf_d1 * s * df) / (2 * sqrt_T)
                - RISK_FREE_RATE * df * (F * norm.cdf(d1) - K * norm.cdf(d2))) / 365
    pe_theta = (-(F * pdf_d1 * s * df) / (2 * sqrt_T)
                + RISK_FREE_RATE * df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))) / 365

    return {
        'ce_delta': round(ce_delta, 4),
        'pe_delta': round(pe_delta, 4),
        'gamma':    round(gamma, 6),
        'vega':     round(vega, 4),
        'ce_theta': round(ce_theta, 4),
        'pe_theta': round(pe_theta, 4),
    }


# -----------------------------------------------------------------------------
# MAX PAIN
# -----------------------------------------------------------------------------

def calculate_max_pain(strikes_data):
    """
    Strike where total option buyer pain (intrinsic value paid out) is minimum.
    This is where option writers (sellers/institutions) want price to expire.
    """
    all_strikes = sorted(set(r['strike'] for r in strikes_data))
    min_pain = float('inf')
    max_pain_strike = None
    pain_by_strike = {}

    for test_s in all_strikes:
        pain = 0.0
        for row in strikes_data:
            k = row['strike']
            ce_oi = (row.get('CE') or {}).get('oi', 0) or 0
            pe_oi = (row.get('PE') or {}).get('oi', 0) or 0
            if test_s > k:
                pain += (test_s - k) * ce_oi
            if test_s < k:
                pain += (k - test_s) * pe_oi
        pain_by_strike[test_s] = pain
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = test_s

    return max_pain_strike, pain_by_strike


# -----------------------------------------------------------------------------
# BUILD ONE CONTRACT'S CHAIN
# -----------------------------------------------------------------------------

def build_chain_snapshot(obj: SmartConnect, master: pd.DataFrame,
                         contract_name: str, prev_snapshot: dict = None) -> dict:
    """Fetch and assemble one contract's full option chain analysis."""

    chain_df, expiry = get_option_chain_universe(master, contract_name)
    fut_token, fut_symbol = get_futures_token(master, contract_name)

    # Get underlying futures price for ATM calc
    fut_quote = obj.getMarketData(mode="LTP", exchangeTokens={"MCX": [fut_token]})
    fut_data = fut_quote.get('data', {}).get('fetched', [])
    if not fut_data:
        raise RuntimeError(f"Could not fetch futures LTP for {contract_name} (token {fut_token})")
    underlying_price = float(fut_data[0]['ltp'])

    strike_step = detect_strike_step(chain_df)
    atm = get_atm_strike(underlying_price, strike_step)
    lower = atm - (RADAR_STRIKES * strike_step)
    upper = atm + (RADAR_STRIKES * strike_step)

    radar = chain_df[(chain_df['strike'] >= lower) & (chain_df['strike'] <= upper)].copy()
    tokens = radar['token'].astype(str).tolist()

    print(f"  {contract_name}: underlying={underlying_price:.2f}  ATM={atm}  "
          f"strikes={radar['strike'].nunique()}  tokens={len(tokens)}")

    quotes = batch_quote(obj, tokens)
    quote_by_token = {str(q.get('symbolToken')): q for q in quotes}

    # Previous OI lookup for buildup classification
    prev_oi_by_token = {}
    if prev_snapshot:
        for row in prev_snapshot.get('strikes', []):
            for side in ['CE', 'PE']:
                d = row.get(side)
                if d and d.get('token'):
                    prev_oi_by_token[d['token']] = d

    # Assemble strike-wise rows
    strikes_out = []
    total_ce_oi, total_pe_oi = 0, 0
    total_ce_vol, total_pe_vol = 0, 0

    for strike in sorted(radar['strike'].unique()):
        row = {'strike': strike, 'CE': None, 'PE': None}
        for _, contract in radar[radar['strike'] == strike].iterrows():
            token = str(contract['token'])
            q = quote_by_token.get(token)
            if not q:
                continue
            opt_type = contract['option_type']
            lot_size = CONTRACT_LOT_SIZES.get(contract_name, 1)
            oi  = int(q.get('opnInterest', 0) or 0) // lot_size
            ltp = q.get('ltp', 0)
            vol = int(q.get('tradeVolume', 0) or 0) // lot_size
            chg_pct = q.get('percentChange', 0)

            prev = prev_oi_by_token.get(token)
            oi_chg    = (oi - prev['oi']) if prev else None
            price_chg = (ltp - prev['ltp']) if prev else None
            buildup   = classify_buildup(price_chg, oi_chg) if (prev and oi_chg is not None) else 'No prior data'

            # Time to expiry in years
            T = max((expiry - date.today()).days / 365.0, 1/365.0)

            # Implied Volatility (Black-76)
            iv = calc_iv(underlying_price, float(contract['strike']), T, ltp, opt_type)

            cell = {
                'token': token,
                'symbol': contract['symbol'],
                'ltp': ltp,
                'oi': oi,
                'oi_chg': oi_chg,
                'volume': vol,
                'pct_change': chg_pct,
                'buildup': buildup,
                'iv': iv,
            }
            row[opt_type] = cell
            if opt_type == 'CE':
                total_ce_oi += oi
                total_ce_vol += vol
            else:
                total_pe_oi += oi
                total_pe_vol += vol

        # Greeks computed once per strike (use CE IV or PE IV, prefer whichever is ATM-ish)
        ce_iv = (row.get('CE') or {}).get('iv')
        pe_iv = (row.get('PE') or {}).get('iv')
        mid_iv = ce_iv or pe_iv
        if mid_iv:
            T_strike = max((expiry - date.today()).days / 365.0, 1/365.0)
            greeks = calc_greeks(underlying_price, strike, T_strike, mid_iv)
            if row.get('CE'):
                row['CE']['delta'] = greeks.get('ce_delta')
                row['CE']['gamma'] = greeks.get('gamma')
                row['CE']['theta'] = greeks.get('ce_theta')
                row['CE']['vega']  = greeks.get('vega')
            if row.get('PE'):
                row['PE']['delta'] = greeks.get('pe_delta')
                row['PE']['gamma'] = greeks.get('gamma')
                row['PE']['theta'] = greeks.get('pe_theta')
                row['PE']['vega']  = greeks.get('vega')

        strikes_out.append(row)

    pcr     = round(total_pe_oi  / total_ce_oi,  3) if total_ce_oi  else None
    vol_pcr = round(total_pe_vol / total_ce_vol, 3) if total_ce_vol else None

    # Max OI strikes
    ce_oi_by_strike = {r['strike']: r['CE']['oi'] for r in strikes_out if r['CE']}
    pe_oi_by_strike = {r['strike']: r['PE']['oi'] for r in strikes_out if r['PE']}
    ce_sorted = sorted(ce_oi_by_strike, key=ce_oi_by_strike.get, reverse=True)
    pe_sorted = sorted(pe_oi_by_strike, key=pe_oi_by_strike.get, reverse=True)
    max_ce_strike = ce_sorted[0] if ce_sorted else None
    max_pe_strike = pe_sorted[0] if pe_sorted else None

    # Max Pain
    max_pain_strike, _ = calculate_max_pain(strikes_out)

    # ATM straddle & expected move
    atm_row = next((r for r in strikes_out if r['strike'] == atm), None)
    atm_ce_ltp = (atm_row.get('CE') or {}).get('ltp', 0) if atm_row else 0
    atm_pe_ltp = (atm_row.get('PE') or {}).get('ltp', 0) if atm_row else 0
    atm_straddle = round(atm_ce_ltp + atm_pe_ltp, 2)
    expected_move_pct = round(atm_straddle / underlying_price * 100, 2) if underlying_price else None

    # Top 3 CE and PE strikes by OI (for multi-level support/resistance)
    top_ce = [{'strike': s, 'oi': ce_oi_by_strike[s]} for s in ce_sorted[:3]]
    top_pe = [{'strike': s, 'oi': pe_oi_by_strike[s]} for s in pe_sorted[:3]]

    return {
        'contract': contract_name,
        'expiry': str(expiry),
        'underlying_price': underlying_price,
        'atm_strike': atm,
        'strike_step': strike_step,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_ce_oi': total_ce_oi,
        'total_pe_oi': total_pe_oi,
        'total_ce_vol': total_ce_vol,
        'total_pe_vol': total_pe_vol,
        'pcr': pcr,
        'vol_pcr': vol_pcr,
        'max_oi_resistance': max_ce_strike,
        'max_oi_support': max_pe_strike,
        'top_resistance': top_ce,
        'top_support': top_pe,
        'max_pain': max_pain_strike,
        'atm_straddle': atm_straddle,
        'expected_move_pct': expected_move_pct,
        'iv_available': SCIPY_OK,
        'strikes': strikes_out,
    }


# -----------------------------------------------------------------------------
# MARKET HOURS CHECK
# -----------------------------------------------------------------------------

def is_market_open() -> bool:
    now = datetime.now().time()
    weekday = datetime.now().weekday()   # 0=Mon..6=Sun
    if weekday == 6:   # Sunday - MCX closed
        return False
    return MARKET_OPEN <= now <= MARKET_CLOSE


# -----------------------------------------------------------------------------
# SNAPSHOT I/O
# -----------------------------------------------------------------------------

def load_previous_snapshot(contract_name: str) -> dict:
    if not LIVE_FILE.exists():
        return None
    try:
        data = json.loads(LIVE_FILE.read_text())
        return data.get(contract_name)
    except Exception:
        return None


def save_live_snapshot(snapshots: dict):
    LIVE_FILE.write_text(json.dumps(snapshots, indent=2))


def append_intraday_history(snapshots: dict):
    today_str = date.today().isoformat()
    fname = HISTORY_DIR / f'intraday_{today_str}.jsonl'
    with open(fname, 'a') as f:
        f.write(json.dumps(snapshots) + '\n')


def save_eod_snapshot(snapshots: dict):
    today_str = date.today().isoformat()
    fname = HISTORY_DIR / f'EOD_{today_str}.json'
    fname.write_text(json.dumps(snapshots, indent=2))
    print(f"  [OK] EOD snapshot saved: {fname}")


def _load_intraday_history(days: int = 5) -> dict:
    """
    Load last N days of intraday JSONLs.
    Returns OI time series for top strikes + today's OI change.
    All strike keys are plain integers so JS String(280) == "280".
    """
    # Collect all intraday files sorted oldest→newest, take last N days
    files = sorted(HISTORY_DIR.glob('intraday_*.jsonl'))[-days:]
    if not files:
        return {}

    all_rows = []
    for fname in files:
        day_label = fname.stem.replace('intraday_', '')          # "2026-06-19"
        try:
            month_day = datetime.strptime(day_label, '%Y-%m-%d').strftime('%b%d')  # "Jun19"
        except Exception:
            month_day = day_label[-5:]
        with open(fname) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    snap = rec.get('NATURALGAS') or rec
                    if 'strikes' not in snap or 'timestamp' not in snap:
                        continue
                    t = snap['timestamp'].split(' ')[1][:5]      # HH:MM
                    snap['_label'] = f"{month_day} {t}"
                    snap['_day']   = day_label
                    all_rows.append(snap)
                except Exception:
                    continue

    if len(all_rows) < 2:
        return {'insufficient': True}

    last  = all_rows[-1]
    today_str = date.today().isoformat()
    today_rows = [r for r in all_rows if r.get('_day') == today_str]
    first_today = today_rows[0] if today_rows else all_rows[0]

    # Top 4 CE + top 4 PE by latest snapshot OI
    ce_top = sorted(last['strikes'], key=lambda r: (r.get('CE') or {}).get('oi', 0), reverse=True)[:4]
    pe_top = sorted(last['strikes'], key=lambda r: (r.get('PE') or {}).get('oi', 0), reverse=True)[:4]
    tracked = sorted(set(int(r['strike']) for r in ce_top + pe_top))

    times = [r['_label'] for r in all_rows]
    price = [r.get('underlying_price') for r in all_rows]
    # Mark day boundaries for vertical lines in chart
    day_breaks = []
    prev_day = None
    for i, r in enumerate(all_rows):
        if r['_day'] != prev_day and i > 0:
            day_breaks.append(i)
        prev_day = r['_day']

    ce_oi: dict = {s: [] for s in tracked}
    pe_oi: dict = {s: [] for s in tracked}
    for snap in all_rows:
        by_strike = {int(r['strike']): r for r in snap.get('strikes', [])}
        for s in tracked:
            row = by_strike.get(s, {})
            ce_oi[s].append((row.get('CE') or {}).get('oi', 0) or 0)
            pe_oi[s].append((row.get('PE') or {}).get('oi', 0) or 0)

    # OI change: today first vs last snapshot
    first_by = {int(r['strike']): r for r in first_today.get('strikes', [])}
    last_by  = {int(r['strike']): r for r in last.get('strikes', [])}
    all_s    = sorted(last_by.keys())
    ce_chg_day = {s: ((last_by.get(s) or {}).get('CE') or {}).get('oi', 0) - ((first_by.get(s) or {}).get('CE') or {}).get('oi', 0) for s in all_s}
    pe_chg_day = {s: ((last_by.get(s) or {}).get('PE') or {}).get('oi', 0) - ((first_by.get(s) or {}).get('PE') or {}).get('oi', 0) for s in all_s}

    return {
        'times':       times,
        'price':       price,
        'day_breaks':  day_breaks,
        'strikes':     tracked,
        'ce_oi':       ce_oi,
        'pe_oi':       pe_oi,
        'ce_chg_day':  ce_chg_day,
        'pe_chg_day':  pe_chg_day,
        'snapshots':   len(all_rows),
        'days':        len(files),
    }


def _trade_signals(ng: dict) -> dict:
    """
    Derive institutional positional trade recommendations from OI data.
    All logic is pure calculation — no AI, deterministic rules.
    """
    pcr  = ng.get('pcr') or 1.0
    vpcr = ng.get('vol_pcr') or 1.0
    price = ng.get('underlying_price') or 0
    atm  = ng.get('atm_strike') or 0
    mp   = ng.get('max_pain')
    em   = ng.get('expected_move_pct') or 0
    strd = ng.get('atm_straddle') or 0
    max_ce = ng.get('max_oi_resistance')
    max_pe = ng.get('max_oi_support')
    top_ce = ng.get('top_resistance', [])
    top_pe = ng.get('top_support', [])
    expiry = ng.get('expiry', '')

    # --- 1. DIRECTIONAL BIAS ---
    if pcr >= 1.4:
        bias, bias_col = 'BEARISH', '#f85149'
        if vpcr >= 1.4:
            bias_note = f'Both OI PCR ({pcr:.2f}) and Volume PCR ({vpcr:.2f}) confirm bearish. Fresh put buying — not just hedging.'
        else:
            bias_note = f'OI PCR {pcr:.2f} bearish but Volume PCR {vpcr:.2f} lower — may be stale OI. Confirm with buildup signals.'
    elif pcr >= 1.1:
        bias, bias_col = 'MILDLY BEARISH', '#d29922'
        bias_note = f'PCR {pcr:.2f} — put-heavy but not extreme. Watch {max_pe} PE wall as support.'
    elif pcr >= 0.85:
        bias, bias_col = 'NEUTRAL', '#8b949e'
        bias_note = f'PCR {pcr:.2f} — balanced book. Range-bound likely between {max_pe} support and {max_ce} resistance.'
    elif pcr >= 0.6:
        bias, bias_col = 'MILDLY BULLISH', '#79c0ff'
        bias_note = f'PCR {pcr:.2f} — call sellers dominant. Trend higher likely while {max_pe} holds as support.'
    else:
        bias, bias_col = 'BULLISH', '#3fb950'
        bias_note = f'PCR {pcr:.2f} — strong call selling. Bullish, but watch for reversal if PCR spikes.'

    # --- 2. MAX PAIN GRAVITY ---
    mp_dist = round(price - mp, 1) if mp else None
    if mp and abs(mp_dist) < price * 0.005:
        mp_note = f'Price pinned at max pain ({mp}). Expiry likely to stay near current level — avoid directional bets.'
        mp_action = 'SELL STRADDLE / IRON CONDOR near max pain'
    elif mp and mp_dist > 0:
        mp_note = f'Price {price} is +{mp_dist} above max pain ({mp}). Sellers will defend — gravity pulls price down toward {mp} into expiry.'
        mp_action = f'BEARISH BIAS — short CE at {max_ce} (max resistance) or buy ATM/near PE'
    elif mp:
        mp_note = f'Price {price} is {mp_dist} below max pain ({mp}). Gravity pulls toward {mp} — mild upside expected near expiry.'
        mp_action = f'BULLISH BIAS — short PE at {max_pe} (max support) or buy ATM/near CE'
    else:
        mp_note = 'Max pain data unavailable.'
        mp_action = ''

    # --- 3. OI WALL STRATEGY ---
    ce_dist = round(max_ce - price, 1) if max_ce else None
    pe_dist = round(price - max_pe, 1) if max_pe else None

    if ce_dist and pe_dist:
        if ce_dist < pe_dist:
            wall_note = f'Resistance ({max_ce}) closer ({ce_dist} pts) than support ({max_pe}, {pe_dist} pts away). Selling CEs near {max_ce} is the higher-probability trade.'
        elif pe_dist < ce_dist:
            wall_note = f'Support ({max_pe}) closer ({pe_dist} pts) than resistance ({max_ce}, {ce_dist} pts away). Selling PEs near {max_pe} is the higher-probability trade.'
        else:
            wall_note = f'Symmetric walls — resistance {max_ce} ({ce_dist} pts), support {max_pe} ({pe_dist} pts). Range-sell both sides.'
        wall_note += f' | 2nd levels: CE={top_ce[1]["strike"] if len(top_ce)>1 else "—"}, PE={top_pe[1]["strike"] if len(top_pe)>1 else "—"}'
    else:
        wall_note = 'OI wall data unavailable.'

    # --- 4. IV / PREMIUM STRATEGY ---
    if em > 6:
        iv_strategy = f'HIGH IV (expected move ±{em:.1f}%). SELL premium: short straddle at {atm} or iron condor {max_pe}–{max_ce}. Theta works in your favour.'
        iv_col = '#f85149'
    elif em > 3.5:
        iv_strategy = f'MODERATE IV (±{em:.1f}%). Balanced — sell OTM strangles ({max_pe}P / {max_ce}C) or buy directional debit spreads depending on bias.'
        iv_col = '#d29922'
    else:
        iv_strategy = f'LOW IV (±{em:.1f}%). BUY premium: long straddle at ATM {atm} or directional debit spread. Risk defined, reward open-ended.'
        iv_col = '#3fb950'

    # --- 5. EXPECTED MOVE INTERPRETATION ---
    em_low  = round(price * (1 - em / 100), 1) if em else None
    em_high = round(price * (1 + em / 100), 1) if em else None
    em_pts  = round(price * em / 100, 1) if em else None

    # --- 6. FUTURES SIGNAL ---
    step_sz = ng.get('strike_step', 5)
    if bias in ('BULLISH', 'MILDLY BULLISH'):
        fut_action = 'BUY'
        fut_col    = '#3fb950'
        fut_entry  = f'Buy NATURALGAS futures on dip toward {max_pe} support (strong PE wall)'
        fut_target = f'{max_ce} ({round(max_ce - price, 1) if max_ce else "—"} pts upside)'
        fut_stop   = f'{round(max_pe - step_sz, 1) if max_pe else "—"} (below PE wall — invalidates bullish thesis)'
        fut_note   = f'1 lot = 1250 mmBtu. Risk = ~{round((price - (max_pe - step_sz)) * 1250, 0) if max_pe else "—"} Rs/lot'
    elif bias in ('BEARISH', 'STRONGLY BEARISH', 'MILDLY BEARISH'):
        fut_action = 'SELL'
        fut_col    = '#f85149'
        fut_entry  = f'Sell NATURALGAS futures at current levels or on bounce to {max_ce} resistance'
        fut_target = f'{max_pe} ({round(price - max_pe, 1) if max_pe else "—"} pts downside)'
        fut_stop   = f'{round(max_ce + step_sz, 1) if max_ce else "—"} (above CE wall — shorts get squeezed)'
        fut_note   = f'1 lot = 1250 mmBtu. Risk = ~{round(((max_ce + step_sz) - price) * 1250, 0) if max_ce else "—"} Rs/lot'
    else:
        fut_action = 'AVOID / RANGE-TRADE'
        fut_col    = '#d29922'
        fut_entry  = f'No clear directional edge. Buy near {max_pe}, sell near {max_ce}'
        fut_target = f'Scalp within {max_pe}–{max_ce} range'
        fut_stop   = f'Exit if closes outside range by >2 pts'
        fut_note   = 'Neutral PCR — futures trap risk high. Prefer options premium-sell over directional futures.'

    # --- 7. TRADE SETUPS ---
    setups = []

    # Setup A: OI wall sell (most reliable institutional play)
    if ce_dist and pe_dist:
        closer_wall = max_ce if ce_dist <= pe_dist else max_pe
        far_wall    = max_pe if ce_dist <= pe_dist else max_ce
        sell_type   = 'CE (call sell)' if ce_dist <= pe_dist else 'PE (put sell)'
        setups.append({
            'name': 'OI Wall Short (Premium Sell)',
            'type': 'SHORT PREMIUM',
            'col': '#f85149' if 'CE' in sell_type else '#3fb950',
            'entry': f'Sell {closer_wall} {sell_type} when price approaches within 2–3 pts',
            'target': f'Full premium decay / price rejects {closer_wall}',
            'stop': f'Close if price sustains beyond {closer_wall} with increasing OI',
            'rationale': f'{closer_wall} has highest {"CE" if "CE" in sell_type else "PE"} OI — strong writer presence. Sellers defend this wall.',
        })

    # Setup A2: Futures directional
    setups.append({
        'name': f'NATURALGAS Futures - {fut_action}',
        'type': 'FUTURES',
        'col': fut_col,
        'entry': fut_entry,
        'target': fut_target,
        'stop': fut_stop,
        'rationale': fut_note,
    })

    # Setup B: Max Pain pin trade
    if mp and abs(mp_dist) > price * 0.005:
        side = 'bearish' if mp_dist > 0 else 'bullish'
        setups.append({
            'name': f'Max Pain Pin Trade ({side.title()})',
            'type': 'DIRECTIONAL / SPREAD',
            'col': '#bc8cff',
            'entry': f'{"Sell ATM CE or buy ATM PE" if mp_dist > 0 else "Sell ATM PE or buy ATM CE"} — price expected to drift toward {mp}',
            'target': f'{mp} (max pain strike) by expiry {expiry}',
            'stop': f'{"Above" if mp_dist > 0 else "Below"} {round(price + (1 if mp_dist < 0 else -1) * strd * 0.5, 1)} (0.5× straddle distance)',
            'rationale': mp_note,
        })

    # Setup C: Range trade if neutral
    if 0.85 <= pcr <= 1.3 and max_ce and max_pe:
        setups.append({
            'name': 'Iron Condor / Strangle (Range)',
            'type': 'NEUTRAL SPREAD',
            'col': '#d29922',
            'entry': f'Sell {max_ce} CE + Sell {max_pe} PE',
            'target': f'Collect {round(strd * 0.3, 1)} pts (30% of straddle)',
            'stop': f'Close if price breaks {max_pe - 5:.0f} or {max_ce + 5:.0f}',
            'rationale': f'Neutral PCR + strong OI walls = classic range. Collect theta between walls.',
        })

    # --- 6. RISK FLAGS ---
    flags = []
    if pcr > 1.5:
        flags.append('PCR very high — potential CE short-covering rally if longs exit fast.')
    if em < 2:
        flags.append('Very low IV — gamma risk high near expiry. Avoid naked short options.')
    if mp and abs(mp_dist) > price * 0.02:
        flags.append(f'Price far from max pain ({abs(mp_dist):.1f} pts). Expect increased volatility as expiry approaches.')

    return {
        'bias': bias, 'bias_col': bias_col, 'bias_note': bias_note,
        'mp_note': mp_note, 'mp_action': mp_action, 'mp_dist': mp_dist,
        'wall_note': wall_note,
        'iv_strategy': iv_strategy, 'iv_col': iv_col,
        'setups': setups,
        'flags': flags,
        'em': em, 'em_pts': em_pts, 'em_low': em_low, 'em_high': em_high,
        'strd': strd, 'max_ce': max_ce, 'max_pe': max_pe,
        'fut_action': fut_action, 'fut_col': fut_col,
        'price': price, 'atm': atm,
    }


def generate_html_dashboard(snapshots: dict):
    """Write output/dashboard.html with snapshot data embedded — opens with file:// directly."""
    ng = snapshots.get('NATURALGAS')
    if not ng or 'error' in ng:
        return

    data_json    = json.dumps(ng)
    hist_json    = json.dumps(_load_intraday_history())
    signals_json = json.dumps(_trade_signals(ng))
    dashboard_file = OUTPUT_DIR / 'dashboard.html'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<title>MCX NATURALGAS - Institutional OI Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}}
.hdr{{background:linear-gradient(135deg,#161b22,#1c2128);padding:18px 28px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}}
.hdr h1{{font-size:19px;font-weight:700;color:#f0f6fc}}.hdr p{{font-size:11px;color:#8b949e;margin-top:3px}}
.ts{{font-size:11px;color:#8b949e}}.ts b{{color:#58a6ff}}
.ct{{padding:18px 28px;max-width:1500px;margin:0 auto}}
.note{{background:#1a1f2e;border:1px solid #58a6ff33;border-radius:8px;padding:9px 14px;font-size:11px;color:#8b949e;margin-bottom:14px}}
.note b{{color:#58a6ff}}

/* METRICS STRIP */
.mstrip{{display:flex;background:#161b22;border:1px solid #30363d;border-radius:10px;margin-bottom:14px;overflow:hidden;flex-wrap:wrap}}
.mc{{flex:1;padding:12px 14px;border-right:1px solid #30363d;text-align:center;min-width:110px}}
.mc:last-child{{border-right:none}}
.ml{{font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}}
.mv{{font-size:20px;font-weight:800;line-height:1}}
.ms{{font-size:10px;color:#8b949e;margin-top:3px}}
.gr{{color:#3fb950}}.rd{{color:#f85149}}.bl{{color:#58a6ff}}.yl{{color:#d29922}}.wh{{color:#f0f6fc}}.pu{{color:#bc8cff}}
.pbar{{height:4px;background:linear-gradient(90deg,#3fb950 0%,#d29922 50%,#f85149 100%);border-radius:2px;margin-top:5px;position:relative}}
.pndl{{position:absolute;top:-4px;width:3px;height:12px;background:#f0f6fc;border-radius:2px;transform:translateX(-50%)}}

/* TWO COL MAIN */
.g2{{display:grid;grid-template-columns:1fr 300px;gap:14px;margin-bottom:14px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}}
.card h3{{font-size:13px;font-weight:700;color:#f0f6fc;margin-bottom:3px}}
.csub{{font-size:11px;color:#8b949e;margin-bottom:12px}}
.leg{{display:flex;gap:14px;margin-bottom:10px}}
.li{{display:flex;align-items:center;gap:5px;font-size:10px;color:#8b949e}}
.ld{{width:9px;height:9px;border-radius:2px}}

/* OI WALL */
.wr{{display:flex;align-items:center;height:22px;margin-bottom:2px}}
.wr.atm{{background:#58a6ff0d;border-radius:3px}}
.wr.mce{{background:#f851490d;border-radius:3px}}
.wr.mpe{{background:#3fb9500d;border-radius:3px}}
.wr.mp{{background:#bc8cff0d;border-radius:3px}}
.wsk{{width:50px;font-size:10px;color:#8b949e;text-align:right;flex-shrink:0;padding-right:6px}}
.wr.atm .wsk{{color:#58a6ff;font-weight:700}}
.wb{{flex:1;position:relative;height:100%}}
.wc{{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#30363d}}
.wce{{position:absolute;right:50%;top:3px;bottom:3px;background:linear-gradient(90deg,#f8514966,#f85149);border-radius:2px 0 0 2px;min-width:1px}}
.wpe{{position:absolute;left:50%;top:3px;bottom:3px;background:linear-gradient(90deg,#3fb950,#3fb95066);border-radius:0 2px 2px 0;min-width:1px}}
.wax{{display:flex;font-size:9px;color:#30363d;justify-content:space-between;margin-top:3px;padding:0 50px}}

/* SIDEBAR */
.sb{{display:flex;flex-direction:column;gap:12px}}
.kr{{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #21262d}}
.kr:last-child{{border-bottom:none}}
.kl{{font-size:11px;color:#8b949e}}.kv{{font-size:13px;font-weight:700}}.ko{{font-size:10px;color:#8b949e;text-align:right}}
.pg{{height:14px;background:linear-gradient(90deg,#3fb950 0%,#d29922 40%,#f85149 100%);border-radius:7px;position:relative;margin-bottom:6px}}
.pn{{position:absolute;top:-5px;width:4px;height:24px;background:#f0f6fc;border-radius:2px;transform:translateX(-50%);box-shadow:0 0 6px #f0f6fc88}}
.pls{{display:flex;justify-content:space-between;font-size:9px;color:#8b949e;margin-bottom:8px}}
.pbig{{text-align:center;font-size:26px;font-weight:900;margin-bottom:3px}}
.pnt{{background:#21262d;border-radius:5px;padding:7px 9px;font-size:11px;color:#8b949e;margin-top:8px;line-height:1.5}}
.bl-row{{display:flex;align-items:flex-start;gap:7px;margin-bottom:7px}}
.chip{{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;white-space:nowrap;flex-shrink:0;margin-top:1px}}
.bt{{font-size:11px;color:#8b949e;line-height:1.4}}
.clb{{background:#0d2b0d;color:#3fb950}}.csb{{background:#2d1010;color:#f85149}}
.csc{{background:#0d1e38;color:#79c0ff}}.clu{{background:#2d2510;color:#d29922}}
.cn{{background:#21262d;color:#8b949e}}

/* THREE CHARTS ROW */
.g3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px}}

/* OI CHANGE CHART */
.ochg-row{{display:flex;align-items:center;height:20px;margin-bottom:2px}}
.ochg-sk{{width:46px;font-size:10px;color:#8b949e;text-align:right;padding-right:6px;flex-shrink:0}}
.ochg-bars{{flex:1;position:relative;height:100%}}
.ochg-ctr{{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#30363d}}
.ochg-ce{{position:absolute;right:50%;top:3px;bottom:3px;border-radius:2px 0 0 2px;min-width:1px}}
.ochg-pe{{position:absolute;left:50%;top:3px;bottom:3px;border-radius:0 2px 2px 0;min-width:1px}}

/* IV SMILE SVG */
.iv-wrap{{overflow:hidden}}
svg.ivchart{{width:100%;height:160px}}

/* VOLUME BARS */
.vol-row{{display:flex;align-items:center;gap:6px;margin-bottom:4px}}
.vol-sk{{width:42px;font-size:9px;color:#8b949e;text-align:right;flex-shrink:0}}
.vol-track{{flex:1;height:14px;background:#21262d;border-radius:2px;display:flex;overflow:hidden}}
.vol-ce{{background:#f8514988;height:100%}}
.vol-pe{{background:#3fb95088;height:100%}}

/* OI TREND CHART */
svg.trendsvg{{width:100%;display:block}}

/* OI HISTOGRAM */
svg.histsvg{{display:block}}
.hist-legend{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px}}
.hist-li{{display:flex;align-items:center;gap:5px;font-size:10px;color:#8b949e}}

/* TRADE SIGNALS */
.sig-bias{{text-align:center;padding:14px;border-radius:8px;margin-bottom:14px;border:1px solid}}
.sig-bias-lbl{{font-size:24px;font-weight:900;letter-spacing:.04em}}
.sig-bias-note{{font-size:12px;margin-top:6px;opacity:.85}}
.sig-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
.sig-card{{background:#21262d;border-radius:7px;padding:12px}}
.sig-card h4{{font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px}}
.sig-txt{{font-size:12px;color:#e6edf3;line-height:1.6}}
.sig-iv{{font-size:12px;padding:10px 14px;border-radius:6px;border:1px solid;line-height:1.6}}
.setup-card{{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:10px}}
.setup-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.setup-name{{font-size:13px;font-weight:700;color:#f0f6fc}}
.setup-type{{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;background:#21262d;color:#8b949e}}
.setup-row{{display:flex;gap:8px;margin-bottom:5px;font-size:11px}}
.setup-lbl{{min-width:70px;color:#8b949e;flex-shrink:0;font-weight:600}}
.setup-val{{color:#e6edf3;line-height:1.4}}
.flag-row{{display:flex;gap:8px;align-items:flex-start;margin-bottom:6px;font-size:11px;color:#d29922}}
.flag-row:before{{content:"⚠";flex-shrink:0}}

/* GREEKS TABLE */
.g-note{{font-size:10px;color:#8b949e;margin-bottom:10px;padding:6px 8px;background:#21262d;border-radius:4px}}

/* FULL CHAIN TABLE */
.tbl{{width:100%;border-collapse:collapse;font-size:11px}}
.tbl th{{background:#21262d;color:#8b949e;padding:7px 8px;text-align:center;border-bottom:1px solid #30363d;font-size:10px;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}}
.tbl th.ce{{text-align:right}}.tbl th.pe{{text-align:left}}.tbl th.sk{{color:#58a6ff}}
.tbl td{{padding:6px 8px;border-bottom:1px solid #21262d;text-align:center;white-space:nowrap}}
.tbl td.ce{{text-align:right}}.tbl td.pe{{text-align:left}}
.tbl tr:hover td{{background:#1c2128}}
.tbl tr.atm td{{background:#58a6ff0d}}
.tbl tr.atm td.sk{{color:#58a6ff;font-weight:700}}
.ibar{{height:4px;border-radius:2px;display:inline-block;vertical-align:middle;margin-right:3px}}
.ib-ce{{background:#f85149}}.ib-pe{{background:#3fb950}}
.up{{color:#3fb950}}.dn{{color:#f85149}}.neu{{color:#8b949e}}
.mp-line{{border-top:2px dashed #bc8cff44;margin:1px 0}}

@media(max-width:1000px){{.g2,.g3{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="hdr">
  <div><h1>MCX NATURALGAS &mdash; Institutional OI Dashboard</h1><p>1250 mmBtu lot &nbsp;|&nbsp; Live option chain analysis &nbsp;|&nbsp; Black-76 IV &amp; Greeks</p></div>
  <div class="ts">Snapshot: <b id="ts">—</b></div>
</div>
<div class="ct">
<div class="note"><b>Auto-refresh:</b> Page reloads every 60s. Run <code>python ng_option_chain.py --loop</code> during market hours (Mon-Sat 9:00-23:30 IST). Open this file directly in browser.</div>

<!-- METRICS STRIP ROW 1 -->
<div class="mstrip" id="m1"></div>
<!-- METRICS STRIP ROW 2 -->
<div class="mstrip" id="m2" style="margin-top:8px"></div>

<!-- MAIN: OI WALL + SIDEBAR -->
<div class="g2">
  <div class="card">
    <h3>Open Interest Wall</h3>
    <div class="csub">CE (resistance) &larr; | &rarr; PE (support) &nbsp;&bull;&nbsp; ATM &plusmn;10 strikes &nbsp;&bull;&nbsp; <span style="color:#bc8cff">&#9670; Max Pain</span></div>
    <div class="leg">
      <div class="li"><div class="ld" style="background:#f85149"></div>Call OI</div>
      <div class="li"><div class="ld" style="background:#3fb950"></div>Put OI</div>
      <div class="li"><div class="ld" style="background:#f851490d;border:1px solid #f8514944"></div>Max CE</div>
      <div class="li"><div class="ld" style="background:#3fb9500d;border:1px solid #3fb95044"></div>Max PE</div>
    </div>
    <div id="wall"></div>
  </div>
  <div class="sb">
    <div class="card"><h3>Key S/R Levels</h3><div id="kl"></div></div>
    <div class="card">
      <h3>Put-Call Ratio (OI)</h3>
      <div class="pg"><div class="pn" id="pneedle"></div></div>
      <div class="pls"><span>Bull &lt;0.7</span><span>Neutral</span><span>Bear &gt;1.3</span></div>
      <div class="pbig" id="pval">—</div>
      <div style="text-align:center;font-size:10px;color:#8b949e">PE OI / CE OI</div>
      <div class="pnt" id="pnote">—</div>
    </div>
  </div>
</div>

<!-- MULTI-STRIKE OI TIME SERIES + PRICE -->
<div class="card" style="margin-bottom:14px">
  <h3>Multi-Strike OI Trend &mdash; Today</h3>
  <div class="csub" id="oi-trend-sub">Top strikes OI over time &bull; right axis = underlying price</div>
  <div id="oi-trend-wrap"><div style="color:#8b949e;font-size:11px;padding:14px 0">Run <code>--loop</code> during market hours to build time series (needs 2+ snapshots today)</div></div>
</div>

<!-- OI CHANGE HISTOGRAM (Sensibull-style) + IV Smile + Volume -->
<div class="g3">
  <div class="card" style="grid-column:1/-1;position:relative">
    <h3>OI Change Histogram</h3>
    <div class="csub">Call OI (red) &amp; Put OI (green) per strike &bull; striped = today&apos;s increase &bull; hollow outline = decrease &bull; hover bars for values</div>
    <div style="overflow-x:auto;position:relative"><div id="oihist"></div></div>
    <div id="hist-tip" style="display:none;position:fixed;background:#1c2128;border:1px solid #30363d;border-radius:7px;padding:10px 14px;font-size:11px;pointer-events:none;z-index:100;min-width:180px;box-shadow:0 4px 20px #00000088"></div>
  </div>
</div>
<div class="g3" style="margin-bottom:14px">
  <div class="card" style="grid-column:span 2;position:relative">
    <h3>IV Smile (Black-76) &mdash; Hover for details</h3>
    <div class="csub" id="iv-sub">Implied Volatility across strikes</div>
    <div class="iv-wrap" style="position:relative">
      <svg class="ivchart" id="ivsvg" viewBox="0 0 560 180" preserveAspectRatio="none" style="width:100%;height:180px"></svg>
      <div id="iv-tip" style="display:none;position:absolute;background:#1c2128;border:1px solid #30363d;border-radius:6px;padding:8px 12px;font-size:11px;pointer-events:none;min-width:160px;z-index:10"></div>
    </div>
    <div id="iv-interp" style="margin-top:10px;padding:10px;background:#21262d;border-radius:6px;font-size:11px;color:#8b949e;line-height:1.7"></div>
  </div>
  <div class="card">
    <h3>Volume Analysis</h3>
    <div class="csub" id="vol-sub">CE vs PE volume by strike (ATM&plusmn;8)</div>
    <div id="volbars"></div>
  </div>
</div>

<!-- EXPECTED MOVE CARD -->
<div class="card" style="margin-bottom:14px">
  <h3>Expected Move &mdash; <span id="em-pct" style="color:#d29922">—</span></h3>
  <div class="csub">ATM straddle price as % of underlying &bull; market&apos;s implied &plusmn; range until expiry</div>
  <div id="em-card"></div>
</div>

<!-- GREEKS + BUILDUP GUIDE -->
<div class="g2" style="margin-bottom:14px">
  <div class="card">
    <h3>Option Greeks Summary (ATM &plusmn;4 strikes)</h3>
    <div class="g-note">Black-76 model &bull; r=6.5% &bull; Delta: CE(+)/PE(-) &bull; Theta: daily decay &bull; Vega: per 1% IV move &bull; Gamma: delta change per 1pt</div>
    <div style="overflow-x:auto"><table class="tbl" id="greekstbl">
      <thead><tr>
        <th class="ce" colspan="5" style="color:#f85149">CALLS</th>
        <th class="sk">Strike</th>
        <th class="pe" colspan="5" style="color:#3fb950">PUTS</th>
      </tr><tr>
        <th class="ce">IV%</th><th class="ce">&Delta;</th><th class="ce">&Theta;</th><th class="ce">Vega</th><th class="ce">&Gamma;</th>
        <th class="sk">Strike</th>
        <th class="pe">&Gamma;</th><th class="pe">Vega</th><th class="pe">&Theta;</th><th class="pe">&Delta;</th><th class="pe">IV%</th>
      </tr></thead>
      <tbody id="greeksbody"></tbody>
    </table></div>
  </div>
  <div class="card">
    <h3>Buildup Classification Guide</h3>
    <div style="margin-bottom:10px;font-size:11px;color:#8b949e">Signals require 2+ snapshots (prior OI data needed)</div>
    <div class="bl-row"><span class="chip clb">Long Buildup</span><span class="bt">Price &uarr; + OI &uarr; &mdash; fresh longs entering. Bullish continuation signal.</span></div>
    <div class="bl-row"><span class="chip csb">Short Buildup</span><span class="bt">Price &darr; + OI &uarr; &mdash; fresh shorts entering. Bearish continuation signal.</span></div>
    <div class="bl-row"><span class="chip csc">Short Covering</span><span class="bt">Price &uarr; + OI &darr; &mdash; shorts forced to cover. Bullish but weak/temporary.</span></div>
    <div class="bl-row"><span class="chip clu">Long Unwinding</span><span class="bt">Price &darr; + OI &darr; &mdash; longs exiting. Bearish, not fresh shorts.</span></div>
    <div class="bl-row"><span class="chip cn">Neutral</span><span class="bt">No significant directional OI change.</span></div>
    <div style="margin-top:10px;padding:8px;background:#21262d;border-radius:5px;font-size:10px;color:#8b949e">
      <b style="color:#f0f6fc">Max Pain:</b> Strike where option buyers lose the most at expiry. Sellers/writers tend to pin price near this level near expiry.<br>
      <b style="color:#f0f6fc">Expected Move:</b> ATM straddle price = market's implied +/- range till expiry.
    </div>
  </div>
</div>

<!-- TRADE SIGNALS -->
<div class="card" style="margin-bottom:14px" id="signals-card">
  <h3>Institutional OI Trade Signals</h3>
  <div class="csub">Derived from live OI data &bull; Positional bias &bull; Max pain gravity &bull; OI wall strategy &bull; IV environment</div>
  <div id="sig-bias" class="sig-bias"></div>
  <div class="sig-grid">
    <div class="sig-card"><h4>Max Pain Gravity</h4><div class="sig-txt" id="sig-mp"></div></div>
    <div class="sig-card"><h4>OI Wall Strategy</h4><div class="sig-txt" id="sig-wall"></div></div>
  </div>
  <div class="sig-iv" id="sig-iv" style="margin-bottom:12px"></div>
  <h4 style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px">Actionable Trade Setups</h4>
  <div id="sig-setups"></div>
  <div id="sig-flags" style="margin-top:10px"></div>
  <div style="background:#21262d;border-radius:6px;padding:8px 12px;font-size:10px;color:#8b949e;margin-top:10px">
    <b style="color:#f0f6fc">Disclaimer:</b> These are OI-derived signals only — not financial advice. Always use your own judgment. OI data reflects positioning, not certainty of price movement.
  </div>
</div>

<!-- FULL CHAIN TABLE -->
<div class="card" style="margin-bottom:16px">
  <h3>Full Option Chain &mdash; NATURALGAS 1250 mmBtu</h3>
  <div class="csub" id="csub">—</div>
  <div style="overflow-x:auto"><table class="tbl">
    <thead><tr>
      <th class="ce" colspan="7" style="color:#f85149;border-bottom:2px solid #f8514944">CALLS (CE)</th>
      <th class="sk">STRIKE</th>
      <th class="pe" colspan="7" style="color:#3fb950;border-bottom:2px solid #3fb95044">PUTS (PE)</th>
    </tr><tr>
      <th class="ce">Buildup</th><th class="ce">OI Chg</th><th class="ce">OI</th><th class="ce">Vol</th><th class="ce">IV%</th><th class="ce">%Chg</th><th class="ce">LTP</th>
      <th class="sk">Strike</th>
      <th class="pe">LTP</th><th class="pe">%Chg</th><th class="pe">IV%</th><th class="pe">Vol</th><th class="pe">OI</th><th class="pe">OI Chg</th><th class="pe">Buildup</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table></div>
</div>
</div>

<script>
const D = {data_json};
const HIST = {hist_json};
const SIG = {signals_json};
const ng = D;

// --- HELPERS ---
function f(n,d=2){{return n!=null?Number(n).toLocaleString('en-IN',{{minimumFractionDigits:d,maximumFractionDigits:d}}):'—'}}
function fo(n){{if(n==null||n===undefined)return'—';const a=Math.abs(n);return a>=1000?(n/1000).toFixed(1)+'K':String(n)}}
function foF(n){{return n!=null?Number(n).toLocaleString('en-IN'):'—'}}
function pnp(p){{return Math.max(0,Math.min(100,((p-0.5)/1.5)*100))}}
function pc(p){{return p<0.7?'#3fb950':p<1?'#79c0ff':p<1.3?'#d29922':'#f85149'}}
function pl(p){{
  if(p<0.7)return['Strongly Bullish','<b style="color:#3fb950">Bullish</b> &mdash; call sellers dominant. Watch for push higher.'];
  if(p<1.0)return['Mildly Bullish','<b style="color:#79c0ff">Mild bullish lean</b> &mdash; slight put dominance.'];
  if(p<1.3)return['Neutral-Cautious','<b style="color:#d29922">Neutral-cautious</b> &mdash; more puts than calls; market hedging downside.'];
  if(p<1.6)return['Bearish','<b style="color:#f85149">Bearish bias</b> &mdash; heavy put buying. Watch PE max-OI as price magnet.'];
  return['Strongly Bearish','<b style="color:#f85149">Extreme puts</b> &mdash; may also signal contrarian reversal zone.'];
}}
function chip(b){{
  const m={{'Long Buildup':['clb','LB'],'Short Buildup':['csb','SB'],'Short Covering':['csc','SC'],'Long Unwinding':['clu','LU'],'Neutral':['cn','NEU'],'No prior data':['cn','—']}};
  const[c,t]=m[b]||['cn',b||'—'];
  return`<span class="chip ${{c}}">${{t}}</span>`
}}
function ochg(v){{
  if(v===null||v===undefined)return'<span class="neu">—</span>';
  if(v>0)return`<span class="up">+${{fo(v)}}</span>`;
  if(v<0)return`<span class="dn">${{fo(v)}}</span>`;
  return'<span class="neu">0</span>'
}}
function pctCell(v){{
  if(v==null)return'—';
  const s=v>0?'up':v<0?'dn':'neu';
  return`<span class="${{s}}">${{v>0?'+':''}}${{f(v,1)}}%</span>`
}}
function ivCell(v){{return v!=null?`<span style="color:#bc8cff">${{f(v,1)}}</span>`:'—'}}
function gk(v,d=4){{return v!=null?f(v,d):'—'}}

document.getElementById('ts').textContent = ng.timestamp;
const pcr = ng.pcr||0;
const vpcr = ng.vol_pcr||0;
const[plbl,pnote]=pl(pcr);
const atm = ng.atm_strike, step = ng.strike_step||5;

// --- METRICS ROW 1: price/position ---
document.getElementById('m1').innerHTML=`
  <div class="mc"><div class="ml">Underlying</div><div class="mv wh">${{f(ng.underlying_price)}}</div><div class="ms">NATURALGAS Fut</div></div>
  <div class="mc"><div class="ml">ATM Strike</div><div class="mv bl">${{atm}}</div><div class="ms">Expiry ${{ng.expiry}}</div></div>
  <div class="mc"><div class="ml">PCR (OI)</div><div class="mv" style="color:${{pc(pcr)}}">${{f(pcr)}}</div><div class="ms">${{plbl}}</div><div class="pbar"><div class="pndl" style="left:${{pnp(pcr)}}%"></div></div></div>
  <div class="mc"><div class="ml">Vol PCR</div><div class="mv" style="color:${{pc(vpcr)}}">${{f(vpcr)}}</div><div class="ms">PE vol / CE vol</div></div>
  <div class="mc"><div class="ml">Total CE OI</div><div class="mv rd">${{fo(ng.total_ce_oi)}}</div><div class="ms">Calls (resistance)</div></div>
  <div class="mc"><div class="ml">Total PE OI</div><div class="mv gr">${{fo(ng.total_pe_oi)}}</div><div class="ms">Puts (support)</div></div>
  <div class="mc"><div class="ml">CE Wall</div><div class="mv rd">${{ng.max_oi_resistance}}</div><div class="ms">Max CE OI strike</div></div>
  <div class="mc"><div class="ml">PE Wall</div><div class="mv gr">${{ng.max_oi_support}}</div><div class="ms">Max PE OI strike</div></div>`;

// --- METRICS ROW 2: analytics ---
const atmRow = ng.strikes.find(r=>r.strike===atm)||{{}};
const atmCEIV = atmRow.CE?.iv; const atmPEIV = atmRow.PE?.iv;
const avgATMIV = (atmCEIV&&atmPEIV)?((atmCEIV+atmPEIV)/2).toFixed(1):atmCEIV||atmPEIV||null;
document.getElementById('m2').innerHTML=`
  <div class="mc"><div class="ml">Max Pain</div><div class="mv pu">${{ng.max_pain??'—'}}</div><div class="ms">Pin risk strike</div></div>
  <div class="mc"><div class="ml">ATM Straddle</div><div class="mv yl">${{f(ng.atm_straddle)}}</div><div class="ms">CE+PE at ${{atm}}</div></div>
  <div class="mc"><div class="ml">Expected Move</div><div class="mv yl">${{ng.expected_move_pct!=null?'±'+f(ng.expected_move_pct,1)+'%':'—'}}</div><div class="ms">Till expiry ${{ng.expiry}}</div></div>
  <div class="mc"><div class="ml">ATM IV (avg)</div><div class="mv pu">${{avgATMIV!=null?f(avgATMIV,1)+'%':'—'}}</div><div class="ms">Implied Vol ATM</div></div>
  <div class="mc"><div class="ml">Total CE Vol</div><div class="mv rd">${{fo(ng.total_ce_vol)}}</div><div class="ms">Call volume</div></div>
  <div class="mc"><div class="ml">Total PE Vol</div><div class="mv gr">${{fo(ng.total_pe_vol)}}</div><div class="ms">Put volume</div></div>
  <div class="mc"><div class="ml">2nd Resistance</div><div class="mv rd">${{ng.top_resistance[1]?.strike??'—'}}</div><div class="ms">${{ng.top_resistance[1]?foF(ng.top_resistance[1].oi)+' lots':''}}</div></div>
  <div class="mc"><div class="ml">2nd Support</div><div class="mv gr">${{ng.top_support[1]?.strike??'—'}}</div><div class="ms">${{ng.top_support[1]?foF(ng.top_support[1].oi)+' lots':''}}</div></div>`;

// --- PCR SIDEBAR ---
document.getElementById('pval').textContent = f(pcr);
document.getElementById('pval').style.color = pc(pcr);
document.getElementById('pneedle').style.left = pnp(pcr)+'%';
document.getElementById('pnote').innerHTML = pnote;

// --- KEY LEVELS SIDEBAR ---
const tr = ng.top_resistance, ts = ng.top_support;
document.getElementById('kl').innerHTML=`
  <div class="kr"><span class="kl">Underlying</span><div><div class="kv bl">${{f(ng.underlying_price)}}</div></div></div>
  <div class="kr"><span class="kl">ATM / Max Pain</span><div><div class="kv bl">${{atm}} / <span class="pu">${{ng.max_pain??'—'}}</span></div></div></div>
  ${{tr.map((x,i)=>`<div class="kr"><span class="kl">Resistance ${{i+1}}</span><div><div class="kv rd">${{x.strike}}</div><div class="ko">${{foF(x.oi)}} lots</div></div></div>`).join('')}}
  ${{ts.map((x,i)=>`<div class="kr"><span class="kl">Support ${{i+1}}</span><div><div class="kv gr">${{x.strike}}</div><div class="ko">${{foF(x.oi)}} lots</div></div></div>`).join('')}}
  <div class="kr"><span class="kl">Expiry</span><div><div class="kv yl">${{ng.expiry}}</div></div></div>`;

// --- OI WALL ---
const vis=[...ng.strikes].reverse().filter(r=>Math.abs(r.strike-atm)<=10*step);
const mxO=Math.max(...vis.map(r=>r.CE?.oi||0),...vis.map(r=>r.PE?.oi||0),1);
let wh='';
for(const r of vis){{
  const co=r.CE?.oi||0,po=r.PE?.oi||0;
  const cw=(co/mxO*46).toFixed(1),pw=(po/mxO*46).toFixed(1);
  const iA=r.strike===atm,iC=r.strike===ng.max_oi_resistance,iP=r.strike===ng.max_oi_support,iM=r.strike===ng.max_pain;
  const rc=iA?'atm':iC?'mce':iP?'mpe':iM?'mp':'';
  const mpMark=iM&&!iA?'<span style="color:#bc8cff;font-size:9px"> &#9670;</span>':'';
  const winfo=JSON.stringify({{
    strike:r.strike,
    ce_oi:co, ce_ltp:r.CE?.ltp??null, ce_iv:r.CE?.iv??null, ce_vol:r.CE?.volume??null, ce_bl:r.CE?.buildup??null,
    pe_oi:po, pe_ltp:r.PE?.ltp??null, pe_iv:r.PE?.iv??null, pe_vol:r.PE?.volume??null, pe_bl:r.PE?.buildup??null,
    is_atm:iA, is_ce_wall:iC, is_pe_wall:iP, is_mp:iM
  }}).replace(/"/g,'&quot;');
  wh+=`<div class="wr ${{rc}}" data-info="${{winfo}}" onmouseenter="showWallTip(this,event)" onmousemove="moveWallTip(event)" onmouseleave="hideWallTip()"><div class="wsk">${{r.strike}}${{iA?' ▶':''}}${{mpMark}}</div><div class="wb"><div class="wc"></div><div class="wce" style="width:${{cw}}%"></div><div class="wpe" style="width:${{pw}}%"></div></div></div>`;
}}
document.getElementById('wall').innerHTML=wh+`<div class="wax"><span>CE OI</span><span>Resistance | Support</span><span>PE OI</span></div>`;

// OI Wall tooltip
const wallTip=document.createElement('div');
wallTip.id='wall-tip';
wallTip.style.cssText='display:none;position:fixed;background:#1c2128;border:1px solid #30363d;border-radius:7px;padding:10px 14px;font-size:11px;pointer-events:none;z-index:200;min-width:220px;box-shadow:0 4px 20px #00000088';
document.body.appendChild(wallTip);
window.showWallTip=function(el,e){{
  const d=JSON.parse(el.getAttribute('data-info').replace(/&quot;/g,'"'));
  const fo2=v=>v!=null?Number(v).toLocaleString():'—';
  const fp=v=>v!=null?v.toFixed(2):'—';
  const fiv=v=>v!=null?(v*100).toFixed(1)+'%':'—';
  let tags='';
  if(d.is_atm) tags+='<span style="color:#58a6ff;font-size:9px;margin-left:4px">ATM</span>';
  if(d.is_ce_wall) tags+='<span style="color:#f85149;font-size:9px;margin-left:4px">CE WALL</span>';
  if(d.is_pe_wall) tags+='<span style="color:#3fb950;font-size:9px;margin-left:4px">PE WALL</span>';
  if(d.is_mp) tags+='<span style="color:#bc8cff;font-size:9px;margin-left:4px">MAX PAIN</span>';
  wallTip.innerHTML=`
    <div style="font-weight:700;font-size:12px;color:#f0f6fc;margin-bottom:8px">Strike ${{d.strike}}${{tags}}</div>
    <table style="width:100%;border-collapse:collapse;font-size:10.5px">
      <tr><td style="color:#8b949e;padding:1px 0">Metric</td><td style="color:#f85149;text-align:right;padding:1px 4px">CE (Call)</td><td style="color:#3fb950;text-align:right;padding:1px 0">PE (Put)</td></tr>
      <tr><td style="color:#8b949e">OI (lots)</td><td style="color:#f85149;text-align:right">${{fo2(d.ce_oi)}}</td><td style="color:#3fb950;text-align:right">${{fo2(d.pe_oi)}}</td></tr>
      <tr><td style="color:#8b949e">LTP</td><td style="color:#f85149;text-align:right">${{fp(d.ce_ltp)}}</td><td style="color:#3fb950;text-align:right">${{fp(d.pe_ltp)}}</td></tr>
      <tr><td style="color:#8b949e">IV</td><td style="color:#f85149;text-align:right">${{fiv(d.ce_iv)}}</td><td style="color:#3fb950;text-align:right">${{fiv(d.pe_iv)}}</td></tr>
      <tr><td style="color:#8b949e">Volume</td><td style="color:#f85149;text-align:right">${{fo2(d.ce_vol)}}</td><td style="color:#3fb950;text-align:right">${{fo2(d.pe_vol)}}</td></tr>
      <tr><td style="color:#8b949e">Buildup</td><td style="color:#f85149;text-align:right;font-size:9.5px">${{d.ce_bl||'—'}}</td><td style="color:#3fb950;text-align:right;font-size:9.5px">${{d.pe_bl||'—'}}</td></tr>
    </table>`;
  wallTip.style.display='block';
  moveWallTip(e);
}};
window.moveWallTip=function(e){{
  const gap=14;
  let lft=e.clientX+gap, top=e.clientY-40;
  if(lft+240>window.innerWidth) lft=e.clientX-240-gap;
  wallTip.style.left=lft+'px'; wallTip.style.top=top+'px';
}};
window.hideWallTip=function(){{wallTip.style.display='none';}};

// --- MULTI-STRIKE OI TREND CHART ---
(function(){{
  if(!HIST.times||HIST.times.length<2)return;
  const strikes=HIST.strikes||[];
  const N=HIST.times.length;
  const PALETTE_CE=['#f85149','#ff7b72','#ffa198','#e87070'];
  const PALETTE_PE=['#3fb950','#56d364','#7ee787','#2ea043'];

  // Build both absolute and delta (change-from-first) series
  const ceAbs={{}},peAbs={{}},ceDelta={{}},peDelta={{}};
  strikes.forEach(s=>{{
    const ck=String(s);
    const ca=HIST.ce_oi[ck]||[], pa=HIST.pe_oi[ck]||[];
    ceAbs[s]=ca; peAbs[s]=pa;
    const c0=ca[0]||0, p0=pa[0]||0;
    ceDelta[s]=ca.map(v=>v-c0);
    peDelta[s]=pa.map(v=>v-p0);
  }});

  function buildSVG(mode){{
    // mode: 'abs' = zoomed absolute OI, 'delta' = change from first snapshot
    const W=900,H=230;
    const pl=58,pr=68,pt=22,pb=36;
    const iw=W-pl-pr, ih=H-pt-pb;
    const ox=i=>N>1?pl+i/(N-1)*iw:pl+iw/2;

    let minOI,maxOI;
    if(mode==='abs'){{
      const allV=[...strikes.flatMap(s=>[...ceAbs[s],...peAbs[s]])].filter(v=>v>0);
      if(!allV.length)return '';
      minOI=Math.min(...allV); maxOI=Math.max(...allV);
      // Zoom: show 5% padding above/below actual range (not from 0)
      const rng=maxOI-minOI||1;
      minOI=Math.max(0,minOI-rng*0.08); maxOI=maxOI+rng*0.08;
    }} else {{
      const allV=strikes.flatMap(s=>[...ceDelta[s],...peDelta[s]]);
      const absMax=Math.max(...allV.map(Math.abs),100);
      minOI=-absMax*1.1; maxOI=absMax*1.1;
    }}
    const oiy=v=>pt+ih-((v-minOI)/(maxOI-minOI)*ih);

    const prices=HIST.price.filter(v=>v!=null);
    const minP=Math.min(...prices)*0.9995, maxP=Math.max(...prices)*1.0005;
    const py=v=>pt+ih-((v-minP)/(maxP-minP)*ih);

    let svg=`<svg class="trendsvg" viewBox="0 0 ${{W}} ${{H}}" preserveAspectRatio="none" style="width:100%;height:${{H}}px">`;

    // Y grid + left axis
    for(let i=0;i<=5;i++){{
      const frac=i/5;
      const yy=(pt+ih*(1-frac)).toFixed(1);
      const v=minOI+(maxOI-minOI)*frac;
      const lbl=mode==='abs'?(v/1000).toFixed(1)+'K':(v>=0?'+':'')+Math.round(v);
      svg+=`<line x1="${{pl}}" y1="${{yy}}" x2="${{W-pr}}" y2="${{yy}}" stroke="#21262d" stroke-width="1"/>`;
      svg+=`<text x="${{pl-4}}" y="${{parseFloat(yy)+3}}" fill="#8b949e" font-size="8.5" text-anchor="end">${{lbl}}</text>`;
    }}
    // Zero line for delta mode
    if(mode==='delta'){{
      const zy=oiy(0).toFixed(1);
      svg+=`<line x1="${{pl}}" y1="${{zy}}" x2="${{W-pr}}" y2="${{zy}}" stroke="#58a6ff44" stroke-width="1.5" stroke-dasharray="6,3"/>`;
    }}
    // Day-break lines
    (HIST.day_breaks||[]).forEach(bi=>{{
      const bx=ox(bi).toFixed(1);
      svg+=`<line x1="${{bx}}" y1="${{pt}}" x2="${{bx}}" y2="${{H-pb}}" stroke="#58a6ff33" stroke-width="1.5" stroke-dasharray="5,4"/>`;
      const dayLbl=(HIST.times[bi]||'').split(' ')[0];
      svg+=`<text x="${{bx}}" y="${{pt-4}}" fill="#58a6ff88" font-size="8" text-anchor="middle">${{dayLbl}}</text>`;
    }});
    // X time labels
    const step2=Math.max(1,Math.floor(N/10));
    for(let i=0;i<N;i+=step2){{
      const parts=(HIST.times[i]||'').split(' ');
      const xx=ox(i).toFixed(1);
      svg+=`<text x="${{xx}}" y="${{H-pb+13}}" fill="#8b949e" font-size="7" text-anchor="middle">${{parts[0]||''}}</text>`;
      svg+=`<text x="${{xx}}" y="${{H-pb+22}}" fill="#8b949e" font-size="7" text-anchor="middle">${{parts[1]||''}}</text>`;
    }}
    // PE lines
    strikes.forEach((s,i)=>{{
      const pts=mode==='abs'?peAbs[s]:peDelta[s];
      if(!pts||pts.length<2)return;
      const d=pts.map((v,j)=>`${{j===0?'M':'L'}}${{ox(j).toFixed(1)}},${{oiy(v).toFixed(1)}}`).join(' ');
      svg+=`<path d="${{d}}" fill="none" stroke="${{PALETTE_PE[i%4]}}" stroke-width="2" stroke-linejoin="round"/>`;
      const lv=pts[N-1], ly=oiy(lv).toFixed(1);
      svg+=`<text x="${{W-pr+4}}" y="${{parseFloat(ly)+3}}" fill="${{PALETTE_PE[i%4]}}" font-size="8" font-weight="600">${{s}}P</text>`;
    }});
    // CE lines
    strikes.forEach((s,i)=>{{
      const pts=mode==='abs'?ceAbs[s]:ceDelta[s];
      if(!pts||pts.length<2)return;
      const d=pts.map((v,j)=>`${{j===0?'M':'L'}}${{ox(j).toFixed(1)}},${{oiy(v).toFixed(1)}}`).join(' ');
      svg+=`<path d="${{d}}" fill="none" stroke="${{PALETTE_CE[i%4]}}" stroke-width="2" stroke-linejoin="round"/>`;
      const lv=pts[N-1], ly=oiy(lv).toFixed(1);
      svg+=`<text x="${{W-pr+4}}" y="${{parseFloat(ly)+3}}" fill="${{PALETTE_CE[i%4]}}" font-size="8" font-weight="600">${{s}}C</text>`;
    }});
    // Price line (right axis)
    const pricePts=HIST.price.map((v,i)=>{{
      if(v==null)return null;
      const prev=i>0?HIST.price[i-1]:null;
      return`${{prev==null?'M':'L'}}${{ox(i).toFixed(1)}},${{py(v).toFixed(1)}}`;
    }}).filter(Boolean).join(' ');
    svg+=`<path d="${{pricePts}}" fill="none" stroke="#58a6ff" stroke-width="1.5" stroke-dasharray="5,3" opacity="0.9"/>`;
    // Right axis price labels
    for(let i=0;i<=4;i++){{
      const v=(minP+(maxP-minP)*(i/4)).toFixed(2);
      const yy=(pt+ih*(1-i/4)).toFixed(1);
      svg+=`<text x="${{W-2}}" y="${{parseFloat(yy)+3}}" fill="#58a6ff77" font-size="7.5" text-anchor="end">${{v}}</text>`;
    }}
    svg+=`<text x="${{W-pr+4}}" y="${{pt+10}}" fill="#58a6ff77" font-size="7.5">Price</text>`;
    // Crosshair vertical line (hidden by default, shown on hover)
    svg+=`<line id="oi-xhair" x1="0" y1="${{pt}}" x2="0" y2="${{H-pb}}" stroke="#58a6ff55" stroke-width="1" stroke-dasharray="4,3" style="display:none;pointer-events:none"/>`;
    // Transparent overlay to capture mouse
    svg+=`<rect x="${{pl}}" y="${{pt}}" width="${{iw}}" height="${{ih}}" fill="transparent" style="cursor:crosshair"
      onmousemove="oiTrendMove(event,this,${{W}},${{pl}},${{pr}},${{pt}},${{ih}},${{iw}},'${{mode}}')"
      onmouseleave="oiTrendHide()"/>`;
    svg+=`</svg>`;
    return svg;
  }}

  // Legend
  let leg=`<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px">`;
  leg+=`<div class="hist-legend" style="margin:0">`;
  strikes.forEach((s,i)=>{{
    leg+=`<div class="hist-li"><svg width="16" height="3"><line x1="0" y1="1.5" x2="16" y2="1.5" stroke="${{PALETTE_CE[i%4]}}" stroke-width="2.5"/></svg>${{s}}CE</div>`;
    leg+=`<div class="hist-li"><svg width="16" height="3"><line x1="0" y1="1.5" x2="16" y2="1.5" stroke="${{PALETTE_PE[i%4]}}" stroke-width="2.5"/></svg>${{s}}PE</div>`;
  }});
  leg+=`<div class="hist-li"><svg width="16" height="3"><line x1="0" y1="1.5" x2="16" y2="1.5" stroke="#58a6ff" stroke-width="2" stroke-dasharray="4,3"/></svg>Price</div></div>`;
  leg+=`<div style="display:flex;gap:6px">
    <button id="btn-abs" onclick="switchOIMode('abs')" style="font-size:10px;padding:4px 10px;border-radius:4px;border:1px solid #58a6ff;background:#58a6ff22;color:#58a6ff;cursor:pointer">Absolute OI</button>
    <button id="btn-delta" onclick="switchOIMode('delta')" style="font-size:10px;padding:4px 10px;border-radius:4px;border:1px solid #30363d;background:transparent;color:#8b949e;cursor:pointer">OI Change (from open)</button>
  </div></div>`;

  const wrap=document.getElementById('oi-trend-wrap');
  const svgDiv=document.createElement('div');
  svgDiv.id='oi-svg-area';
  wrap.innerHTML=leg;
  wrap.appendChild(svgDiv);

  window._oiSVGs={{abs:buildSVG('abs'),delta:buildSVG('delta')}};
  window.switchOIMode=function(mode){{
    svgDiv.innerHTML=window._oiSVGs[mode]||'';
    document.getElementById('btn-abs').style.background=mode==='abs'?'#58a6ff22':'transparent';
    document.getElementById('btn-abs').style.borderColor=mode==='abs'?'#58a6ff':'#30363d';
    document.getElementById('btn-abs').style.color=mode==='abs'?'#58a6ff':'#8b949e';
    document.getElementById('btn-delta').style.background=mode==='delta'?'#58a6ff22':'transparent';
    document.getElementById('btn-delta').style.borderColor=mode==='delta'?'#58a6ff':'#30363d';
    document.getElementById('btn-delta').style.color=mode==='delta'?'#58a6ff':'#8b949e';
    const sub=`Top ${{strikes.length}} strikes • ${{N}} snapshots • ${{HIST.times[0]}} – ${{HIST.times[N-1]}} • ${{mode==='abs'?'Y-axis zoomed to actual OI range (not from 0)':'Change vs first snapshot of the day — rising = OI building, falling = OI unwinding'}}`;
    document.getElementById('oi-trend-sub').textContent=sub;
  }};
  window.switchOIMode('abs');

  // OI Trend tooltip + crosshair
  const oiTip=document.createElement('div');
  oiTip.id='oi-trend-tip';
  oiTip.style.cssText='display:none;position:fixed;background:#1c2128;border:1px solid #30363d;border-radius:7px;padding:10px 14px;font-size:11px;pointer-events:none;z-index:200;min-width:200px;box-shadow:0 4px 20px #00000088';
  document.body.appendChild(oiTip);

  window.oiTrendMove=function(e,rect,W,pl,pr,pt,ih,iw,mode){{
    const svgEl=rect.closest('svg');
    const bb=svgEl.getBoundingClientRect();
    const scaleX=W/bb.width;
    const mx=(e.clientX-bb.left)*scaleX;
    const ix=(mx-pl)/iw*(N-1);
    const i=Math.max(0,Math.min(N-1,Math.round(ix)));
    const xLine=svgEl.getElementById('oi-xhair')||svgEl.querySelector('#oi-xhair');
    if(xLine){{xLine.setAttribute('x1',pl+i/(N-1)*iw);xLine.setAttribute('x2',pl+i/(N-1)*iw);xLine.style.display='';}}
    const fo2=v=>v!=null?Number(v).toLocaleString():'—';
    const signStr=v=>v>0?'+'+Number(v).toLocaleString():Number(v).toLocaleString();
    const chgSpan=(chg,show)=>{{if(!show||chg==null)return '';const col=chg>=0?'#3fb950':'#f85149';return ' <span style="font-size:9px;color:'+col+'">('+signStr(chg)+')</span>';}};
    let rows='';
    strikes.forEach((s,si)=>{{
      const cv0=ceAbs[s]?ceAbs[s][0]||0:0;
      const pv0=peAbs[s]?peAbs[s][0]||0:0;
      const cvAbs=ceAbs[s]?ceAbs[s][i]??null:null;
      const pvAbs=peAbs[s]?peAbs[s][i]??null:null;
      const cvChg=cvAbs!=null?cvAbs-cv0:null;
      const pvChg=pvAbs!=null?pvAbs-pv0:null;
      const cCol=PALETTE_CE[si%4], pCol=PALETTE_PE[si%4];
      rows+='<tr>'
        +'<td style="color:#8b949e;padding:1px 0">'+s+'</td>'
        +'<td style="color:'+cCol+';text-align:right;padding:1px 4px">'+fo2(cvAbs)+chgSpan(cvChg,mode==='abs')+'</td>'
        +'<td style="color:'+pCol+';text-align:right">'+fo2(pvAbs)+chgSpan(pvChg,mode==='abs')+'</td>'
        +'</tr>';
    }});
    const pr2=HIST.price[i];
    oiTip.innerHTML=`
      <div style="font-weight:700;font-size:11px;color:#f0f6fc;margin-bottom:7px">${{HIST.times[i]||''}}${{pr2!=null?` &nbsp;<span style="color:#58a6ff">₹${{pr2.toFixed(2)}}</span>`:''}}</div>
      <table style="width:100%;border-collapse:collapse;font-size:10.5px">
        <tr><td style="color:#8b949e">Strike</td><td style="color:#f85149;text-align:right;padding:0 4px">CE OI</td><td style="color:#3fb950;text-align:right">PE OI</td></tr>
        ${{rows}}
      </table>
      <div style="color:#8b949e;font-size:9px;margin-top:6px">${{mode==='abs'?'Lots (Δ from open in brackets)':'OI change from day open'}}</div>`;
    oiTip.style.display='block';
    const gap=14;
    let lft=e.clientX+gap, top=e.clientY-60;
    if(lft+220>window.innerWidth) lft=e.clientX-220-gap;
    if(top<0) top=e.clientY+gap;
    oiTip.style.left=lft+'px'; oiTip.style.top=top+'px';
  }};
  window.oiTrendHide=function(){{
    oiTip.style.display='none';
    const svgEl=document.querySelector('#oi-svg-area svg');
    if(svgEl){{const xLine=svgEl.querySelector('#oi-xhair');if(xLine)xLine.style.display='none';}}
  }};
}})();

// --- OI CHANGE HISTOGRAM (Sensibull-style vertical bars) ---
(function(){{
  const rows=[...ng.strikes];
  const bw=22,gap=4,grpGap=2;   // bar width, gap between CE/PE pair, gap between strikes
  const totalW=rows.length*(bw*2+gap+grpGap)+60;
  const H=200;
  const pl2=44,pr2=16,pt2=14,pb2=32;
  const ih2=H-pt2-pb2;
  const maxOI2=Math.max(...rows.flatMap(r=>[(r.CE?.oi||0),(r.PE?.oi||0)]),1);
  const bh=v=>Math.max(1,(v/maxOI2)*ih2);
  const by=v=>pt2+ih2-bh(v);

  // Y axis ticks
  let svg2=`<svg class="histsvg" viewBox="0 0 ${{totalW}} ${{H}}" width="${{totalW}}" height="${{H}}" style="min-width:${{totalW}}px">`;
  for(let i=0;i<=4;i++){{
    const v=(maxOI2*(1-i/4)/1000).toFixed(0);
    const y=(pt2+ih2*(i/4)).toFixed(1);
    svg2+=`<line x1="${{pl2}}" y1="${{y}}" x2="${{totalW-pr2}}" y2="${{y}}" stroke="#21262d" stroke-width="1"/>`;
    svg2+=`<text x="${{pl2-3}}" y="${{parseFloat(y)+3}}" fill="#8b949e" font-size="8" text-anchor="end">${{v}}K</text>`;
  }}

  // ATM vertical guide
  const atmIdx=rows.findIndex(r=>r.strike===atm);
  if(atmIdx>=0){{
    const ax=(pl2+atmIdx*(bw*2+gap+grpGap)+bw).toFixed(1);
    svg2+=`<line x1="${{ax}}" y1="${{pt2}}" x2="${{ax}}" y2="${{H-pb2}}" stroke="#58a6ff44" stroke-width="1" stroke-dasharray="3,3"/>`;
  }}

  rows.forEach((r,i)=>{{
    const x=pl2+i*(bw*2+gap+grpGap);
    const cOI=r.CE?.oi||0, pOI=r.PE?.oi||0;
    const sk=String(r.strike);
    const cChg=(HIST.ce_chg_day&&HIST.ce_chg_day[sk]!=null)?HIST.ce_chg_day[sk]:(r.CE?.oi_chg||0);
    const pChg=(HIST.pe_chg_day&&HIST.pe_chg_day[sk]!=null)?HIST.pe_chg_day[sk]:(r.PE?.oi_chg||0);
    const iA=r.strike===atm, iM=r.strike===ng.max_pain;

    // CE bar (red) — solid base, striped increase on top
    const cy=by(cOI), ch=bh(cOI);
    const ceInfo=JSON.stringify({{s:r.strike,side:'CE',oi:cOI,chg:cChg,ltp:r.CE?.ltp,iv:r.CE?.iv,vol:r.CE?.volume,buildup:r.CE?.buildup}}).replace(/"/g,'&quot;');
    svg2+=`<rect x="${{x}}" y="${{cy.toFixed(1)}}" width="${{bw}}" height="${{ch.toFixed(1)}}" fill="${{cOI>0?'#f8514966':'#21262d'}}" rx="1" class="hist-bar" data-info="${{ceInfo}}" style="cursor:pointer"/>`;
    if(cChg>0){{
      const ch2=bh(cChg);
      svg2+=`<rect x="${{x}}" y="${{(cy-ch2).toFixed(1)}}" width="${{bw}}" height="${{ch2.toFixed(1)}}" fill="url(#hce)" rx="1" pointer-events="none"/>`;
    }} else if(cChg<0){{
      const ch2=bh(Math.abs(cChg));
      svg2+=`<rect x="${{x}}" y="${{cy.toFixed(1)}}" width="${{bw}}" height="${{ch2.toFixed(1)}}" fill="none" stroke="#f8514999" stroke-width="1" rx="1" pointer-events="none"/>`;
    }}

    // PE bar (green)
    const px2=x+bw+gap;
    const pyy=by(pOI), ph=bh(pOI);
    const peInfo=JSON.stringify({{s:r.strike,side:'PE',oi:pOI,chg:pChg,ltp:r.PE?.ltp,iv:r.PE?.iv,vol:r.PE?.volume,buildup:r.PE?.buildup}}).replace(/"/g,'&quot;');
    svg2+=`<rect x="${{px2}}" y="${{pyy.toFixed(1)}}" width="${{bw}}" height="${{ph.toFixed(1)}}" fill="${{pOI>0?'#3fb95066':'#21262d'}}" rx="1" class="hist-bar" data-info="${{peInfo}}" style="cursor:pointer"/>`;
    if(pChg>0){{
      const ph2=bh(pChg);
      svg2+=`<rect x="${{px2}}" y="${{(pyy-ph2).toFixed(1)}}" width="${{bw}}" height="${{ph2.toFixed(1)}}" fill="url(#hpe)" rx="1" pointer-events="none"/>`;
    }} else if(pChg<0){{
      const ph2=bh(Math.abs(pChg));
      svg2+=`<rect x="${{px2}}" y="${{pyy.toFixed(1)}}" width="${{bw}}" height="${{ph2.toFixed(1)}}" fill="none" stroke="#3fb95099" stroke-width="1" rx="1" pointer-events="none"/>`;
    }}

    // Strike label
    const lx=(x+bw).toFixed(1);
    svg2+=`<text x="${{lx}}" y="${{H-pb2+12}}" fill="${{iA?'#58a6ff':iM?'#bc8cff':'#8b949e'}}" font-size="7.5" text-anchor="middle" font-weight="${{iA||iM?'bold':'normal'}}">${{r.strike}}${{iA?' ★':''}}${{iM?' ◆':''}}</text>`;
  }});

  // Hatching patterns
  svg2=`<svg class="histsvg" viewBox="0 0 ${{totalW}} ${{H}}" width="${{totalW}}" height="${{H}}" style="min-width:${{totalW}}px">
<defs>
<pattern id="hce" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
<line x1="0" y1="0" x2="0" y2="6" stroke="#f85149" stroke-width="3" opacity="0.8"/>
</pattern>
<pattern id="hpe" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
<line x1="0" y1="0" x2="0" y2="6" stroke="#3fb950" stroke-width="3" opacity="0.8"/>
</pattern>
</defs>`+svg2.replace(/^<svg[^>]*>/,'')+`</svg>`;

  const hasOIChg=ng.strikes.some(r=>(r.CE?.oi_chg||0)!==0||(r.PE?.oi_chg||0)!==0);
  const histLeg=`<div class="hist-legend">
    <div class="hist-li"><svg width="14" height="12"><rect x="0" y="0" width="6" height="12" fill="#f8514966" rx="1"/><rect x="8" y="0" width="6" height="12" fill="#3fb95066" rx="1"/></svg>CE / PE OI</div>
    <div class="hist-li"><svg width="14" height="12"><rect x="0" y="0" width="6" height="12" fill="url(#hce_l)" rx="1"/><rect x="8" y="0" width="6" height="12" fill="url(#hpe_l)" rx="1"/>
    <defs><pattern id="hce_l" patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="4" stroke="#f85149" stroke-width="2"/></pattern>
    <pattern id="hpe_l" patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="4" stroke="#3fb950" stroke-width="2"/></pattern>
    </defs></svg>Increase</div>
    <div class="hist-li"><svg width="14" height="12"><rect x="0" y="0" width="6" height="12" fill="none" stroke="#f8514999" rx="1"/><rect x="8" y="0" width="6" height="12" fill="none" stroke="#3fb95099" rx="1"/></svg>Decrease</div>
    <div class="hist-li" style="color:#58a6ff"><span>★</span> ATM</div>
    <div class="hist-li" style="color:#bc8cff"><span>◆</span> Max Pain=${{ng.max_pain}}</div>
    ${{!hasOIChg?'<div class="hist-li" style="color:#d29922">OI change patterns show after 2+ loop snapshots</div>':''}}
  </div>`;
  document.getElementById('oihist').innerHTML=histLeg+svg2;

  // Histogram bar hover tooltips
  const htip=document.getElementById('hist-tip');
  const hasHistChg=HIST.ce_chg_day&&Object.values(HIST.ce_chg_day).some(v=>v!==0);
  document.querySelectorAll('.hist-bar').forEach(bar=>{{
    bar.addEventListener('mouseenter',e=>{{
      const d=JSON.parse(bar.dataset.info.replace(/&quot;/g,'"'));
      const isCE=d.side==='CE';
      const col=isCE?'#f85149':'#3fb950';
      const chgSign=d.chg>0?'+':d.chg<0?'':'±0';
      const chgCol=d.chg>0?'#3fb950':d.chg<0?'#f85149':'#8b949e';
      const chgLabel=d.chg>0?'ADDING (building)':d.chg<0?'SHEDDING (unwinding)':'No change today';
      const buStr=d.buildup||'No prior data';
      const buCls={{'Long Buildup':'clb','Short Buildup':'csb','Short Covering':'csc','Long Unwinding':'clu','Neutral':'cn','No prior data':'cn'}}[buStr]||'cn';
      htip.innerHTML=`
        <div style="font-weight:700;color:${{col}};font-size:13px;margin-bottom:6px">Strike ${{d.s}} — ${{d.side}}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;margin-bottom:8px">
          <div><div style="color:#8b949e;font-size:9px">TOTAL OI</div><div style="font-size:16px;font-weight:800;color:${{col}}">${{foF(d.oi)}}</div><div style="font-size:9px;color:#8b949e">lots</div></div>
          <div><div style="color:#8b949e;font-size:9px">TODAY&apos;S CHANGE</div>
            <div style="font-size:16px;font-weight:800;color:${{chgCol}}">${{hasHistChg?(chgSign+foF(Math.abs(d.chg))):'—'}}</div>
            <div style="font-size:9px;color:${{chgCol}}">${{hasHistChg?chgLabel:'run --loop for day data'}}</div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;padding-top:6px;border-top:1px solid #30363d">
          <div><span style="color:#8b949e;font-size:9px">LTP</span> <span style="color:#f0f6fc">${{d.ltp!=null?f(d.ltp):'—'}}</span></div>
          <div><span style="color:#8b949e;font-size:9px">IV</span> <span style="color:#bc8cff">${{d.iv!=null?f(d.iv,1)+'%':'—'}}</span></div>
          <div><span style="color:#8b949e;font-size:9px">Volume</span> <span style="color:#f0f6fc">${{foF(d.vol||0)}}</span></div>
          <div><span class="chip ${{buCls}}" style="font-size:9px">${{buStr}}</span></div>
        </div>`;
      htip.style.left=(e.clientX+14)+'px';
      htip.style.top=(e.clientY-10)+'px';
      htip.style.display='block';
      bar.setAttribute('opacity','0.75');
    }});
    bar.addEventListener('mousemove',e=>{{
      htip.style.left=(e.clientX+14)+'px';
      htip.style.top=(e.clientY-10)+'px';
    }});
    bar.addEventListener('mouseleave',()=>{{htip.style.display='none';bar.setAttribute('opacity','1');}});
  }});
}})();

// --- IV SMILE SVG + HOVER + INTERPRETATION ---
(function(){{
  const ivData=ng.strikes.filter(r=>r.CE?.iv||r.PE?.iv);
  if(ivData.length<2)return;
  const allIV=[...ivData.map(r=>r.CE?.iv||0),...ivData.map(r=>r.PE?.iv||0)].filter(v=>v>0);
  const minIV=Math.min(...allIV)*0.88, maxIV=Math.max(...allIV)*1.06;
  const minSK=ivData[0].strike, maxSK=ivData[ivData.length-1].strike;
  const W=560,H=180,pad={{l:32,r:32,t:14,b:28}};
  const sx=s=>pad.l+(s-minSK)/(maxSK-minSK)*(W-pad.l-pad.r);
  const sy=v=>H-pad.b-(v-minIV)/(maxIV-minIV)*(H-pad.t-pad.b);
  let cePts='',pePts='';
  for(const r of ivData){{
    if(r.CE?.iv)cePts+=`${{sx(r.strike).toFixed(1)}},${{sy(r.CE.iv).toFixed(1)}} `;
    if(r.PE?.iv)pePts+=`${{sx(r.strike).toFixed(1)}},${{sy(r.PE.iv).toFixed(1)}} `;
  }}
  const atmX=sx(atm).toFixed(1);
  let svg='';
  // grid
  [0,0.25,0.5,0.75,1].forEach(t=>{{
    const yy=(pad.t+(1-t)*(H-pad.t-pad.b)).toFixed(1);
    const v=(minIV+t*(maxIV-minIV)).toFixed(1);
    svg+=`<line x1="${{pad.l}}" y1="${{yy}}" x2="${{W-pad.r}}" y2="${{yy}}" stroke="#21262d" stroke-width="1"/>`;
    svg+=`<text x="${{pad.l-3}}" y="${{parseFloat(yy)+3}}" fill="#8b949e" font-size="7.5" text-anchor="end">${{v}}%</text>`;
  }});
  // Strike X labels
  ivData.filter((_,i)=>i%4===0).forEach(r=>{{
    svg+=`<text x="${{sx(r.strike).toFixed(1)}}" y="${{H-pad.b+12}}" fill="#8b949e" font-size="7.5" text-anchor="middle">${{r.strike}}</text>`;
  }});
  // ATM line
  svg+=`<line x1="${{atmX}}" y1="${{pad.t}}" x2="${{atmX}}" y2="${{H-pad.b}}" stroke="#58a6ff55" stroke-width="1.5" stroke-dasharray="4,3"/>`;
  svg+=`<text x="${{atmX}}" y="${{pad.t-2}}" fill="#58a6ff" font-size="7.5" text-anchor="middle">ATM ${{atm}}</text>`;
  // Lines
  if(cePts)svg+=`<polyline points="${{cePts.trim()}}" fill="none" stroke="#f85149" stroke-width="2" stroke-linejoin="round"/>`;
  if(pePts)svg+=`<polyline points="${{pePts.trim()}}" fill="none" stroke="#3fb950" stroke-width="2" stroke-linejoin="round"/>`;
  // Visible dots on BOTH CE and PE lines
  ivData.forEach(r=>{{
    const x=sx(r.strike).toFixed(1);
    const enc=JSON.stringify({{s:r.strike,ce:r.CE?.iv,pe:r.PE?.iv,ced:r.CE?.delta,ped:r.PE?.delta,cet:r.CE?.theta,pet:r.PE?.theta,v:r.CE?.vega||r.PE?.vega}}).replace(/"/g,'&quot;');
    if(r.CE?.iv) svg+=`<circle cx="${{x}}" cy="${{sy(r.CE.iv).toFixed(1)}}" r="5" fill="#f85149" stroke="#0d1117" stroke-width="1.5" class="iv-dot" data-info="${{enc}}" style="cursor:pointer"/>`;
    if(r.PE?.iv) svg+=`<circle cx="${{x}}" cy="${{sy(r.PE.iv).toFixed(1)}}" r="5" fill="#3fb950" stroke="#0d1117" stroke-width="1.5" class="iv-dot" data-info="${{enc}}" style="cursor:pointer"/>`;
  }});
  // Transparent full-width hover overlay for line tracking
  svg+=`<rect x="${{pad.l}}" y="${{pad.t}}" width="${{W-pad.l-pad.r}}" height="${{H-pad.t-pad.b}}" fill="transparent" id="iv-overlay"/>`;
  // Crosshair (initially hidden)
  svg+=`<line id="iv-xhair" x1="0" y1="${{pad.t}}" x2="0" y2="${{H-pad.b}}" stroke="#ffffff22" stroke-width="1" stroke-dasharray="3,3" display="none"/>`;
  // Legends
  svg+=`<circle cx="${{W-pad.r-28}}" cy="${{pad.t+6}}" r="4" fill="#f85149"/>`;
  svg+=`<text x="${{W-pad.r-20}}" y="${{pad.t+10}}" fill="#f85149" font-size="8">CE IV</text>`;
  svg+=`<circle cx="${{W-pad.r-28}}" cy="${{pad.t+20}}" r="4" fill="#3fb950"/>`;
  svg+=`<text x="${{W-pad.r-20}}" y="${{pad.t+24}}" fill="#3fb950" font-size="8">PE IV</text>`;

  const el=document.getElementById('ivsvg');
  el.innerHTML=svg;
  document.getElementById('iv-sub').textContent=`IV% vs strike • ATM CE: ${{atmRow.CE?.iv?.toFixed(1)||'—'}}% • ATM PE: ${{atmRow.PE?.iv?.toFixed(1)||'—'}}% • hover any dot for full Greeks`;

  // Hover tooltip on dots
  const tip=document.getElementById('iv-tip');
  function showIVTip(d, e){{
    const info=JSON.parse(d.dataset.info.replace(/&quot;/g,'"'));
    const s=info.s, ce=info.ce, pe=info.pe, ced=info.ced, ped=info.ped, cet=info.cet, pet=info.pet, v=info.v;
    // Skew at this strike
    const skewAtStrike=(ce&&pe)?(pe-ce).toFixed(1):null;
    const skewTxt=skewAtStrike!=null?(parseFloat(skewAtStrike)>2?`<span style="color:#f85149">+${{skewAtStrike}}% put skew (fear)</span>`:parseFloat(skewAtStrike)<-2?`<span style="color:#3fb950">${{skewAtStrike}}% call skew (bullish)</span>`:`<span style="color:#8b949e">Flat (${{skewAtStrike}}%)</span>`):'—';
    tip.innerHTML=`
      <div style="font-weight:700;color:#f0f6fc;margin-bottom:6px;font-size:12px">Strike ${{s}}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;margin-bottom:6px">
        <div><span style="color:#8b949e;font-size:10px">CE IV</span><br><span style="color:#f85149;font-size:14px;font-weight:700">${{ce?ce.toFixed(1)+'%':'—'}}</span></div>
        <div><span style="color:#8b949e;font-size:10px">PE IV</span><br><span style="color:#3fb950;font-size:14px;font-weight:700">${{pe?pe.toFixed(1)+'%':'—'}}</span></div>
        <div><span style="color:#8b949e;font-size:10px">CE Delta</span><br><span style="color:#f0f6fc;font-size:12px">${{ced?parseFloat(ced).toFixed(3):'—'}}</span></div>
        <div><span style="color:#8b949e;font-size:10px">PE Delta</span><br><span style="color:#f0f6fc;font-size:12px">${{ped?parseFloat(ped).toFixed(3):'—'}}</span></div>
        <div><span style="color:#8b949e;font-size:10px">CE Theta/day</span><br><span style="color:#d29922;font-size:12px">${{cet?parseFloat(cet).toFixed(2):'—'}} Rs</span></div>
        <div><span style="color:#8b949e;font-size:10px">PE Theta/day</span><br><span style="color:#d29922;font-size:12px">${{pet?parseFloat(pet).toFixed(2):'—'}} Rs</span></div>
      </div>
      <div style="font-size:10px;color:#8b949e">Vega (per 1% IV): <b style="color:#bc8cff">${{v?parseFloat(v).toFixed(2):'—'}} Rs</b></div>
      <div style="font-size:10px;margin-top:4px">Skew: ${{skewTxt}}</div>`;
    const wrap=el.parentElement.getBoundingClientRect();
    const ex=e.clientX-wrap.left+12, ey=e.clientY-wrap.top-10;
    tip.style.left=(ex+200>wrap.width?ex-215:ex)+'px';
    tip.style.top=Math.max(0,ey)+'px';
    tip.style.display='block';
  }}
  el.querySelectorAll('.iv-dot').forEach(dot=>{{
    dot.addEventListener('mouseenter', e=>showIVTip(dot,e));
    dot.addEventListener('mouseleave', ()=>{{tip.style.display='none';}});
  }});
  // Crosshair on overlay
  const overlay=el.querySelector('#iv-overlay');
  const xhair=el.querySelector('#iv-xhair');
  if(overlay&&xhair){{
    overlay.addEventListener('mousemove',e=>{{
      const svgRect=el.getBoundingClientRect();
      const scaleX=W/svgRect.width;
      const mx=(e.clientX-svgRect.left)*scaleX;
      xhair.setAttribute('x1',mx); xhair.setAttribute('x2',mx);
      xhair.setAttribute('display','inline');
      // Find nearest strike
      let nearest=null, minDist=Infinity;
      ivData.forEach(r=>{{const dx=Math.abs(sx(r.strike)-mx); if(dx<minDist){{minDist=dx;nearest=r;}}}});
      if(nearest&&minDist<20){{
        const fakeEl={{dataset:{{info:JSON.stringify({{s:nearest.strike,ce:nearest.CE?.iv,pe:nearest.PE?.iv,ced:nearest.CE?.delta,ped:nearest.PE?.delta,cet:nearest.CE?.theta,pet:nearest.PE?.theta,v:nearest.CE?.vega||nearest.PE?.vega}})}}}};
        showIVTip(fakeEl,e);
      }} else tip.style.display='none';
    }});
    overlay.addEventListener('mouseleave',()=>{{xhair.setAttribute('display','none');tip.style.display='none';}});
  }}

  // IV interpretation
  const atmCEIV=atmRow.CE?.iv, atmPEIV=atmRow.PE?.iv;
  const avgIV=atmCEIV&&atmPEIV?(atmCEIV+atmPEIV)/2:atmCEIV||atmPEIV||0;
  // Skew: PE IV vs CE IV at ATM — positive = put skew (fear)
  const skew=atmPEIV&&atmCEIV?(atmPEIV-atmCEIV).toFixed(1):null;
  let ivInterp='<b style="color:#f0f6fc">IV Smile Reading:</b> ';
  if(avgIV>55) ivInterp+='<span style="color:#f85149">High IV (>55%)</span> — market pricing big move. Premium sellers have edge: short straddles/strangles. ';
  else if(avgIV>35) ivInterp+='<span style="color:#d29922">Normal IV (35–55%)</span> — balanced. Debit spreads vs. credit spreads both viable depending on direction. ';
  else ivInterp+='<span style="color:#3fb950">Low IV (<35%)</span> — cheap options. Buy ATM straddle / debit spreads before a catalyst. ';
  if(skew!==null){{
    if(parseFloat(skew)>3) ivInterp+=`<br><b style="color:#f0f6fc">Skew:</b> <span style="color:#f85149">+${{skew}}% put skew</span> — market paying premium for downside protection. Bearish fear dominant. Short PE spread or sell puts at support.`;
    else if(parseFloat(skew)<-3) ivInterp+=`<br><b style="color:#f0f6fc">Skew:</b> <span style="color:#3fb950">${{skew}}% call skew</span> — market pricing upside breakout. Bullish sentiment in options. Short CE spread or sell calls at resistance.`;
    else ivInterp+=`<br><b style="color:#f0f6fc">Skew:</b> <span style="color:#8b949e">Symmetric (${{skew}}%)</span> — no directional fear premium. Market neutral.`;
  }}
  document.getElementById('iv-interp').innerHTML=ivInterp;
}})();

// --- VOLUME BARS ---
const volRows=ng.strikes.filter(r=>Math.abs(r.strike-atm)<=8*step).reverse();
const mxVol=Math.max(...volRows.map(r=>(r.CE?.volume||0)+(r.PE?.volume||0)),1);
let vb='';
for(const r of volRows){{
  const cv=r.CE?.volume||0,pv=r.PE?.volume||0,tot=cv+pv||1;
  const cp=(cv/tot*100).toFixed(0),pp=(pv/tot*100).toFixed(0);
  vb+=`<div class="vol-row"><div class="vol-sk">${{r.strike}}</div><div class="vol-track" style="height:${{Math.max(6,Math.round(tot/mxVol*14))}}px"><div class="vol-ce" style="width:${{cp}}%"></div><div class="vol-pe" style="width:${{pp}}%"></div></div></div>`;
}}
document.getElementById('volbars').innerHTML=vb||'<div style="color:#8b949e;font-size:11px">No volume data</div>';

// --- EXPECTED MOVE CARD ---
(function(){{
  const s=SIG;
  if(!s.em)return;
  document.getElementById('em-pct').textContent=`±${{f(s.em,1)}}% (${{s.em_pts!=null?'±'+f(s.em_pts,1)+' pts':'—'}})`;
  const lo=s.em_low, hi=s.em_high, pr=s.price, st=s.atm, strd=s.strd;
  const em=s.em;

  let strategy='', stratCol='#d29922';
  if(em<3){{strategy='Low IV — <b>BUY the straddle/strangle</b>. Market underpricing risk. One big move pays for multiple contracts.'; stratCol='#3fb950';}}
  else if(em<5){{strategy='Moderate IV — <b>directional bias trade preferred</b>. If you have a view on direction, use vertical debit spreads. If no view, sell iron condor between walls.'; stratCol='#d29922';}}
  else{{strategy='High IV — <b>SELL the straddle/strangle</b>. Market overpricing risk. Collect premium and profit from time decay.'; stratCol='#f85149';}}

  const barW=500;
  const lo_pct=(lo-lo*0.995)/(hi*1.005-lo*0.995)*100;
  const hi_pct=(hi-lo*0.995)/(hi*1.005-lo*0.995)*100;
  const pr_pct=(pr-lo*0.995)/(hi*1.005-lo*0.995)*100;

  document.getElementById('em-card').innerHTML=`
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">
      <div style="background:#21262d;border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#8b949e;margin-bottom:4px">LOWER BOUND</div>
        <div style="font-size:20px;font-weight:800;color:#f85149">${{f(lo)}}</div>
        <div style="font-size:10px;color:#8b949e">-${{f(s.em_pts,1)}} pts (-${{f(em,1)}}%)</div>
      </div>
      <div style="background:#21262d;border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#8b949e;margin-bottom:4px">CURRENT PRICE</div>
        <div style="font-size:20px;font-weight:800;color:#58a6ff">${{f(pr)}}</div>
        <div style="font-size:10px;color:#8b949e">ATM Straddle: ${{f(strd)}} pts</div>
      </div>
      <div style="background:#21262d;border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#8b949e;margin-bottom:4px">UPPER BOUND</div>
        <div style="font-size:20px;font-weight:800;color:#3fb950">${{f(hi)}}</div>
        <div style="font-size:10px;color:#8b949e">+${{f(s.em_pts,1)}} pts (+${{f(em,1)}}%)</div>
      </div>
    </div>
    <div style="background:#21262d;border-radius:6px;padding:12px;margin-bottom:10px">
      <div style="font-size:10px;color:#8b949e;margin-bottom:8px">EXPECTED RANGE VISUALIZER</div>
      <div style="position:relative;height:24px;background:#0d1117;border-radius:4px;overflow:hidden">
        <div style="position:absolute;left:${{lo_pct.toFixed(1)}}%;right:${{(100-hi_pct).toFixed(1)}}%;top:0;bottom:0;background:#58a6ff1a;border-left:2px solid #f85149;border-right:2px solid #3fb950"></div>
        <div style="position:absolute;left:${{pr_pct.toFixed(1)}}%;top:0;bottom:0;width:2px;background:#58a6ff;transform:translateX(-50%)"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;margin-top:4px">
        <span style="color:#f85149">${{f(lo)}} (-${{f(em,1)}}%)</span>
        <span style="color:#58a6ff">Current: ${{f(pr)}}</span>
        <span style="color:#3fb950">${{f(hi)}} (+${{f(em,1)}}%)</span>
      </div>
    </div>
    <div style="background:#21262d;border-radius:6px;padding:10px;font-size:11px;color:${{stratCol}};line-height:1.7;margin-bottom:8px">
      <b>Strategy:</b> ${{strategy}}
    </div>
    <div style="font-size:11px;color:#8b949e;line-height:1.7;padding:8px 10px;background:#161b22;border-radius:6px">
      <b style="color:#f0f6fc">How to read:</b> The market is pricing a move of <b style="color:#d29922">±${{f(em,1)}}% (±${{f(s.em_pts,1)}} pts)</b> until expiry ${{ng.expiry}}.<br>
      &bull; If price stays within ${{f(lo)}}–${{f(hi)}}, <b>option sellers win</b> (the straddle premium decays).<br>
      &bull; If price breaks outside this range, <b>option buyers win</b>.<br>
      &bull; The straddle costs <b>${{f(strd)}} pts (₹${{foF(Math.round(strd*1250))}} per lot)</b>. That is your breakeven for a long straddle.<br>
      &bull; For institutionals: use this range as the iron condor wings — sell ${{f(lo)}} PE and ${{f(hi)}} CE, collect premium within range.
    </div>`;
}})();

// --- TRADE SIGNALS ---
(function(){{
  const s=SIG;
  // Bias banner
  document.getElementById('sig-bias').innerHTML=`<div class="sig-bias-lbl" style="color:${{s.bias_col}}">${{s.bias}}</div><div class="sig-bias-note">${{s.bias_note}}</div>`;
  document.getElementById('sig-bias').style.borderColor=s.bias_col+'44';
  document.getElementById('sig-bias').style.background=s.bias_col+'0d';

  // Max pain
  const mpDist=s.mp_dist;
  const mpColor=mpDist==null?'#8b949e':mpDist>0?'#f85149':mpDist<0?'#3fb950':'#58a6ff';
  document.getElementById('sig-mp').innerHTML=
    `<div style="margin-bottom:6px">${{s.mp_note}}</div>`+
    `<div style="color:${{mpColor}};font-weight:700;font-size:12px">&rarr; ${{s.mp_action}}</div>`;

  // OI wall
  document.getElementById('sig-wall').innerHTML=s.wall_note;

  // IV strategy
  document.getElementById('sig-iv').innerHTML=`<b>IV Environment:</b> ${{s.iv_strategy}}`;
  document.getElementById('sig-iv').style.borderColor=s.iv_col+'44';
  document.getElementById('sig-iv').style.color=s.iv_col;

  // Setups
  let sh='';
  for(const t of s.setups){{
    sh+=`<div class="setup-card" style="border-left:3px solid ${{t.col}}">
      <div class="setup-header">
        <div class="setup-name">${{t.name}}</div>
        <div class="setup-type" style="color:${{t.col}};border:1px solid ${{t.col}}44">${{t.type}}</div>
      </div>
      <div class="setup-row"><div class="setup-lbl">Entry</div><div class="setup-val">${{t.entry}}</div></div>
      <div class="setup-row"><div class="setup-lbl">Target</div><div class="setup-val gr">${{t.target}}</div></div>
      <div class="setup-row"><div class="setup-lbl">Stop</div><div class="setup-val rd">${{t.stop}}</div></div>
      <div class="setup-row"><div class="setup-lbl">Rationale</div><div class="setup-val neu">${{t.rationale}}</div></div>
    </div>`;
  }}
  document.getElementById('sig-setups').innerHTML=sh||'<div style="color:#8b949e;font-size:11px">No setups generated.</div>';

  // Flags
  let fh='';
  for(const fl of s.flags) fh+=`<div class="flag-row">${{fl}}</div>`;
  document.getElementById('sig-flags').innerHTML=fh;
}})();

// --- GREEKS TABLE (ATM ±4) ---
const gRows=ng.strikes.filter(r=>Math.abs(r.strike-atm)<=4*step);
let gr='';
for(const r of gRows){{
  const ce=r.CE||{{}},pe=r.PE||{{}};
  const iA=r.strike===atm;
  gr+=`<tr class="${{iA?'atm':''}}">
    <td class="ce">${{ivCell(ce.iv)}}</td>
    <td class="ce">${{gk(ce.delta)}}</td>
    <td class="ce">${{gk(ce.theta)}}</td>
    <td class="ce">${{gk(ce.vega)}}</td>
    <td class="ce">${{gk(ce.gamma,6)}}</td>
    <td class="sk" style="font-weight:700">${{r.strike}}${{iA?' ★':''}}</td>
    <td class="pe">${{gk(pe.gamma,6)}}</td>
    <td class="pe">${{gk(pe.vega)}}</td>
    <td class="pe">${{gk(pe.theta)}}</td>
    <td class="pe">${{gk(pe.delta)}}</td>
    <td class="pe">${{ivCell(pe.iv)}}</td>
  </tr>`;
}}
document.getElementById('greeksbody').innerHTML=gr;

// --- FULL CHAIN TABLE ---
const mxCOI=Math.max(...ng.strikes.map(r=>r.CE?.oi||0),1);
const mxPOI=Math.max(...ng.strikes.map(r=>r.PE?.oi||0),1);
document.getElementById('csub').textContent=`Expiry: ${{ng.expiry}} • ${{ng.strikes.length}} strikes • OI in lots • Underlying: ${{ng.underlying_price}} • Max Pain: ${{ng.max_pain}}`;
let th='';
for(const r of ng.strikes){{
  const ce=r.CE||{{}},pe=r.PE||{{}};
  const iA=r.strike===atm,iM=r.strike===ng.max_pain&&!iA;
  const cbw=Math.round((ce.oi||0)/mxCOI*50),pbw=Math.round((pe.oi||0)/mxPOI*50);
  th+=`${{iM?'<tr class="mp-line"><td colspan="15"></td></tr>':''}}
  <tr class="${{iA?'atm':''}}">
    <td class="ce">${{chip(ce.buildup)}}</td>
    <td class="ce">${{ochg(ce.oi_chg)}}</td>
    <td class="ce"><span class="ibar ib-ce" style="width:${{cbw}}px"></span>${{fo(ce.oi)}}</td>
    <td class="ce">${{fo(ce.volume)}}</td>
    <td class="ce">${{ivCell(ce.iv)}}</td>
    <td class="ce">${{pctCell(ce.pct_change)}}</td>
    <td class="ce" style="font-weight:600">${{f(ce.ltp)}}</td>
    <td class="sk" style="font-weight:700">${{r.strike}}${{iA?' ★':''}}${{iM?' &#9670;':''}}</td>
    <td class="pe" style="font-weight:600">${{f(pe.ltp)}}</td>
    <td class="pe">${{pctCell(pe.pct_change)}}</td>
    <td class="pe">${{ivCell(pe.iv)}}</td>
    <td class="pe">${{fo(pe.volume)}}</td>
    <td class="pe"><span class="ibar ib-pe" style="width:${{pbw}}px"></span>${{fo(pe.oi)}}</td>
    <td class="pe">${{ochg(pe.oi_chg)}}</td>
    <td class="pe">${{chip(pe.buildup)}}</td>
  </tr>`;
}}
document.getElementById('tbody').innerHTML=th;
</script>
</body>
</html>"""

    dashboard_file.write_text(html, encoding='utf-8')
    print(f"  [OK] Dashboard: {dashboard_file}")


# -----------------------------------------------------------------------------
# ONE FULL CYCLE
# -----------------------------------------------------------------------------

def run_cycle(obj: SmartConnect, master: pd.DataFrame) -> dict:
    snapshots = {}
    for contract in CONTRACTS:
        prev = load_previous_snapshot(contract)
        try:
            snap = build_chain_snapshot(obj, master, contract, prev_snapshot=prev)
            snapshots[contract] = snap
        except Exception as e:
            print(f"  ERROR building {contract} chain: {e}")
            snapshots[contract] = {'error': str(e), 'timestamp': datetime.now().isoformat()}
    return snapshots


def print_summary(snapshots: dict):
    print(f"\n{'='*70}")
    for contract, snap in snapshots.items():
        if 'error' in snap:
            print(f"{contract}: ERROR - {snap['error']}")
            continue
        print(f"\n{contract}  (expiry {snap['expiry']})")
        print(f"  Underlying: {snap['underlying_price']:.2f}  |  ATM: {snap['atm_strike']}")
        print(f"  PCR: {snap['pcr']}  |  Total CE OI: {snap['total_ce_oi']:,}  |  Total PE OI: {snap['total_pe_oi']:,}")
        print(f"  Max OI Resistance (CE): {snap['max_oi_resistance']}  |  Max OI Support (PE): {snap['max_oi_support']}")
    print(f"{'='*70}\n")


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop', action='store_true', help='Run continuously during market hours')
    parser.add_argument('--interval', type=int, default=POLL_SECONDS, help='Seconds between polls in loop mode')
    parser.add_argument('--push', action='store_true', help='Push dashboard.html to GitHub Pages after each cycle')
    parser.add_argument('--no-push', action='store_true', help='Generate dashboard only, skip push (used by GitHub Actions)')
    args = parser.parse_args()

    env = load_env()
    obj = login(env)

    if not args.loop:
        # Single snapshot run
        master = fetch_scrip_master()
        snapshots = run_cycle(obj, master)
        save_live_snapshot(snapshots)
        append_intraday_history(snapshots)
        generate_html_dashboard(snapshots)
        if args.push:
            import push_dashboard; push_dashboard.push()
        print_summary(snapshots)
        return

    # Loop mode
    print(f"Starting loop mode - polling every {args.interval}s during MCX hours "
          f"({MARKET_OPEN}-{MARKET_CLOSE} IST, Mon-Sat)")
    master = None
    master_fetch_date = None
    eod_saved_today = False

    while True:
        now = datetime.now()

        if not is_market_open():
            print(f"{now.strftime('%H:%M:%S')} Market closed - sleeping 5 min")
            eod_saved_today = False   # reset flag for tomorrow
            time.sleep(300)
            continue

        # Refresh scrip master once per day
        if master is None or master_fetch_date != date.today():
            master = fetch_scrip_master()
            master_fetch_date = date.today()

        try:
            snapshots = run_cycle(obj, master)
            save_live_snapshot(snapshots)
            append_intraday_history(snapshots)
            generate_html_dashboard(snapshots)
            if args.push:
                import push_dashboard; push_dashboard.push()
            print_summary(snapshots)

            # Save EOD snapshot once, shortly before close
            if now.time() >= dtime(23, 25) and not eod_saved_today:
                save_eod_snapshot(snapshots)
                eod_saved_today = True

        except Exception as e:
            print(f"Cycle error: {e}")

        time.sleep(args.interval)


if __name__ == '__main__':
    main()
