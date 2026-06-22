# MCX Natural Gas Option Chain Fetcher

Fetches live option chain data for both NATURALGAS (institutional, 1250
mmBtu lot) and NATGASMINI (retail, 250 mmBtu lot) at the nearest expiry,
using your Angel One SmartAPI account.

## Setup (one-time)

1. Install dependencies:
   ```
   pip install smartapi-python pyotp pandas requests logzero websocket-client
   ```

2. Get your TOTP secret (if you haven't already):
   - Visit https://smartapi.angelone.in/enable-totp
   - Login with client code + MPIN, verify OTP
   - Copy the **text secret** shown below the QR code (not the QR itself)

3. Copy `.env.example` to `.env` and fill in your real credentials:
   ```
   cp .env.example .env
   ```
   Then edit `.env` with your actual API key, client code, MPIN, and TOTP secret.

   **Never commit `.env` to git.** It's already excluded via `.gitignore`.

## Usage

**Single snapshot** (good for testing, or running once manually):
```
python ng_option_chain.py
```

**Continuous loop** (polls every 30s during MCX hours, saves EOD snapshot at close):
```
python ng_option_chain.py --loop
```

Custom poll interval:
```
python ng_option_chain.py --loop --interval 60
```

## Output files

```
output/
├── live_snapshot.json          # always current — dashboard reads this
└── history/
    ├── intraday_2026-06-19.jsonl   # every poll appended, one file per day
    └── EOD_2026-06-19.json         # saved once near market close (23:25 IST)
```

## What's in live_snapshot.json

```json
{
  "NATURALGAS": {
    "contract": "NATURALGAS",
    "expiry": "2026-06-25",
    "underlying_price": 284.5,
    "atm_strike": 285,
    "pcr": 1.12,
    "total_ce_oi": 45000,
    "total_pe_oi": 50400,
    "max_oi_resistance": 300,
    "max_oi_support": 270,
    "strikes": [
      {
        "strike": 270,
        "CE": { "ltp": 18.5, "oi": 3200, "oi_chg": 150, "buildup": "Long Buildup", ... },
        "PE": { "ltp": 4.2,  "oi": 8500, "oi_chg": -80, "buildup": "Short Covering", ... }
      },
      ...
    ]
  },
  "NATGASMINI": { ... same structure ... }
}
```

## Notes

- **Session expires at midnight IST** — you'll need to re-run (which re-logs-in via fresh TOTP) each day.
- **Scrip Master is ~40MB** and refetched once per day automatically in loop mode.
- **MCX hours**: 9:00 AM – 11:30 PM IST, Monday–Saturday. The loop sleeps outside these hours.
- **OI buildup classification** needs a previous snapshot to compare against — the first run of the day will show "No prior data" for every strike, which is expected.
