import os
import telebot
import requests
import pandas as pd
import numpy as np
import threading
import time
import json
from telebot import types

# ================= CONFIG =================
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
CHAT_ID = 1263295916
KLINES_URL = "https://api.binance.com/api/v3/klines"
TOP_COINS_URL = "https://api.binance.com/api/v3/ticker/24hr"

bot = telebot.TeleBot(BOT_TOKEN)

# ================= STORAGE =================
USER_COINS_FILE = "user_coins.json"
SETTINGS_FILE = "settings.json"
LAST_SIGNAL_FILE = "last_signals.json"
MUTED_COINS_FILE = "muted_coins.json"
COIN_INTERVALS_FILE = "coin_intervals.json"

def load_json(file, default):
    if not os.path.exists(file):
        return default
    with open(file, "r") as f:
        return json.load(f)

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f)

coins = load_json(USER_COINS_FILE, [])
settings = load_json(SETTINGS_FILE, {"rsi_buy": 30, "rsi_sell": 70, "signal_validity_min": 15})
last_signals = load_json(LAST_SIGNAL_FILE, {})
muted_coins = load_json(MUTED_COINS_FILE, [])
coin_intervals = load_json(COIN_INTERVALS_FILE, {})

# ================= TECHNICAL ANALYSIS =================
def get_klines(symbol, interval="15m", limit=100):
    url = f"{KLINES_URL}?symbol={symbol}&interval={interval}&limit={limit}"
    data = requests.get(url, timeout=10).json()
    closes = [float(c[4]) for c in data]
    return closes

def rsi(data, period=14):
    delta = np.diff(data)
    gain = np.maximum(delta,0)
    loss = -np.minimum(delta,0)
    avg_gain = pd.Series(gain).rolling(period).mean()
    avg_loss = pd.Series(loss).rolling(period).mean()
    rs = avg_gain/avg_loss
    return 100-(100/(1+rs))

def ema(data, period=50):
    return pd.Series(data).ewm(span=period).mean().values

def macd(data, fast=12, slow=26, signal=9):
    fast_ema = pd.Series(data).ewm(span=fast).mean()
    slow_ema = pd.Series(data).ewm(span=slow).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal).mean()
    return macd_line.values, signal_line.values

def generate_signal(symbol, interval):
    try:
        closes = get_klines(symbol, interval)
        if len(closes) < 30: return None
        last_close = closes[-1]
        rsi_val = rsi(closes)[-1]
        ema50 = ema(closes)[-1]
        macd_val, macd_signal = macd(closes)
        macd_val = macd_val[-1]
        macd_signal = macd_signal[-1]

        if rsi_val < settings["rsi_buy"] and macd_val > macd_signal and last_close > ema50:
            return f"🟢 ULTRA BUY {symbol} | RSI {rsi_val:.2f} | MACD {macd_val:.4f} | Price {last_close}"
        elif rsi_val > settings["rsi_sell"] and macd_val < macd_signal and last_close < ema50:
            return f"🔴 ULTRA SELL {symbol} | RSI {rsi_val:.2f} | MACD {macd_val:.4f} | Price {last_close}"
        return None
    except Exception as e:
        print(f"Error generating signal for {symbol}: {e}")
        return None

# ================= SIGNAL MANAGEMENT =================
auto_signals_enabled = True
tracked_coin = None

def send_signal_if_new(coin, interval, sig):
    global last_signals, muted_coins
    if coin in muted_coins: return
    key = f"{coin}_{interval}"
    now_ts = time.time()
    if key not in last_signals or now_ts - last_signals[key] > settings["signal_validity_min"]*60:
        bot.send_message(CHAT_ID, f"⚡ {sig}")
        last_signals[key] = now_ts
        save_json(LAST_SIGNAL_FILE, last_signals)

def get_top_coins(n=50):
    try:
        data = requests.get(TOP_COINS_URL).json()
        data = [d for d in data if "USDT" in d['symbol']]
        sorted_data = sorted(data, key=lambda x: float(x['quoteVolume']), reverse=True)
        return [d['symbol'] for d in sorted_data[:n]]
    except:
        return ["BTCUSDT","ETHUSDT","SOLUSDT"]

def signal_scanner():
    while True:
        if auto_signals_enabled:
            active_coins = coins if coins else get_top_coins(50)
            if tracked_coin:
                active_coins.append(tracked_coin)
            for c in active_coins:
                intervals = coin_intervals.get(c, ["1m","5m","15m","1h"])
                for interval in intervals:
                    sig = generate_signal(c, interval)
                    if sig:
                        send_signal_if_new(c, interval, sig)
        time.sleep(60)

threading.Thread(target=signal_scanner, daemon=True).start()

# ================= MARKUPS =================
def main_menu_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("📈 Top Movers","📡 Signals")
    markup.add("🛑 Stop Signals","🔄 Reset Settings")
    markup.add("⚙️ Signal Settings","🔍 Preview Signal")
    return markup

def back_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("⬅️ Back")
    return markup

# ================= BOT COMMANDS =================
@bot.message_handler(commands=["start"])
def start(msg):
    bot.send_message(msg.chat.id,"🤖 Welcome! Choose an option:", reply_markup=main_menu_markup())

