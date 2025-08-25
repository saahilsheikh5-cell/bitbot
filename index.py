import os
import telebot
import requests
import pandas as pd
import threading
import time
import json
from telebot import types
from flask import Flask, request
import numpy as np

# ================= CONFIG =================
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
CHAT_ID = 1263295916
KLINES_URL = "https://api.binance.com/api/v3/klines"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ================= STORAGE =================
USER_COINS_FILE = "user_coins.json"
SETTINGS_FILE = "settings.json"
LAST_SIGNAL_FILE = "last_signals.json"
MUTED_COINS_FILE = "muted_coins.json"
COIN_INTERVALS_FILE = "coin_intervals.json"

def load_json(file, default):
    if not os.path.exists(file):
        return default
    with open(file,"r") as f:
        return json.load(f)

def save_json(file,data):
    with open(file,"w") as f:
        json.dump(data,f)

coins = load_json(USER_COINS_FILE,[])
settings = load_json(SETTINGS_FILE,{"rsi_buy":20,"rsi_sell":80,"signal_validity_min":15})
last_signals = load_json(LAST_SIGNAL_FILE,{})
muted_coins = load_json(MUTED_COINS_FILE,[])
coin_intervals = load_json(COIN_INTERVALS_FILE,{})
tracked_coins = {}  # For "Track Particular Coin"

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

def generate_signal(symbol, interval):
    try:
        closes = get_klines(symbol, interval)
        if len(closes)<20: return None
        last_close = closes[-1]
        rsi_val = rsi(closes)[-1]
        if rsi_val<settings["rsi_buy"]:
            return f"🟢 STRONG BUY {symbol} | RSI {rsi_val:.2f} | Price {last_close} | Valid {settings['signal_validity_min']}min"
        elif rsi_val>settings["rsi_sell"]:
            return f"🔴 STRONG SELL {symbol} | RSI {rsi_val:.2f} | Price {last_close} | Valid {settings['signal_validity_min']}min"
        return None
    except Exception as e:
        print(f"Error generating signal for {symbol}: {e}")
        return None

# ================= SIGNAL MANAGEMENT =================
auto_signals_enabled = True

def send_signal_if_new(coin, interval, sig):
    global last_signals, muted_coins
    if coin in muted_coins: return
    key = f"{coin}_{interval}"
    now_ts = time.time()
    if key not in last_signals or now_ts - last_signals[key] > settings["signal_validity_min"]*60:
        bot.send_message(CHAT_ID,f"⚡ {sig}")
        last_signals[key] = now_ts
        save_json(LAST_SIGNAL_FILE,last_signals)

def signal_scanner():
    while True:
        if auto_signals_enabled:
            active_coins = coins if coins else []  # Your coins
            top_coins = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","ADAUSDT"]  # You can expand top 100 here
            all_coins = list(set(active_coins + top_coins))
            for c in all_coins:
                intervals = coin_intervals.get(c, ["1m","5m","15m","1h","4h","1d"])
                for interval in intervals:
                    sig = generate_signal(c, interval)
                    if sig: send_signal_if_new(c, interval, sig)
        # Tracked coins
        for coin, intervals in tracked_coins.items():
            for interval in intervals:
                sig = generate_signal(coin, interval)
                if sig:
                    bot.send_message(CHAT_ID, f"🎯 Tracked Coin Signal:\n{sig}")
        time.sleep(60)

threading.Thread(target=signal_scanner, daemon=True).start()

# ================= BOT COMMANDS =================
def main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 My Coins","🌐 All Coins")
    markup.add("🎯 Track Coin","➕ Add Coin","➖ Remove Coin")
    markup.add("🤖 Auto Signals","🛑 Stop Signals")
    markup.add("🔄 Reset Settings","⚙️ Settings")
    markup.add("📡 Signals","🔍 Preview Signal")
    markup.add("🔇 Mute Coin","🔔 Unmute Coin")
    markup.add("⏱ Coin Intervals")
    bot.send_message(chat_id,"🤖 Welcome! Choose an option:",reply_markup=markup)

@bot.message_handler(commands=["start"])
def start(msg):
    main_menu(msg.chat.id)

