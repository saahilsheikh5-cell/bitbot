import os
import threading
import time
import json
import requests
import pandas as pd
import numpy as np
import telebot
from telebot import types
from flask import Flask

# ================= CONFIG =================
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
CHAT_ID = 1263295916
KLINES_URL = "https://api.binance.com/api/v3/klines"

# --- TeleBot & Flask ---
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ================= STORAGE =================
USER_COINS_FILE = "user_coins.json"        # list[str]
SETTINGS_FILE = "settings.json"            # dict
LAST_SIGNAL_FILE = "last_signals.json"     # dict: key "COIN_INTERVAL" -> last_ts
MUTED_COINS_FILE = "muted_coins.json"      # list[str]
COIN_INTERVALS_FILE = "coin_intervals.json"# dict: coin -> list[str]

def _safe_write_json(path, obj):
    try:
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception as e:
        print(f"[WARN] Failed to write {path}: {e}")

def _safe_read_json(path, default):
    if not os.path.exists(path):
        _safe_write_json(path, default)
        return default
    try:
        with open(path, "r") as f:
            data = json.load(f)
        # shape-guard by type
        if isinstance(default, list) and not isinstance(data, list):
            print(f"[WARN] {path} had wrong shape; resetting.")
            _safe_write_json(path, default)
            return default
        if isinstance(default, dict) and not isinstance(data, dict):
            print(f"[WARN] {path} had wrong shape; resetting.")
            _safe_write_json(path, default)
            return default
        return data
    except Exception as e:
        print(f"[WARN] Failed to read {path}: {e}, resetting.")
        _safe_write_json(path, default)
        return default

# initialize files and in-memory state
coins = _safe_read_json(USER_COINS_FILE, [])
settings = _safe_read_json(SETTINGS_FILE, {"rsi_buy": 20, "rsi_sell": 80, "signal_validity_min": 15})
last_signals = _safe_read_json(LAST_SIGNAL_FILE, {})
muted_coins = _safe_read_json(MUTED_COINS_FILE, [])
coin_intervals = _safe_read_json(COIN_INTERVALS_FILE, {})

# in-memory toggle (not persisted)
auto_signals_enabled = True

# ================= TECHNICAL ANALYSIS =================
def get_klines(symbol, interval="15m", limit=100):
    """
    Returns list of close prices (floats). Handles Binance errors gracefully.
    """
    try:
        url = f"{KLINES_URL}?symbol={symbol}&interval={interval}&limit={limit}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if not isinstance(data, list):
            # Binance error like {"code":-1121,"msg":"Invalid symbol."}
            print(f"[BINANCE] Error for {symbol} {interval}: {data}")
            return []
        closes = [float(c[4]) for c in data if isinstance(c, list) and len(c) >= 5]
        return closes
    except Exception as e:
        print(f"[ERR] get_klines({symbol},{interval}) -> {e}")
        return []

def rsi(values, period=14):
    """
    Pandas RSI; returns last RSI or None if not enough candles.
    """
    try:
        if len(values) < period + 1:
            return None
        series = pd.Series(values, dtype=float)
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_series = 100 - (100 / (1 + rs))
        last = rsi_series.iloc[-1]
        if pd.isna(last):
            return None
        return float(last)
    except Exception as e:
        print(f"[ERR] rsi() -> {e}")
        return None

def generate_signal(symbol, interval):
    """
    Create ultra-filtered signal with validity. Returns message string or None.
    """
    try:
        closes = get_klines(symbol, interval)
        if len(closes) < 20:
            return None

        last_close = closes[-1]
        rsi_val = rsi(closes)
        if rsi_val is None:
            return None

        valid_min = int(settings.get("signal_validity_min", 15))
        if rsi_val < float(settings.get("rsi_buy", 20)):
            return f"🟢 STRONG BUY {symbol} [{interval}] | RSI {rsi_val:.2f} | Price {last_close:.6f} | Valid {valid_min}m"
        if rsi_val > float(settings.get("rsi_sell", 80)):
            return f"🔴 STRONG SELL {symbol} [{interval}] | RSI {rsi_val:.2f} | Price {last_close:.6f} | Valid {valid_min}m"
        return None
    except Exception as e:
        print(f"[ERR] generate_signal({symbol},{interval}) -> {e}")
        return None

