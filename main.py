"""
Multi-Coin Binance USDT-M Futures Scanner
------------------------------------------
Scans Binance USDT-margined perpetual futures for trend-following entries,
gated by a BTC market bias filter, and sends Telegram alerts.

Strategy rules implemented:
1. Universe: USDT-margined perpetuals with 24h quote volume > $50,000,000.
2. Timeframe: 15-minute candles.
3. Market filter: BTC/USDT must be checked first. LONG signals on altcoins are
   only allowed while BTC is above its own 200 EMA; SHORT signals are only
   allowed while BTC is below its own 200 EMA.
4. Trend strength: ADX(14) must be above 25.
5. Entry:
   - LONG: close > 200 EMA and RSI(14) just crossed up through 30.
   - SHORT: close < 200 EMA and RSI(14) just crossed down through 70.
6. Risk: Stop Loss = 1.5x ATR(14), Take Profit = 3x ATR(14) (1:2 risk:reward).
7. Alerts: Telegram message with ticker, direction, entry, SL, TP, and a risk note.
8. Execution: runs forever, re-scanning once per closed 15-minute candle.

IMPORTANT, please read before running:
- This script is for education and personal use. It is not financial advice,
  and nothing here guarantees profitability. Backtest and paper-trade the
  logic before risking real capital, ideally on Binance's futures testnet.
- The scanner only reads public market data (klines, tickers) and sends
  Telegram messages. It does not place any orders. Because of that, the
  Binance API key/secret below are technically optional for this script as
  written; they are included so you can extend it later (position sizing,
  auto-execution, higher private rate limits) without restructuring anything.
- pandas_ta has had compatibility issues with numpy 2.0 in the past (it used
  the removed alias np.NaN). If pip install works but you get an
  AttributeError mentioning "np.NaN" at runtime, either upgrade pandas_ta to
  a version that fixes this, or run: pip install "numpy<2.0"
  I cannot confirm from here which state your environment will be in, so
  please check the error message if one appears.
- ccxt has deprecated the old options={'defaultType': 'future'} pattern for
  Binance. This script uses the dedicated ccxt.binanceusdm() class instead,
  which is the current recommended way to reach Binance USD-M futures. ccxt
  changes over time, so if a call in this script errors out with something
  like "not supported" or "unknown method", check the ccxt changelog/docs for
  the version you have installed.
"""
from keep_alive import keep_alive
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import pandas_ta as ta
import requests

# ============================================================
# CONFIGURATION - PASTE YOUR CREDENTIALS HERE
# ============================================================
# You can either replace the placeholder strings directly, or (recommended
# for anything other than quick local testing) set these as environment
# variables and leave the code untouched, e.g.:
#   export BINANCE_API_KEY="..."
#   export BINANCE_API_SECRET="..."
#   export TELEGRAM_BOT_TOKEN="..."
#   export TELEGRAM_CHAT_ID="..."
# Hardcoding real keys directly in a script you might commit to git or share
# is a real risk, please keep that in mind.
import os

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "Ys3wVCi37k12Ic8wfUlhvivyEV4EEgZAYdCCJ45n6OnJLPice0Pid5XuER47wVZn")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "b9rLqSnv9NYwYwc3VwXgXZD0BEyIRIE0Vk6VrztCx8eq9lHgquEQvC8cHfJ1liZ8")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8841503761:AAEwLyQd4KkVzCvyfhxK1ZmNBbnu20UigbQ")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8081956847")

# ============================================================
# STRATEGY CONFIGURATION
# ============================================================
TIMEFRAME = "15m"
TIMEFRAME_MINUTES = 15
BTC_SYMBOL = "BTC/USDT:USDT"          # ccxt unified symbol for BTC perpetual

MIN_24H_VOLUME_USDT = 50_000_000       # Rule 1

EMA_LENGTH = 200
RSI_LENGTH = 14
ATR_LENGTH = 14
ADX_LENGTH = 14
ADX_THRESHOLD = 25                     # Rule 4

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

ATR_SL_MULTIPLIER = 1.5                # Rule 6
ATR_TP_MULTIPLIER = 3.0                # Rule 6 (3 / 1.5 = 2, i.e. 1:2 R:R)

RISK_PCT_OF_CAPITAL = 1.0              # Rule 7, informational text only

OHLCV_LIMIT = 500                      # extra candles so the 200 EMA has converged
LOOP_BUFFER_SECONDS = 15               # small pad after each candle close

LOG_FILE = "futures_scanner.log"


# ============================================================
# LOGGING
# ============================================================
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE),
        ],
    )