# --- Add Coin ---
@bot.message_handler(func=lambda m: m.text=="➕ Add Coin")
def add_coin(msg):
    bot.send_message(msg.chat.id,"Type coin symbol to add:", reply_markup=back_markup())
    bot.register_next_step_handler(msg, process_add_coin)

def process_add_coin(msg):
    global coins
    if msg.text=="⬅️ Back":
        start(msg)
        return
    coin = msg.text.upper()
    if coin not in coins:
        coins.append(coin)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(msg.chat.id,f"✅ {coin} added.", reply_markup=main_menu_markup())
    else:
        bot.send_message(msg.chat.id,f"{coin} already exists.", reply_markup=main_menu_markup())

# --- My Coins ---
@bot.message_handler(func=lambda m: m.text=="📊 My Coins")
def my_coins_menu(msg):
    if not coins:
        bot.send_message(msg.chat.id,"⚠️ No coins saved.", reply_markup=main_menu_markup())
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: markup.add(c)
    markup.add("⬅️ Back")
    bot.send_message(msg.chat.id,"Select a coin:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_my_coin_selection)

def process_my_coin_selection(msg):
    if msg.text=="⬅️ Back":
        start(msg)
        return
    coin = msg.text.upper()
    if coin not in coins:
        bot.send_message(msg.chat.id,"Coin not found.", reply_markup=main_menu_markup())
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in ["1m","5m","15m","1h","1d"]: markup.add(t)
    markup.add("⬅️ Back")
    bot.send_message(msg.chat.id,f"Select timeframe for {coin}:", reply_markup=markup)
    bot.register_next_step_handler(msg, lambda m: show_coin_analysis(coin, m))

def show_coin_analysis(coin,msg):
    if msg.text=="⬅️ Back":
        my_coins_menu(msg)
        return
    interval = msg.text
    sig = generate_signal(coin,interval)
    if sig:
        bot.send_message(msg.chat.id,f"📊 {coin} {interval} Analysis:\n{sig}", reply_markup=main_menu_markup())
    else:
        bot.send_message(msg.chat.id,f"No strong signals for {coin} in {interval}", reply_markup=main_menu_markup())

# --- STOP SIGNALS ---
@bot.message_handler(func=lambda m: m.text=="🛑 Stop Signals")
def stop_signals_menu(msg):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: markup.add(c)
    markup.add("⬅️ Back")
    bot.send_message(msg.chat.id,"Select coin to stop signals:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_stop_signal)

def process_stop_signal(msg):
    global muted_coins
    if msg.text=="⬅️ Back":
        start(msg)
        return
    coin = msg.text.upper()
    if coin not in muted_coins:
        muted_coins.append(coin)
        save_json(MUTED_COINS_FILE, muted_coins)
    bot.send_message(msg.chat.id,f"⛔ Signals muted for {coin}", reply_markup=main_menu_markup())

# --- RESET SETTINGS ---
@bot.message_handler(func=lambda m: m.text=="🔄 Reset Settings")
def reset_settings(msg):
    global coins, last_signals, muted_coins, coin_intervals
    coins=[]
    last_signals={}
    muted_coins=[]
    coin_intervals={}
    save_json(USER_COINS_FILE, coins)
    save_json(LAST_SIGNAL_FILE, last_signals)
    save_json(MUTED_COINS_FILE, muted_coins)
    save_json(COIN_INTERVALS_FILE, coin_intervals)
    bot.send_message(msg.chat.id,"🔄 All settings reset.", reply_markup=main_menu_markup())

# --- SIGNAL SETTINGS ---
@bot.message_handler(func=lambda m: m.text=="⚙️ Signal Settings")
def signal_settings(msg):
    bot.send_message(msg.chat.id,f"Current RSI Buy: {settings['rsi_buy']}, RSI Sell: {settings['rsi_sell']}\nSend new as: buy,sell")
    bot.register_next_step_handler(msg, update_signal_settings)

def update_signal_settings(msg):
    try:
        parts = [int(x.strip()) for x in msg.text.split(",")]
        settings["rsi_buy"]=parts[0]
        settings["rsi_sell"]=parts[1]
        save_json(SETTINGS_FILE, settings)
        bot.send_message(msg.chat.id,"✅ Signal settings updated.", reply_markup=main_menu_markup())
    except:
        bot.send_message(msg.chat.id,"⚠️ Invalid format. Send as: buy,sell", reply_markup=main_menu_markup())

# --- PREVIEW SIGNAL ---
@bot.message_handler(func=lambda m: m.text=="🔍 Preview Signal")
def preview_signal(msg):
    active_coins = coins if coins else get_top_coins(50)
    signals_list=[]
    for c in active_coins:
        intervals = coin_intervals.get(c, ["1m","5m","15m","1h"])
        for interval in intervals:
            sig = generate_signal(c, interval)
            if sig: signals_list.append(sig)
    if signals_list:
        bot.send_message(msg.chat.id,"📡 Signals Preview:\n\n"+"\n".join(signals_list[:20]), reply_markup=main_menu_markup())
    else:
        bot.send_message(msg.chat.id,"⚡ No signals available.", reply_markup=main_menu_markup())

# ================= START BOT =================
if __name__=="__main__":
    bot.infinity_polling()




