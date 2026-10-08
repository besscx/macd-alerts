#!/usr/bin/env python3
"""
MACD(12,26,9) 金叉/死叉 alert bot — 100% free stack.

- Price data : Yahoo Finance via yfinance (free, no API key)
- MACD       : computed locally with pandas (no paid library)
- Alerts     : Telegram Bot API via plain urllib (no extra dependency)

Monitors MACD crossovers per timeframe:
    15m : APP, ASTS, COST
    30m : QQQ, SPY, META, AAPL, AMZN, TSLA, NVDA, SMH, MSFT, APP, ASTS, COST
    4h  : QQQ, SPY, META, AAPL, AMZN, TSLA, NVDA, SMH, MSFT
(4h bars are resampled from 1h bars, anchored at 9:30 ET.)

A signal fires only when the LAST FULLY CLOSED bar completes a
crossover, so alerts never repaint. Each (symbol, timeframe, bar, signal)
alerts exactly once, tracked in state.json.

Usage:
    python macd_alert.py --dry-run                 # print signals, send nothing
    python macd_alert.py                           # one check cycle, send via Telegram
    python macd_alert.py --timeframe 15m           # check only the 15m timeframe
    python macd_alert.py --timeframe 30m           # check only the 30m timeframe
    python macd_alert.py --timeframe 4h            # check only the 4h timeframe

Alert state is kept in state-15m.json / state-30m.json / state-4h.json
(one per timeframe), so each GitHub Actions workflow can commit its own
file without conflicts.

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

import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------- config

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

SYMBOLS = ["QQQ", "SPY", "META", "AAPL", "AMZN", "TSLA", "NVDA"]

# symbols monitored per timeframe
TIMEFRAME_SYMBOLS = {
    "15m": ["APP", "ASTS", "COST"],
    "30m": ["QQQ", "SPY", "META", "AAPL", "AMZN", "TSLA", "NVDA",
            "SMH", "MSFT", "APP", "ASTS", "COST"],
    "4h":  ["QQQ", "SPY", "META", "AAPL", "AMZN", "TSLA", "NVDA",
            "SMH", "MSFT"],
}

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MIN_BARS = 60          # need enough history for the EMAs to stabilise
ET = ZoneInfo("America/New_York")

# timeframe -> (yfinance interval, yfinance period, bar minutes, resample rule)
TIMEFRAMES = {
    "15m": {"interval": "15m", "period": "1mo", "minutes": 15, "resample": None},
    "30m": {"interval": "30m", "period": "1mo", "minutes": 30, "resample": None},
    "4h":  {"interval": "1h",  "period": "3mo", "minutes": 240,
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


def add_macd(df):
    close = df["Close"]
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    df = df.copy()
    df["macd"] = ema_fast - ema_slow
    df["signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    return df


def detect_cross(df):
    """Return 'golden' (金叉), 'death' (死叉) or None, based on the last
    two closed bars: MACD line crossing the signal line."""
    if len(df) < 2:
        return None
    prev = df["macd"].iloc[-2] - df["signal"].iloc[-2]
    curr = df["macd"].iloc[-1] - df["signal"].iloc[-1]
    if prev <= 0 < curr:
        return "golden"
    if prev >= 0 > curr:
        return "death"
    return None


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.status == 200


def format_message(symbol, timeframe, signal, price, macd, sig, bar_open, bar_minutes):
    if signal == "golden":
        head = "🟢 *金叉 GOLDEN CROSS*"
    else:
        head = "🔴 *死叉 DEATH CROSS*"
    day_close = bar_open.floor("D") + pd.Timedelta(hours=16)
    bar_close = min(bar_open + pd.Timedelta(minutes=bar_minutes), day_close)
    return (f"{head}\n"
            f"*{symbol}* · {timeframe}\n"
            f"Price: {price:.2f}\n"
            f"MACD: {macd:.4f} / Signal: {sig:.4f}\n"
            f"Bar closed: {bar_close:%Y-%m-%d %H:%M} ET")


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

            df = add_macd(df)
            signal = detect_cross(df)
            bar_time = df.index[-1]
            state_key = f"{key}|{bar_time.isoformat()}|{signal}"

            if signal and state.get(key) != state_key:
                row = df.iloc[-1]
                msg = format_message(symbol, tf_name, signal, float(row["Close"]),
                                     float(row["macd"]), float(row["signal"]),
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
    args = ap.parse_args()
    run(dry_run=args.dry_run,
        timeframes=[args.timeframe] if args.timeframe else None)