# ================= SIGNAL MANAGEMENT =================
def save_all_states():
    _safe_write_json(USER_COINS_FILE, coins)
    _safe_write_json(SETTINGS_FILE, settings)
    _safe_write_json(LAST_SIGNAL_FILE, last_signals)
    _safe_write_json(MUTED_COINS_FILE, muted_coins)
    _safe_write_json(COIN_INTERVALS_FILE, coin_intervals)

def send_signal_if_new(coin, interval, sig):
    if not sig:
        return
    if coin in muted_coins:
        return
    key = f"{coin}_{interval}"
    now_ts = time.time()
    validity = int(settings.get("signal_validity_min", 15)) * 60
    last_ts = last_signals.get(key, 0)
    if now_ts - last_ts >= validity:
        try:
            bot.send_message(CHAT_ID, f"⚡ {sig}")
            last_signals[key] = now_ts
            _safe_write_json(LAST_SIGNAL_FILE, last_signals)
        except Exception as e:
            print(f"[ERR] send_signal_if_new -> {e}")

def signal_scanner_loop():
    """
    Runs forever in a background thread. Scans coins at chosen intervals.
    """
    print("[SCAN] signal scanner started")
    while True:
        try:
            if auto_signals_enabled:
                # Only real coin symbols should be in `coins`
                active = coins if (isinstance(coins, list) and len(coins) > 0) else ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
                for c in active:
                    if not isinstance(c, str) or len(c) < 6:
                        # Guard against accidental non-symbol values
                        continue
                    intervals = coin_intervals.get(c, ["1m", "5m", "15m", "1h", "4h", "1d"])
                    for iv in intervals:
                        sig = generate_signal(c, iv)
                        if sig:
                            send_signal_if_new(c, iv, sig)
            time.sleep(60)
        except Exception as e:
            print(f"[ERR] signal_scanner_loop -> {e}")
            time.sleep(5)

# ================= BOT COMMANDS =================
@bot.message_handler(commands=["start"])
def start(msg):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📊 My Coins", "➕ Add Coin")
    kb.add("➖ Remove Coin", "🤖 Auto Signals")
    kb.add("🛑 Stop Signals", "🔄 Reset Settings")
    kb.add("⚙️ Settings", "📡 Signals")
    kb.add("🔍 Preview Signal", "🔇 Mute Coin", "🔔 Unmute Coin")
    kb.add("⏱ Coin Intervals")
    bot.send_message(msg.chat.id, "🤖 Welcome! Choose an option:", reply_markup=kb)

