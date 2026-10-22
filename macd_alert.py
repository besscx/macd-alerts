#!/usr/bin/env python3
"""
DDMM 底部买入 / 顶部卖出 alert bot — 100% free stack.

- Price data : Yahoo Finance via yfinance (free, no API key)
- DDMM       : computed locally with pandas (no paid library)
- Alerts     : Telegram Bot API via plain urllib (no extra dependency)

This is a faithful port of the shared DDMM (Tongdaxin-style) formula
(params 12/26/9). Per timeframe it monitors:

    15m : APP, COST, QQQ, VOO, SMH
    30m : QQQ, SPY, META, AAPL, AMZN, TSLA, NVDA, SMH, MSFT, APP, COST
    4h  : QQQ, SPY, SMH + NDX_QUALITY_4H (Nasdaq-100 names passing the
          fundamental screen: TTM EPS > 0, FCF > 0, D/E <= 300;
          see screen_ndx_fundamentals.py; 2026-10-14: 85 pass / 16 excluded)
          + BLUECHIP_4H (Xi's hand-picked large-cap non-Nasdaq blue chips:
          2026-10-14: JNJ, V, MA, LLY, KO, JPM, CAT)
(4h bars are resampled from 1h bars, anchored at 9:30 ET.)

Signal definitions (exactly as in the DDMM script):

  底部买入 BOTTOM BUY — a three-step confirmation chain:
    1. Bottom divergence (直接底背离 or 隔峰底背离): within the current
       below-zero MACD-histogram segment, price makes a lower low than the
       previous (or skip-one) segment's low, while DIFF makes a HIGHER low.
    2. The next bar: DIFF turns back up toward zero by >= 1%
       (底部结构). The FIRST such bar prints 底部买入.
    3. If the structure later fails, the script prints 消失 (not alerted).

  顶部卖出 TOP SELL — mirror image:
    1. Top divergence (price higher high, DIFF lower high, DIFF > 0).
    2. Next bar: DIFF turns down from its high by >= 1% (顶部结构).
       The FIRST such bar prints 顶部卖出.

A signal fires only on the LAST FULLY CLOSED bar, so alerts never
repaint. Each (symbol, timeframe, bar, signal) alerts exactly once,
tracked in state.json.

Usage:
    python macd_alert.py --dry-run                 # print signals, send nothing
    python macd_alert.py                           # one check cycle, send via Telegram
    python macd_alert.py --timeframe 15m           # check only the 15m timeframe
    python macd_alert.py --timeframe 30m           # check only the 30m timeframe
    python macd_alert.py --timeframe 4h            # check only the 4h timeframe
    python macd_alert.py --fear-greed             # hourly CNN Fear & Greed check

Alert state is kept in state-15m.json / state-30m.json / state-4h.json
(one per timeframe), so each GitHub Actions workflow can commit its own
file without conflicts. The CNN Fear & Greed check keeps its own
state-fear-greed.json the same way, and alerts exactly once per entry
into Extreme Fear (re-armed automatically when the rating exits it).

Configure credentials in config.json (see config.example.json) or via
env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Nothing is sent if the
token/chat id are missing — the script just prints what it would send.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------- config

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

# Nasdaq-100 constituents passing the fundamental screen
# (TTM EPS > 0, FCF > 0, D/E <= 300; screened 2026-10-14 via
# screen_ndx_fundamentals.py — 85 pass, 16 excluded: unprofitable
# CRWV/GILD/INTC/LITE/MRNA/MSTR/RKLB/SPCX/TTWO, cash-burning
# AEP/CEG/EXC/NBIS/XEL, extreme leverage AMGN, missing data HONA)
NDX_QUALITY_4H = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "ALAB", "ALNY",
    "AMAT", "AMD", "AMZN", "APP", "ARM", "ASML", "AVGO", "AXON",
    "BKNG", "BKR", "CCEP", "CDNS", "CMCSA", "COST", "CPRT", "CRWD",
    "CSCO", "CSX", "CTAS", "DASH", "DDOG", "DXCM", "FANG", "FAST",
    "FER", "FTNT", "GEHC", "GOOG", "GOOGL", "HON", "IDXX", "INTU",
    "ISRG", "KDP", "KLAC", "LIN", "LRCX", "MAR", "MCHP", "MDLZ",
    "MELI", "META", "MNST", "MPWR", "MRVL", "MSFT", "MU", "NFLX",
    "NVDA", "NXPI", "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "PDD",
    "PEP", "PLTR", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX",
    "SHOP", "SNDK", "SNPS", "STX", "TER", "TMUS", "TRI", "TSLA",
    "TXN", "VRTX", "WDAY", "WDC", "WMT",
]

# Xi's hand-picked large-cap blue chips outside Nasdaq-100 (added 2026-10-14)
BLUECHIP_4H = ["JNJ", "V", "MA", "LLY", "KO", "JPM", "CAT"]

# symbols monitored per timeframe
TIMEFRAME_SYMBOLS = {
    "15m": ["APP", "COST", "QQQ", "VOO", "SMH"],
    "30m": ["QQQ", "SPY", "META", "AAPL", "AMZN", "TSLA", "NVDA",
            "SMH", "MSFT", "APP", "COST"],
    "4h":  sorted(set(["QQQ", "SPY", "SMH"]) | set(NDX_QUALITY_4H)
                  | set(BLUECHIP_4H)),
}

DDMM_FAST, DDMM_SLOW, DDMM_SIGNAL = 12, 26, 9   # DDMM(12,26,9)
MIN_BARS = 60          # need enough history for EMAs + divergence segments
ET = ZoneInfo("America/New_York")

# timeframe -> (yfinance interval, yfinance period, bar minutes, resample rule)
TIMEFRAMES = {
    "15m": {"interval": "15m", "period": "1mo", "minutes": 15, "resample": None},
    "30m": {"interval": "30m", "period": "1mo", "minutes": 30, "resample": None},
    "4h":  {"interval": "1h",  "period": "6mo", "minutes": 240,
            "resample": "4h"},  # 4h bars built from 1h bars, anchored at 9:30 ET
}

# ---------------------------------------------------------------- helpers

def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id", "")
    return token.strip(), str(chat_id).strip()


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(path, state):
    path.write_text(json.dumps(state, indent=2))


def fetch_bars(symbol, tf_cfg):
    """Download intraday bars; resample to 4h when needed."""
    df = yf.download(symbol, interval=tf_cfg["interval"], period=tf_cfg["period"],
                     progress=False, auto_adjust=True)
    if df.empty:
        return df
    # flatten MultiIndex columns -> Open/High/Low/Close/Volume
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.tz_convert(ET)

    if tf_cfg["resample"]:
        # anchor 4h bins at 9:30 ET (market open): 9:30-13:30, 13:30-17:30
        df = df.resample("4h", origin="start_day", offset="90min").agg(
            {"Open": "first", "High": "max", "Low": "min",
             "Close": "last", "Volume": "sum"}).dropna()

    # drop the still-forming bar — only evaluate fully closed bars
    now = datetime.now(ET)
    bar_min = tf_cfg["minutes"]
    while len(df):
        last = df.index[-1]
        day_close = last.floor("D") + pd.Timedelta(hours=16)  # 16:00 ET
        if min(last + pd.Timedelta(minutes=bar_min), day_close) <= now:
            break
        df = df.iloc[:-1]
    return df


# ---------------------------------------------------------------- DDMM port
# Faithful translation of the shared Tongdaxin-style DDMM formula.
# Naming follows the original script (pinyin), so each line can be
# checked against the source.

def _barslast(cond):
    """Tongdaxin BARSLAST: bars since cond was last True (0 = true now).
    NaN where cond was never True."""
    c = cond.fillna(False).to_numpy(dtype=bool)
    idx = np.arange(len(c))
    last_true = np.maximum.accumulate(np.where(c, idx, -1))
    out = np.full(len(c), np.nan)
    ok = last_true >= 0
    out[ok] = idx[ok] - last_true[ok]
    return pd.Series(out, index=cond.index)


def _ref_var(s, n):
    """Tongdaxin REF(X, N) with a per-bar variable offset N."""
    vals = s.to_numpy(dtype=float)
    off = n.to_numpy(dtype=float)
    out = np.full(len(s), np.nan)
    valid = np.isfinite(off)
    tgt = np.empty(len(s), dtype=np.int64)
    tgt[valid] = np.arange(len(s))[valid] - np.round(off[valid]).astype(np.int64)
    ok = valid & (tgt >= 0)
    out[ok] = vals[tgt[ok]]
    return pd.Series(out, index=s.index)


def ddmm_signals(close, diff, dea):
    """Core DDMM divergence/structure logic. Returns a DataFrame with
    boolean columns ddmm_buy (底部买入) and ddmm_sell (顶部卖出)."""
    hist = (diff - dea) * 2  # MACD histogram, same as the script's MACD

    # N1/M1: bars since the histogram crossed below/above the zero axis
    cross_down = (hist.shift(1) >= 0) & (hist < 0)
    cross_up = (hist.shift(1) <= 0) & (hist > 0)
    N1 = _barslast(cross_down)
    M1 = _barslast(cross_up)

    # Segment extremes: LLV/HHV over the current zero-axis segment,
    # implemented as cumulative min/max within each segment.
    seg_below = cross_down.cumsum()
    seg_above = cross_up.cumsum()
    CL1 = close.groupby(seg_below).cummin()
    DIFL1 = diff.groupby(seg_below).cummin()
    CH1 = close.groupby(seg_above).cummax()
    DIFH1 = diff.groupby(seg_above).cummax()

    # Previous-segment extremes via variable REF
    CL2 = _ref_var(CL1, M1 + 1)
    CL3 = _ref_var(CL2, M1 + 1)
    DIFL2 = _ref_var(DIFL1, M1 + 1)
    DIFL3 = _ref_var(DIFL2, M1 + 1)
    CH2 = _ref_var(CH1, N1 + 1)
    CH3 = _ref_var(CH2, N1 + 1)
    DIFH2 = _ref_var(DIFH1, N1 + 1)
    DIFH3 = _ref_var(DIFH2, N1 + 1)

    prev_hist_neg = hist.shift(1) < 0
    prev_hist_pos = hist.shift(1) > 0

    # ---- bottom divergence ----
    direct_bull = (CL1 < CL2) & (DIFL1 > DIFL2) & prev_hist_neg & (diff < 0)
    skip_bull = ((CL1 < CL3) & (DIFL1 < DIFL2) & (DIFL1 > DIFL3)
                 & prev_hist_neg & (diff < 0))
    bull_div = (direct_bull | skip_bull) & (diff < 0)          # 底部背离
    # 底部结构: divergence held yesterday AND DIFF turns up >= 1% toward zero
    bull_struct = (bull_div.shift(1).fillna(False)
                   & (diff.shift(1).abs() >= diff.abs() * 1.01))
    bull_buy = (~bull_struct.shift(1).fillna(False)) & bull_struct  # 底结构形成

    # ---- top divergence (mirror) ----
    direct_bear = (CH1 > CH2) & (DIFH1 < DIFH2) & prev_hist_pos & (diff > 0)
    skip_bear = ((CH1 > CH3) & (DIFH1 > DIFH2) & (DIFH1 < DIFH3)
                 & prev_hist_pos & (diff > 0))
    bear_div = (direct_bear | skip_bear) & (diff > 0)         # 顶部背离
    # 顶部结构: divergence held yesterday AND DIFF turns down >= 1%
    bear_struct = (bear_div.shift(1).fillna(False)
                   & (diff.shift(1) >= diff * 1.01))
    bear_sell = (~bear_struct.shift(1).fillna(False)) & bear_struct  # 顶结构形成

    return pd.DataFrame({"ddmm_buy": bull_buy.fillna(False),
                         "ddmm_sell": bear_sell.fillna(False)},
                        index=close.index)


def add_ddmm(df, fast=DDMM_FAST, slow=DDMM_SLOW, m=DDMM_SIGNAL):
    """Attach DIFF/DEA/histogram plus ddmm_buy / ddmm_sell signals."""
    df = df.copy()
    close = df["Close"]
    diff = (close.ewm(span=fast, adjust=False).mean()
            - close.ewm(span=slow, adjust=False).mean())
    dea = diff.ewm(span=m, adjust=False).mean()
    df["ddmm_diff"] = diff
    df["ddmm_dea"] = dea
    df["ddmm_hist"] = (diff - dea) * 2
    sig = ddmm_signals(close, diff, dea)
    df["ddmm_buy"] = sig["ddmm_buy"]
    df["ddmm_sell"] = sig["ddmm_sell"]
    return df


def detect_ddmm(df):
    """Return 'bottom_buy' (底部买入), 'top_sell' (顶部卖出) or None,
    based on the last fully closed bar."""
    if len(df) < 1:
        return None
    last = df.iloc[-1]
    if bool(last["ddmm_buy"]):
        return "bottom_buy"
    if bool(last["ddmm_sell"]):
        return "top_sell"
    return None


# ---------------------------------------------------------------- alerts

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.status == 200


def format_message(symbol, timeframe, signal, price, diff, dea,
                   bar_open, bar_minutes):
    if signal == "bottom_buy":
        head = "🟢 *底部买入 BOTTOM BUY* (DDMM)"
    else:
        head = "🔴 *顶部卖出 TOP SELL* (DDMM)"
    day_close = bar_open.floor("D") + pd.Timedelta(hours=16)
    bar_close = min(bar_open + pd.Timedelta(minutes=bar_minutes), day_close)
    return (f"{head}\n"
            f"*{symbol}* · {timeframe}\n"
            f"Price: {price:.2f}\n"
            f"DIFF: {diff:.4f} / DEA: {dea:.4f}\n"
            f"Bar closed: {bar_close:%Y-%m-%d %H:%M} ET")


# ---------------------------------------------------------------- CNN Fear & Greed

# CNN's own dataviz endpoint (no API key). Payload shape, as used by
# multiple public clients of this endpoint:
#   {"fear_and_greed": {"score": int, "rating": str, "timestamp": ...},
#    "fear_and_greed_historical": {"data": [{"x": ms_epoch, "y": score}]}}
# The endpoint rejects bare programmatic clients (HTTP 418), so fetch
# with the small header set a browser would send from CNN's index page.
CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_FEAR_GREED_STATE = "state-fear-greed.json"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
CNN_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
}


def _band_from_score(score):
    if score is None:
        return None
    if score <= 24:
        return "Extreme Fear"
    if score <= 44:
        return "Fear"
    if score <= 55:
        return "Neutral"
    if score <= 74:
        return "Greed"
    return "Extreme Greed"


def _normalize_rating(rating, score):
    if rating:
        rating = " ".join(w.capitalize() for w in str(rating).lower().split())
        if rating:
            return rating
    return _band_from_score(score)


def _format_cnn_timestamp(ts):
    if ts is None:
        return None
    try:
        v = float(ts)
        if v > 1e12:  # epoch ms -> s
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=ET).strftime("%Y-%m-%d %H:%M ET")
    except (TypeError, ValueError):
        return str(ts)


def parse_cnn_payload(data):
    """Extract (score, rating, updated_str) from CNN's payload.

    Prefers the current fear_and_greed object; falls back to the last
    point of the historical series (which tracks the current reading).
    Raises ValueError when no usable score is present.
    """
    score, rating, ts = None, None, None
    current = data.get("fear_and_greed") or {}
    if isinstance(current, dict):
        score = current.get("score")
        rating = current.get("rating")
        ts = current.get("timestamp")
    if score is None:
        hist = (data.get("fear_and_greed_historical") or {}).get("data") or []
        if hist:
            last = hist[-1]
            score = last.get("y")
            ts = last.get("x")
    if score is None:
        raise ValueError("CNN payload has no usable score")
    score = int(round(float(score)))
    return score, _normalize_rating(rating, score), _format_cnn_timestamp(ts)


def fetch_cnn_fear_greed():
    """Fetch CNN's Fear & Greed Index; return (score, rating, updated)."""
    req = urllib.request.Request(CNN_FEAR_GREED_URL, headers=CNN_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return parse_cnn_payload(data)


def check_fear_greed(dry_run=False):
    """Hourly CNN Fear & Greed check. Alerts exactly once per entry
    into Extreme Fear; re-arms automatically when the rating exits it.
    A failed fetch leaves state untouched so the next hour retries."""
    token, chat_id = load_config()
    can_send = bool(token and chat_id)
    if not can_send and not dry_run:
        print("No Telegram credentials configured — running in dry-run mode.")
        dry_run = True

    state_path = BASE_DIR / CNN_FEAR_GREED_STATE
    state = load_state(state_path)
    try:
        score, rating, updated = fetch_cnn_fear_greed()
    except Exception as e:  # noqa: BLE001 — a failed fetch must not touch state
        print(f"[fear-greed] fetch failed ({e}) — state unchanged",
              file=sys.stderr)
        return 0

    prev_score = state.get("last_score")
    prev_rating = state.get("last_rating")
    prev_extreme = (prev_rating == "Extreme Fear"
                    or (isinstance(prev_score, (int, float))
                        and prev_score <= 24))
    now_extreme = (rating == "Extreme Fear" or score <= 24)

    prev_txt = (f" — previous: {prev_score} ({prev_rating})"
                if prev_score is not None else " — first reading")
    upd_txt = f" — updated {updated}" if updated else ""
    print(f"[fear-greed] CNN Fear & Greed: {score} ({rating}){upd_txt}{prev_txt}")

    if now_extreme and not prev_extreme:
        msg = (f"🚨 *CNN 恐慌指数进入 Extreme Fear*\n"
               f"当前：{score} / 100（{rating}）\n")
        if prev_score is not None:
            msg += f"上一读数：{prev_score}（{prev_rating}）\n"
        if updated:
            msg += f"更新时间：{updated}"
        if dry_run:
            print(f"[DRY-RUN] would send:\n{msg}\n")
        else:
            try:
                if send_telegram(token, chat_id, msg):
                    print("[fear-greed] alert sent")
                else:
                    print("[fear-greed] send failed", file=sys.stderr)
                    return 0  # do not advance state; retry next hour
            except Exception as e:  # noqa: BLE001
                print(f"[fear-greed] send error: {e}", file=sys.stderr)
                return 0  # do not advance state; retry next hour
    else:
        print("[fear-greed] no alert (not a new entry into Extreme Fear)")

    if not dry_run:
        save_state(state_path, {"last_score": score, "last_rating": rating})
    return 0


# ---------------------------------------------------------------- main

def run(dry_run=False, timeframes=None):
    token, chat_id = load_config()
    can_send = bool(token and chat_id)
    if not can_send and not dry_run:
        print("No Telegram credentials configured — running in dry-run mode.")
        dry_run = True

    tfs = {k: v for k, v in TIMEFRAMES.items()
           if timeframes is None or k in timeframes}
    states = {tf: load_state(BASE_DIR / f"state-{tf}.json") for tf in tfs}
    alerts = 0

    for tf_name, tf_cfg in tfs.items():
        state = states[tf_name]
        for symbol in TIMEFRAME_SYMBOLS[tf_name]:
            key = symbol
            try:
                df = fetch_bars(symbol, tf_cfg)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not kill the run
                print(f"[{symbol} {tf_name}] data error: {e}", file=sys.stderr)
                continue
            if len(df) < MIN_BARS:
                print(f"[{symbol} {tf_name}] not enough history ({len(df)} bars), skipped")
                continue

            df = add_ddmm(df)
            signal = detect_ddmm(df)
            bar_time = df.index[-1]
            state_key = f"{key}|{bar_time.isoformat()}|{signal}"

            if signal and state.get(key) != state_key:
                row = df.iloc[-1]
                msg = format_message(symbol, tf_name, signal, float(row["Close"]),
                                     float(row["ddmm_diff"]), float(row["ddmm_dea"]),
                                     bar_time, tf_cfg["minutes"])
                if dry_run:
                    print(f"[DRY-RUN] would send:\n{msg}\n")
                else:
                    try:
                        if send_telegram(token, chat_id, msg):
                            print(f"[{symbol} {tf_name}] alert sent: {signal} @ {bar_time:%H:%M}")
                        else:
                            print(f"[{symbol} {tf_name}] send failed", file=sys.stderr)
                            continue  # retry next run
                    except Exception as e:  # noqa: BLE001
                        print(f"[{symbol} {tf_name}] send error: {e}", file=sys.stderr)
                        continue  # retry next run
                state[key] = state_key
                alerts += 1
            else:
                print(f"[{symbol} {tf_name}] no new signal "
                      f"(last closed bar {bar_time:%m-%d %H:%M})")

    if not dry_run:
        for tf_name, state in states.items():
            save_state(BASE_DIR / f"state-{tf_name}.json", state)
    print(f"Done. {alerts} new signal(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print signals instead of sending to Telegram")
    ap.add_argument("--timeframe", choices=["15m", "30m", "4h"], default=None,
                    help="check only this timeframe (default: all)")
    ap.add_argument("--fear-greed", action="store_true",
                    help="check only the CNN Fear & Greed Index (hourly job)")
    args = ap.parse_args()
    if args.fear_greed:
        raise SystemExit(check_fear_greed(dry_run=args.dry_run))
    run(dry_run=args.dry_run,
        timeframes=[args.timeframe] if args.timeframe else None)
