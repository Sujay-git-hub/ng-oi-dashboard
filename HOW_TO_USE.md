# MCX NATURALGAS OI Dashboard — How To Use

## What This Does

Fetches the live option chain for MCX NATURALGAS (1250 mmBtu lot, institutional) via Angel One SmartAPI. Builds a visual dashboard showing:
- Open Interest wall (support/resistance levels)
- Put-Call Ratio with sentiment interpretation
- Per-strike OI buildup classification (Long/Short Buildup, Short Covering, Long Unwinding)
- Key levels: max CE OI (resistance) and max PE OI (support)

---

## One-Time Setup (already done if you're reading this)

1. **Install Python packages**
   ```
   pip install smartapi-python pyotp requests pandas truststore
   ```

2. **Create `.env` file** in the project folder (copy from `.env.example`, fill in real values):
   ```
   ANGEL_API_KEY=your_api_key
   ANGEL_CLIENT_CODE=your_client_code
   ANGEL_MPIN=your_mpin
   ANGEL_TOTP_SECRET=your_totp_secret
   ```
   Get your TOTP secret from: https://smartapi.angelone.in/enable-totp

---

## Daily Usage

### Option A — Single snapshot (anytime)
```
python ng_option_chain.py
```
- Takes ~30 seconds (downloads ~40MB scrip master)
- Generates `output/dashboard.html` — open this in your browser

### Option B — Live loop during market hours (recommended)
```
python ng_option_chain.py --loop
```
- Runs continuously Mon–Sat, 9:00 AM – 11:30 PM IST
- Refreshes every 30 seconds
- Dashboard auto-reloads every 60 seconds
- Saves EOD snapshot automatically near market close (11:25 PM)
- Press Ctrl+C to stop

### View the dashboard
Open `output/dashboard.html` directly in Chrome/Edge/Firefox.
No server needed — the data is embedded in the file.

---

## Output Files

| File | What it is |
|------|------------|
| `output/dashboard.html` | Live dashboard — open this in your browser |
| `output/live_snapshot.json` | Raw JSON of last snapshot |
| `output/history/intraday_YYYY-MM-DD.jsonl` | Every snapshot from today appended |
| `output/history/EOD_YYYY-MM-DD.json` | End-of-day snapshot (saved near market close) |

---

## GitHub Pages Hosting

The dashboard HTML has **no credentials** — only market data. Safe to host publicly.

### Push dashboard to GitHub after each run:
```
python push_dashboard.py
```
This copies `output/dashboard.html` to the `ng-oi-dashboard` GitHub repo and deploys it to GitHub Pages.

Live URL (after first push): **https://sujay-git-hub.github.io/ng-oi-dashboard/**

### Or run with auto-push:
```
python ng_option_chain.py --loop --push
```
Pushes the dashboard to GitHub after every cycle (adds ~2s per cycle).

---

## Reading the Dashboard

### PCR (Put-Call Ratio)
| PCR | Signal |
|-----|--------|
| < 0.7 | Strongly bullish — call sellers dominant |
| 0.7–1.0 | Mildly bullish |
| 1.0–1.3 | Neutral to cautious |
| > 1.3 | Bearish — heavy put buying |

### OI Wall
- **Red bars (left)** = Call OI = resistance above current price
- **Green bars (right)** = Put OI = support below current price
- Highlighted row = ATM strike
- Highlighted red row = max CE OI (strongest resistance)
- Highlighted green row = max PE OI (strongest support)

### Buildup Classification (requires 2+ snapshots)
| Signal | Meaning |
|--------|---------|
| Long Buildup | Price up + OI up — fresh longs entering |
| Short Buildup | Price down + OI up — fresh shorts entering |
| Short Covering | Price up + OI down — shorts exiting (bullish) |
| Long Unwinding | Price down + OI down — longs exiting (bearish) |
| Neutral | No significant change |

**Note:** Buildup shows "NEU" on first snapshot (no prior data to compare). Run for 2+ cycles to see live signals.

---

## MCX Trading Hours
- Monday–Saturday: 9:00 AM – 11:30 PM IST
- Sunday: Closed
- The loop script automatically pauses outside these hours.

## Known Limitations
- Angel One session expires at midnight IST — restart the script each trading day
- Scrip master (~40MB) is downloaded fresh each run — takes 10–20 seconds
- The `optionChain` REST endpoint is not available on this account — script uses scrip-master + batch-quote approach