# ============================================================
# EXCHANGE / DATA HELPERS
# ============================================================
def get_exchange():
    """
    Uses the dedicated binanceusdm class for Binance USDT-margined futures.
    This is ccxt's current recommended way to reach this market.
    """
    exchange = ccxt.binanceusdm(
        {
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {
                "adjustForTimeDifference": True,  # সার্ভারের সময়ের ব্যবধান ঠিক রাখবে
            }
        }
    )
    return exchange


def get_usdt_futures_symbols(exchange, min_quote_volume):
    """
    Returns unified symbols (e.g. 'ETH/USDT:USDT') for all active USDT-margined
    linear perpetual swaps whose 24h quote volume exceeds min_quote_volume.
    """
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()

    symbols = []
    for symbol, market in markets.items():
        if not market.get("swap") or not market.get("linear"):
            continue
        if market.get("quote") != "USDT":
            continue
        if not market.get("active", True):
            continue

        ticker = tickers.get(symbol)
        if not ticker:
            continue

        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None:
            continue

        if quote_volume >= min_quote_volume:
            symbols.append(symbol)

    return sorted(symbols)


def compute_indicators(df):
    """
    Adds EMA_200, RSI_14, ATR_14, ADX_14 columns to df in place-ish (returns df).
    The ADX column name is located dynamically rather than hardcoded, because
    the exact column naming from pandas_ta's adx() has varied a bit across
    releases. If this ever comes back empty, print adx_df.columns to see what
    your installed version actually returns.
    """
    df["EMA_200"] = ta.ema(df["close"], length=EMA_LENGTH)
    df["RSI_14"] = ta.rsi(df["close"], length=RSI_LENGTH)
    df["ATR_14"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LENGTH)

    adx_df = ta.adx(df["high"], df["low"], df["close"], length=ADX_LENGTH)
    adx_col = None
    if adx_df is not None:
        matches = [c for c in adx_df.columns if c.upper().startswith("ADX")]
        if matches:
            adx_col = matches[0]

    df["ADX_14"] = adx_df[adx_col] if adx_col else pd.NA
    return df


def build_dataframe(exchange, symbol):
    """
    Fetches OHLCV candles for symbol and returns a DataFrame with indicators,
    or None if there is not enough data to work with.
    """
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=OHLCV_LIMIT)
    if not ohlcv or len(ohlcv) < (EMA_LENGTH + 10):
        return None

    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return compute_indicators(df)


def get_btc_market_bias(exchange):
    """
    Rule 3: check BTC first. Returns 'LONG' if BTC is above its own 200 EMA,
    'SHORT' if below, or None if this cannot be determined this cycle.
    """
    df = build_dataframe(exchange, BTC_SYMBOL)
    if df is None or len(df) < 3:
        return None

    current = df.iloc[-2]  # last fully closed candle
    if pd.isna(current["EMA_200"]):
        return None

    if current["close"] > current["EMA_200"]:
        return "LONG"
    elif current["close"] < current["EMA_200"]:
        return "SHORT"
    return None


# ============================================================
# SIGNAL LOGIC
# ============================================================
def check_signal(df, symbol, btc_bias):
    """
    Evaluates Rules 4 and 5 against the last fully closed candle and returns
    a signal dict (with Rule 6 SL/TP already calculated) or None.
    Uses df.iloc[-2] as the "current" closed candle and df.iloc[-3] as the
    prior one, since df.iloc[-1] may still be an in-progress candle depending
    on exactly when fetch_ohlcv was called.
    """
    if len(df) < 3:
        return None

    current = df.iloc[-2]
    previous = df.iloc[-3]

    required = ["ADX_14", "EMA_200", "ATR_14", "RSI_14"]
    if any(pd.isna(current[col]) for col in required):
        return None
    if pd.isna(previous["RSI_14"]):
        return None

    if current["ADX_14"] <= ADX_THRESHOLD:
        return None

    price = current["close"]
    ema200 = current["EMA_200"]
    atr = current["ATR_14"]
    rsi_now = current["RSI_14"]
    rsi_prev = previous["RSI_14"]

    direction = None
    if btc_bias == "LONG" and price > ema200 and rsi_prev < RSI_OVERSOLD <= rsi_now:
        direction = "LONG"
    elif btc_bias == "SHORT" and price < ema200 and rsi_prev > RSI_OVERBOUGHT >= rsi_now:
        direction = "SHORT"

    if direction is None:
        return None

    if direction == "LONG":
        stop_loss = price - (ATR_SL_MULTIPLIER * atr)
        take_profit = price + (ATR_TP_MULTIPLIER * atr)
    else:
        stop_loss = price + (ATR_SL_MULTIPLIER * atr)
        take_profit = price - (ATR_TP_MULTIPLIER * atr)

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": float(price),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "adx": float(current["ADX_14"]),
        "rsi": float(rsi_now),
        "candle_time": current["timestamp"],
    }