# --- My Coins ---
@bot.message_handler(func=lambda m: m.text == "📊 My Coins")
def my_coins(msg):
    if not coins:
        bot.send_message(msg.chat.id, "⚠️ No coins saved. Use ➕ Add Coin.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
    bot.send_message(msg.chat.id, "📊 Select a coin:", reply_markup=kb)

# --- Add Coin ---
@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def add_coin(msg):
    bot.send_message(msg.chat.id, "Type coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, process_add_coin)

def process_add_coin(msg):
    coin = (msg.text or "").upper().strip()
    if not coin or len(coin) < 6:
        bot.send_message(msg.chat.id, "⚠️ Invalid symbol.")
        return
    if coin not in coins:
        coins.append(coin)
        _safe_write_json(USER_COINS_FILE, coins)
        bot.send_message(msg.chat.id, f"✅ {coin} added.")
    else:
        bot.send_message(msg.chat.id, f"{coin} already exists.")

# --- Remove Coin ---
@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def remove_coin(msg):
    if not coins:
        bot.send_message(msg.chat.id, "⚠️ No coins to remove.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
    bot.send_message(msg.chat.id, "Select coin to remove:", reply_markup=kb)
    bot.register_next_step_handler(msg, process_remove_coin)

def process_remove_coin(msg):
    coin = (msg.text or "").upper().strip()
    if coin in coins:
        coins.remove(coin)
        _safe_write_json(USER_COINS_FILE, coins)
        bot.send_message(msg.chat.id, f"❌ {coin} removed.")
    else:
        bot.send_message(msg.chat.id, "Coin not found.")

# --- Auto Signals Toggle ---
@bot.message_handler(func=lambda m: m.text == "🤖 Auto Signals")
def enable_signals(msg):
    global auto_signals_enabled
    auto_signals_enabled = True
    bot.send_message(msg.chat.id, "✅ Auto signals ENABLED.")

@bot.message_handler(func=lambda m: m.text == "🛑 Stop Signals")
def stop_signals(msg):
    global auto_signals_enabled
    auto_signals_enabled = False
    bot.send_message(msg.chat.id, "⛔ Auto signals DISABLED.")

# --- Reset Settings ---
@bot.message_handler(func=lambda m: m.text == "🔄 Reset Settings")
def reset_settings(msg):
    global coins, last_signals, muted_coins, coin_intervals
    coins = []
    last_signals = {}
    muted_coins = []
    coin_intervals = {}
    save_all_states()
    bot.send_message(msg.chat.id, "🔄 All settings reset.")

# --- Manual Signals ---
@bot.message_handler(func=lambda m: m.text == "📡 Signals")
def signals(msg):
    active = coins if (isinstance(coins, list) and len(coins) > 0) else ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    out = []
    for c in active:
        if not isinstance(c, str) or len(c) < 6:
            continue
        intervals = coin_intervals.get(c, ["1m", "5m", "15m", "1h", "4h", "1d"])
        for iv in intervals:
            sig = generate_signal(c, iv)
            if sig:
                out.append(sig)
    if not out:
        bot.send_message(msg.chat.id, "⚡ No strong signals right now.")
    else:
        bot.send_message(msg.chat.id, "📡 Ultra-Filtered Signals:\n\n" + "\n".join(out))

# --- Preview Signal ---
@bot.message_handler(func=lambda m: m.text == "🔍 Preview Signal")
def preview_signal(msg):
    if not coins:
        bot.send_message(msg.chat.id, "⚠️ No coins available.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
    bot.send_message(msg.chat.id, "Select coin to preview:", reply_markup=kb)
    bot.register_next_step_handler(msg, process_preview_signal)

def process_preview_signal(msg):
    coin = (msg.text or "").upper().strip()
    if coin not in coins:
        bot.send_message(msg.chat.id, "⚠️ Coin not found.")
        return
    intervals = coin_intervals.get(coin, ["1m", "5m", "15m", "1h", "4h", "1d"])
    out = []
    for iv in intervals:
        sig = generate_signal(coin, iv)
        if sig:
            out.append(sig)
    if not out:
        bot.send_message(msg.chat.id, f"⚡ No strong signals for {coin} now.")
    else:
        bot.send_message(msg.chat.id, f"📊 Preview Signals for {coin}:\n" + "\n".join(out))

# --- Mute / Unmute ---
@bot.message_handler(func=lambda m: m.text == "🔇 Mute Coin")
def mute_coin(msg):
    if not coins:
        bot.send_message(msg.chat.id, "⚠️ No coins available.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
    bot.send_message(msg.chat.id, "Select coin to mute:", reply_markup=kb)
    bot.register_next_step_handler(msg, process_mute_coin)

def process_mute_coin(msg):
    coin = (msg.text or "").upper().strip()
    if coin and coin not in muted_coins:
        muted_coins.append(coin)
        _safe_write_json(MUTED_COINS_FILE, muted_coins)
        bot.send_message(msg.chat.id, f"🔇 {coin} muted.")
    else:
        bot.send_message(msg.chat.id, "⚠️ Invalid or already muted.")

@bot.message_handler(func=lambda m: m.text == "🔔 Unmute Coin")
def unmute_coin(msg):
    if not muted_coins:
        bot.send_message(msg.chat.id, "⚠️ No muted coins.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in muted_coins:
        kb.add(c)
    bot.send_message(msg.chat.id, "Select coin to unmute:", reply_markup=kb)
    bot.register_next_step_handler(msg, process_unmute_coin)

def process_unmute_coin(msg):
    coin = (msg.text or "").upper().strip()
    if coin in muted_coins:
        muted_coins.remove(coin)
        _safe_write_json(MUTED_COINS_FILE, muted_coins)
        bot.send_message(msg.chat.id, f"🔔 {coin} unmuted.")
    else:
        bot.send_message(msg.chat.id, "⚠️ Coin not muted.")

# --- Coin Intervals ---
@bot.message_handler(func=lambda m: m.text == "⏱ Coin Intervals")
def coin_intervals_menu(msg):
    if not coins:
        bot.send_message(msg.chat.id, "⚠️ No coins available.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
    bot.send_message(msg.chat.id, "Select coin to set intervals:", reply_markup=kb)
    bot.register_next_step_handler(msg, process_coin_intervals_select)

def process_coin_intervals_select(msg):
    coin = (msg.text or "").upper().strip()
    if coin not in coins:
        bot.send_message(msg.chat.id, "⚠️ Coin not found.")
        return
    bot.send_message(msg.chat.id, "Send intervals separated by commas (e.g., 1m,5m,15m,1h,4h,1d):")
    bot.register_next_step_handler(msg, lambda m: save_coin_intervals(coin, m.text))

def save_coin_intervals(coin, text):
    parts = [x.strip() for x in (text or "").split(",") if x.strip()]
    if not parts:
        bot.send_message(CHAT_ID, "⚠️ Invalid input. No changes made.")
        return
    # very light validation: must end with m/h/d and start with number
    valid = []
    for p in parts:
        p_low = p.lower()
        if (p_low.endswith("m") or p_low.endswith("h") or p_low.endswith("d")) and p_low[:-1].isdigit():
            valid.append(p_low)
    if not valid:
        bot.send_message(CHAT_ID, "⚠️ Invalid intervals. No changes made.")
        return
    coin_intervals[coin] = valid
    _safe_write_json(COIN_INTERVALS_FILE, coin_intervals)
    bot.send_message(CHAT_ID, f"✅ Intervals for {coin} updated: {', '.join(valid)}")

# --- Settings ---
@bot.message_handler(func=lambda m: m.text == "⚙️ Settings")
def settings_menu(msg):
    bot.send_message(
        msg.chat.id,
        f"Current settings:\n"
        f"RSI Buy Threshold: {settings.get('rsi_buy')}\n"
        f"RSI Sell Threshold: {settings.get('rsi_sell')}\n"
        f"Signal Validity (min): {settings.get('signal_validity_min')}\n\n"
        f"Send as: buy,sell,validity (e.g., 18,82,30)"
    )
    bot.register_next_step_handler(msg, update_settings)

def update_settings(msg):
    try:
        parts = [int(x.strip()) for x in (msg.text or "").split(",")]
        if len(parts) != 3:
            raise ValueError("Need three integers.")
        settings["rsi_buy"] = parts[0]
        settings["rsi_sell"] = parts[1]
        settings["signal_validity_min"] = parts[2]
        _safe_write_json(SETTINGS_FILE, settings)
        bot.send_message(msg.chat.id, "✅ Settings updated.")
    except Exception:
        bot.send_message(msg.chat.id, "⚠️ Invalid format. Send as: buy,sell,validity")

# ================= FLASK (KEEP-ALIVE FOR RENDER) =================
@app.route("/")
def index():
    return "OK", 200

@app.route("/health")
def health():
    return "healthy", 200

# ================= STARTUP HELPERS =================
def notify_bot_live():
    try:
        bot.send_message(CHAT_ID, "✅ Bot deployed and running!")
    except Exception as e:
        print(f"[WARN] Failed to send startup message: {e}")

def run_bot_polling():
    # VERY IMPORTANT: Remove webhook before polling
    try:
        bot.remove_webhook()
        print("[BOT] webhook removed; starting polling...")
    except Exception as e:
        print(f"[BOT] remove_webhook error: {e}")
    # Start polling (blocks) – but we run it in its own thread
    bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)

def run_signal_scanner():
    signal_scanner_loop()

# ================= MAIN =================
if __name__ == "__main__":
    # Threads: bot + scanner
    t1 = threading.Thread(target=run_bot_polling, daemon=True)
    t2 = threading.Thread(target=run_signal_scanner, daemon=True)
    t1.start()
    t2.start()

    # Notify once
    notify_bot_live()

    # Flask keep-alive (Render needs open port)
    port = int(os.environ.get("PORT", "10000"))
    print(f"[FLASK] starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)