# --- My Coins ---
@bot.message_handler(func=lambda m: m.text=="📊 My Coins")
def my_coins_menu(msg):
    if not coins:
        bot.send_message(msg.chat.id,"⚠️ No coins saved. Use ➕ Add Coin.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: markup.add(c)
    markup.add("⬅️ Back")
    bot.send_message(msg.chat.id,"📊 Select a coin:",reply_markup=markup)
    bot.register_next_step_handler(msg, my_coins_action)

def my_coins_action(msg):
    if msg.text=="⬅️ Back":
        main_menu(msg.chat.id)
        return
    coin = msg.text.upper()
    if coin not in coins:
        bot.send_message(msg.chat.id,"⚠️ Coin not found.")
        main_menu(msg.chat.id)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    intervals = ["1m","5m","15m","1h","4h","1d"]
    for i in intervals: markup.add(i)
    markup.add("⬅️ Back")
    bot.send_message(msg.chat.id,f"⏱ Select timeframe for {coin}:",reply_markup=markup)
    bot.register_next_step_handler(msg, lambda m: show_coin_signal(coin, m))

def show_coin_signal(coin, msg):
    if msg.text=="⬅️ Back":
        my_coins_menu(msg)
        return
    interval = msg.text
    sig = generate_signal(coin, interval)
    if not sig:
        bot.send_message(msg.chat.id,f"⚡ No strong signals for {coin} on {interval}")
    else:
        bot.send_message(msg.chat.id,sig)
    my_coins_menu(msg)

# --- Add/Remove Coin ---
@bot.message_handler(func=lambda m: m.text=="➕ Add Coin")
def add_coin(msg):
    bot.send_message(msg.chat.id,"Type coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, process_add_coin)
def process_add_coin(msg):
    coin = msg.text.upper()
    if coin not in coins:
        coins.append(coin)
        save_json(USER_COINS_FILE,coins)
        bot.send_message(msg.chat.id,f"✅ {coin} added.")
    else:
        bot.send_message(msg.chat.id,f"{coin} already exists.")
    main_menu(msg.chat.id)

@bot.message_handler(func=lambda m: m.text=="➖ Remove Coin")
def remove_coin(msg):
    if not coins:
        bot.send_message(msg.chat.id,"⚠️ No coins to remove.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: markup.add(c)
    markup.add("⬅️ Back")
    bot.send_message(msg.chat.id,"Select coin to remove:",reply_markup=markup)
    bot.register_next_step_handler(msg,process_remove_coin)
def process_remove_coin(msg):
    if msg.text=="⬅️ Back":
        main_menu(msg.chat.id)
        return
    coin = msg.text.upper()
    if coin in coins:
        coins.remove(coin)
        save_json(USER_COINS_FILE,coins)
        bot.send_message(msg.chat.id,f"❌ {coin} removed.")
    main_menu(msg.chat.id)

# --- Auto Signals ---
@bot.message_handler(func=lambda m: m.text=="🤖 Auto Signals")
def enable_signals(msg):
    global auto_signals_enabled
    auto_signals_enabled=True
    bot.send_message(msg.chat.id,"✅ Auto signals ENABLED.")
@bot.message_handler(func=lambda m: m.text=="🛑 Stop Signals")
def stop_signals(msg):
    global auto_signals_enabled
    auto_signals_enabled=False
    bot.send_message(msg.chat.id,"⛔ Auto signals DISABLED.")

# --- Reset Settings ---
@bot.message_handler(func=lambda m: m.text=="🔄 Reset Settings")
def reset_settings(msg):
    global coins, last_signals, muted_coins, coin_intervals, tracked_coins
    coins=[]
    last_signals={}
    muted_coins=[]
    coin_intervals={}
    tracked_coins={}
    save_json(USER_COINS_FILE,coins)
    save_json(LAST_SIGNAL_FILE,last_signals)
    save_json(MUTED_COINS_FILE,muted_coins)
    save_json(COIN_INTERVALS_FILE,coin_intervals)
    bot.send_message(msg.chat.id,"🔄 All settings reset.")
    main_menu(msg.chat.id)

# --- Track Particular Coin ---
@bot.message_handler(func=lambda m: m.text=="🎯 Track Coin")
def track_coin_menu(msg):
    bot.send_message(msg.chat.id,"Type the coin symbol you want to track (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, process_track_coin)

def process_track_coin(msg):
    coin = msg.text.upper()
    bot.send_message(msg.chat.id,"Send timeframes separated by commas (e.g., 1m,5m,15m,1h,4h,1d):")
    bot.register_next_step_handler(msg, lambda m: save_tracked_coin(coin, m.text, msg.chat.id))

def save_tracked_coin(coin, text, chat_id):
    intervals = [x.strip() for x in text.split(",") if x.strip()]
    if intervals:
        tracked_coins[coin] = intervals
        bot.send_message(chat_id,f"✅ Now tracking {coin} on intervals: {', '.join(intervals)}")
    else:
        bot.send_message(chat_id,"⚠️ Invalid input. No coin tracked.")

# ================= SETTINGS =================
@bot.message_handler(func=lambda m: m.text=="⚙️ Settings")
def settings_menu(msg):
    bot.send_message(msg.chat.id,f"Current settings:\nRSI Buy: {settings['rsi_buy']}\nRSI Sell: {settings['rsi_sell']}\nSignal Validity (min): {settings['signal_validity_min']}\nSend as: buy,sell,validity (e.g., 20,80,15)")
    bot.register_next_step_handler(msg,update_settings)
def update_settings(msg):
    try:
        parts=[int(x.strip()) for x in msg.text.split(",")]
        settings["rsi_buy"]=parts[0]
        settings["rsi_sell"]=parts[1]
        settings["signal_validity_min"]=parts[2]
        save_json(SETTINGS_FILE,settings)
        bot.send_message(msg.chat.id,"✅ Settings updated.")
    except:
        bot.send_message(msg.chat.id,"⚠️ Invalid format. Send as: buy,sell,validity")
    main_menu(msg.chat.id)

# ================= FLASK WEBHOOK (optional) =================
@app.route("/")
def index():
    return "Bot running!",200

# --- Notify Admin ---
def notify_bot_live():
    try:
        bot.send_message(CHAT_ID, "✅ Bot deployed and running!")
    except Exception as e:
        print(f"Failed to send startup message: {e}")

if __name__=="__main__":
    notify_bot_live()
    bot.remove_webhook()
    bot.infinity_polling()


