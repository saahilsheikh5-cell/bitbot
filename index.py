import os
import json
import time
import threading
import requests
import telebot
from telebot import types

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q")
bot = telebot.TeleBot(BOT_TOKEN)

# File paths
USER_COINS_FILE = "user_coins.json"
MUTED_COINS_FILE = "muted_coins.json"
LAST_SIGNALS_FILE = "last_signals.json"
SETTINGS_FILE = "settings.json"
COIN_INTERVALS_FILE = "coin_intervals.json"

# ================= JSON HELPERS =================
def load_json(file, default):
    try:
        with open(file, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

user_coins = load_json(USER_COINS_FILE, {})
muted_coins = load_json(MUTED_COINS_FILE, {})
last_signals = load_json(LAST_SIGNALS_FILE, {})
settings = load_json(SETTINGS_FILE, {})
coin_intervals = load_json(COIN_INTERVALS_FILE, {})

# ================= SIGNAL GENERATOR =================
def generate_signal(symbol, interval):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=50"
        data = requests.get(url, timeout=10).json()
        closes = [float(x[4]) for x in data]

        sma_short = sum(closes[-7:]) / 7
        sma_long = sum(closes[-25:]) / 25

        if sma_short > sma_long:
            return "BUY"
        elif sma_short < sma_long:
            return "SELL"
        else:
            return "NEUTRAL"
    except Exception as e:
        print(f"Error generating signal for {symbol}: {e}")
        return "ERROR"

# ================= BOT COMMANDS =================
@bot.message_handler(commands=["start"])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📈 My Coins", "➕ Add/Remove Coin")
    markup.row("🤖 Auto Signals", "🛑 Stop Signals")
    markup.row("🔄 Reset Settings", "📊 Signals")
    markup.row("👀 Preview Signal", "🔕 Mute/Unmute Coin")
    markup.row("⏱ Coin Intervals", "⚙ Settings")
    bot.send_message(message.chat.id, "🚀 Welcome to BitBobb Bot!", reply_markup=markup)

# My Coins
@bot.message_handler(func=lambda m: m.text == "📈 My Coins")
def my_coins(message):
    coins = user_coins.get(str(message.chat.id), [])
    if not coins:
        bot.send_message(message.chat.id, "❌ You have no coins added.")
    else:
        bot.send_message(message.chat.id, f"📌 Your coins: {', '.join(coins)}")

# Add/Remove Coin
@bot.message_handler(func=lambda m: m.text == "➕ Add/Remove Coin")
def add_remove_coin(message):
    bot.send_message(message.chat.id, "✍ Send a coin symbol (e.g. BTCUSDT) to add/remove.")

@bot.message_handler(func=lambda m: m.text and m.text.upper().endswith("USDT"))
def add_remove_coin_input(message):
    coin = message.text.upper()
    uid = str(message.chat.id)
    if uid not in user_coins:
        user_coins[uid] = []
    if coin in user_coins[uid]:
        user_coins[uid].remove(coin)
        bot.send_message(message.chat.id, f"❌ Removed {coin}")
    else:
        user_coins[uid].append(coin)
        bot.send_message(message.chat.id, f"✅ Added {coin}")
    save_json(USER_COINS_FILE, user_coins)

# Auto Signals
@bot.message_handler(func=lambda m: m.text == "🤖 Auto Signals")
def auto_signals(message):
    settings[str(message.chat.id)] = {"auto": True}
    save_json(SETTINGS_FILE, settings)
    bot.send_message(message.chat.id, "✅ Auto signals enabled.")

# Stop Signals
@bot.message_handler(func=lambda m: m.text == "🛑 Stop Signals")
def stop_signals(message):
    settings[str(message.chat.id)] = {"auto": False}
    save_json(SETTINGS_FILE, settings)
    bot.send_message(message.chat.id, "🛑 Auto signals stopped.")

# Reset Settings
@bot.message_handler(func=lambda m: m.text == "🔄 Reset Settings")
def reset_settings(message):
    uid = str(message.chat.id)
    user_coins.pop(uid, None)
    muted_coins.pop(uid, None)
    settings.pop(uid, None)
    coin_intervals.pop(uid, None)
    save_json(USER_COINS_FILE, user_coins)
    save_json(MUTED_COINS_FILE, muted_coins)
    save_json(SETTINGS_FILE, settings)
    save_json(COIN_INTERVALS_FILE, coin_intervals)
    bot.send_message(message.chat.id, "🔄 Settings reset.")

# Signals
@bot.message_handler(func=lambda m: m.text == "📊 Signals")
def signals(message):
    uid = str(message.chat.id)
    coins = user_coins.get(uid, [])
    if not coins:
        bot.send_message(message.chat.id, "❌ No coins added.")
        return
    text = ""
    for coin in coins:
        for interval in coin_intervals.get(uid, ["1m", "5m", "15m", "1h", "4h", "1d"]):
            sig = generate_signal(coin, interval)
            text += f"{coin} ({interval}) → {sig}\n"
    bot.send_message(message.chat.id, text or "⚠ No signals.")

# Preview Signal
@bot.message_handler(func=lambda m: m.text == "👀 Preview Signal")
def preview_signal(message):
    bot.send_message(message.chat.id, "✍ Send a coin (e.g. BTCUSDT) to preview.")

# Mute/Unmute Coin
@bot.message_handler(func=lambda m: m.text == "🔕 Mute/Unmute Coin")
def mute_unmute(message):
    bot.send_message(message.chat.id, "✍ Send a coin to mute/unmute.")

# Coin Intervals
@bot.message_handler(func=lambda m: m.text == "⏱ Coin Intervals")
def coin_intervals_cmd(message):
    bot.send_message(message.chat.id, "✍ Send intervals for your coins (comma-separated, e.g. 1m,5m,15m).")

# Settings
@bot.message_handler(func=lambda m: m.text == "⚙ Settings")
def settings_cmd(message):
    bot.send_message(message.chat.id, "⚙ Settings menu coming soon.")

# ================= BACKGROUND SIGNAL SCANNER =================
def signal_scanner():
    while True:
        for uid, coins in user_coins.items():
            if not settings.get(uid, {}).get("auto"):
                continue
            for coin in coins:
                if not coin.endswith("USDT"):  # ✅ only valid tickers
                    continue
                for interval in coin_intervals.get(uid, ["1m","5m","15m","1h","4h","1d"]):
                    sig = generate_signal(coin, interval)
                    if sig not in ["ERROR", "NEUTRAL"]:
                        bot.send_message(int(uid), f"📢 {coin} ({interval}) → {sig}")
        time.sleep(60)

# ================= START BOT =================
if __name__ == "__main__":
    threading.Thread(target=signal_scanner, daemon=True).start()
    print("🚀 Bot is running with polling...")
    bot.infinity_polling()