def calculate_position_size(account_balance, risk_pct, entry, stop_loss):
    """
    Optional helper, not wired into the main loop since this script does not
    fetch your account balance automatically. Call it yourself if you want a
    suggested position size, e.g.:
        balance = exchange.fetch_balance()['USDT']['free']
        size = calculate_position_size(balance, RISK_PCT_OF_CAPITAL, entry, sl)
    Returns position size in units of the base asset.
    """
    risk_amount = account_balance * (risk_pct / 100.0)
    price_risk = abs(entry - stop_loss)
    if price_risk == 0:
        return 0.0
    return risk_amount / price_risk


# ============================================================
# ALERTS
# ============================================================
def format_price(value):
    """Crypto prices span many orders of magnitude, scale decimals to match."""
    if value >= 1:
        return f"{value:.4f}"
    elif value >= 0.01:
        return f"{value:.6f}"
    else:
        return f"{value:.8f}"


def format_alert_message(signal):
    lines = [
        "Futures Scanner Signal",
        "",
        f"Coin: {signal['symbol']}",
        f"Direction: {signal['direction']}",
        f"Entry: {format_price(signal['entry'])}",
        f"Stop Loss: {format_price(signal['stop_loss'])}",
        f"Take Profit: {format_price(signal['take_profit'])}",
        f"Risk: Max {RISK_PCT_OF_CAPITAL:.0f}% of capital",
        f"ADX(14): {signal['adx']:.1f}",
        f"RSI(14): {signal['rsi']:.1f}",
        f"Timeframe: {TIMEFRAME}",
        f"Candle (UTC): {signal['candle_time']}",
    ]
    return "\n".join(lines)


def send_telegram_alert(message):
    if "PASTE_YOUR" in TELEGRAM_BOT_TOKEN or "PASTE_YOUR" in TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials are not configured, alert not sent.")
        logging.info("Message that would have been sent:\n%s", message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.error("Failed to send Telegram alert: %s", exc)


# ============================================================
# MAIN SCAN CYCLE
# ============================================================
def run_scan_cycle(exchange, last_alert_candle):
    bias = get_btc_market_bias(exchange)
    if bias is None:
        logging.warning("Could not determine a BTC 200 EMA bias this cycle, skipping.")
        return

    logging.info("BTC 200 EMA bias: %s", bias)

    symbols = get_usdt_futures_symbols(exchange, MIN_24H_VOLUME_USDT)
    logging.info("%d symbols pass the $%s 24h volume filter.", len(symbols), f"{MIN_24H_VOLUME_USDT:,.0f}")

    for symbol in symbols:
        if symbol == BTC_SYMBOL:
            continue
        try:
            df = build_dataframe(exchange, symbol)
            if df is None:
                continue

            signal = check_signal(df, symbol, bias)
            if signal is None:
                continue

            dedup_key = (symbol, signal["direction"])
            if last_alert_candle.get(dedup_key) == signal["candle_time"]:
                continue  # already alerted for this exact candle
            last_alert_candle[dedup_key] = signal["candle_time"]

            logging.info("Signal found: %s", signal)
            send_telegram_alert(format_alert_message(signal))

        except ccxt.BaseError as exc:
            logging.warning("Exchange error while analyzing %s: %s", symbol, exc)
        except Exception as exc:
            logging.exception("Unexpected error while analyzing %s: %s", symbol, exc)


def seconds_until_next_close(interval_minutes=15, buffer_seconds=15):
    """
    Aligns the loop to real candle closes (e.g. :00, :15, :30, :45 UTC) plus a
    small buffer, instead of a naive fixed sleep that would slowly drift.
    """
    now = datetime.now(timezone.utc)
    remainder = now.minute % interval_minutes
    minutes_to_next = interval_minutes - remainder if remainder != 0 else interval_minutes
    next_close = (now + timedelta(minutes=minutes_to_next)).replace(second=0, microsecond=0)
    wait_seconds = (next_close - now).total_seconds() + buffer_seconds
    return max(wait_seconds, buffer_seconds)


def main():
    keep_alive()
    setup_logging()
    exchange = get_exchange()
    exchange.load_markets()

    logging.info("Multi-Coin Binance Futures Scanner starting up.")
    send_telegram_alert("Futures Scanner started and connected to Binance.")

    last_alert_candle = {}

    while True:
        try:
            run_scan_cycle(exchange, last_alert_candle)
        except Exception as exc:
            logging.exception("Unhandled error in scan cycle: %s", exc)

        wait_seconds = seconds_until_next_close(TIMEFRAME_MINUTES, LOOP_BUFFER_SECONDS)
        logging.info("Cycle complete, sleeping %.0fs until the next 15m close.", wait_seconds)
        time.sleep(wait_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Scanner stopped by user.")
        sys.exit(0)
