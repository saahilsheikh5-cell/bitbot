import os
import telebot
import requests
import threading
import time
from flask import Flask, request
from telebot import types

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-app.onrender.com/" + BOT_TOKEN)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# === DATA STORAGE ===
my_coins = []   # Stores user’s added coins (e.g. BTCUSDT, ETHUSDT)
tracking = {}   # For signal tracking {"BTCUSDT": True}

# === UTILS ===
def normalize_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol

def get_binance_data(symbol: str, interval: str = "1m"):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=100"
        res = requests.get(url, timeout=10)
        data = res.json()
        return data
    except Exception as e:
        print(f"[BINANCE] Error for {symbol} {interval}: {e}")
        return None

def technical_analysis(symbol: str, interval: str):
    data = get_binance_data(symbol, interval)
    if not data or isinstance(data, dict):
        return f"⚠️ No data for {symbol} {interval}"

    closes = [float(x[4]) for x in data]

    # RSI
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            losses.append(abs(diff))
            gains.append(0)
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14 if sum(losses[-14:]) > 0 else 1
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # EMA (12 & 26)
    def ema(prices, period):
        k = 2 / (period + 1)
        ema_values = [sum(prices[:period]) / period]
        for price in prices[period:]:
            ema_values.append((price * k) + ema_values[-1] * (1 - k))
        return ema_values

    ema12 = ema(closes, 12)[-1]
    ema26 = ema(closes, 26)[-1]

    # MACD
    macd = ema12 - ema26

    # Decision
    decision = "NEUTRAL"
    if rsi < 30 and macd > 0:
        decision = "BUY"
    elif rsi > 70 and macd < 0:
        decision = "SELL"

    return f"📊 {symbol} {interval}\nRSI: {rsi:.2f}\nMACD: {macd:.4f}\nEMA12: {ema12:.2f}\nEMA26: {ema26:.2f}\n➡️ Signal: {decision}"

# === BOT MENUS ===
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ ADD COIN", "📊 MY COINS")
    markup.add("📈 TOP MOVERS", "📡 SIGNALS")
    markup.add("⏹ STOP", "🔄 RESET")
    markup.add("⚙ SETTINGS", "👁 PREVIEW")
    return markup

@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "👋 Welcome! Choose an option:", reply_markup=main_menu())

# === ADD COIN ===
@bot.message_handler(func=lambda m: m.text == "➕ ADD COIN")
def ask_coin(message):
    msg = bot.send_message(message.chat.id, "Enter coin name (e.g. BTC, ETH):")
    bot.register_next_step_handler(msg, save_coin)

def save_coin(message):
    try:
        symbol = normalize_symbol(message.text)
        if symbol not in my_coins:
            my_coins.append(symbol)
            bot.reply_to(message, f"✅ Added {symbol}")
        else:
            bot.reply_to(message, f"⚠️ {symbol} already in list")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

# === MY COINS ===
@bot.message_handler(func=lambda m: m.text == "📊 MY COINS")
def show_my_coins(message):
    if not my_coins:
        bot.send_message(message.chat.id, "⚠️ No coins added.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for coin in my_coins:
        markup.add(coin)
    markup.add("🔙 BACK")
    bot.send_message(message.chat.id, "📊 Select coin:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text in my_coins)
def choose_timeframe(message):
    coin = message.text
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m", "5m", "15m", "1h", "4h", "1d"]:
        markup.add(f"{coin} {tf}")
    markup.add("🔙 BACK")
    bot.send_message(message.chat.id, f"⏳ Select timeframe for {coin}:", reply_markup=markup)

@bot.message_handler(func=lambda m: any(tf in m.text for tf in ["1m","5m","15m","1h","4h","1d"]))
def show_analysis(message):
    try:
        parts = message.text.split()
        coin, tf = parts[0], parts[1]
        symbol = normalize_symbol(coin.replace("USDT", ""))
        result = technical_analysis(symbol, tf)
        bot.send_message(message.chat.id, result)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {e}")

# === SIGNALS ===
@bot.message_handler(func=lambda m: m.text == "📡 SIGNALS")
def ask_track_coin(message):
    msg = bot.send_message(message.chat.id, "Enter coin to track (e.g. BTC):")
    bot.register_next_step_handler(msg, start_tracking)

def start_tracking(message):
    symbol = normalize_symbol(message.text)
    tracking[symbol] = True
    bot.send_message(message.chat.id, f"📡 Tracking {symbol}... you’ll get alerts!")

# === STOP ===
@bot.message_handler(func=lambda m: m.text == "⏹ STOP")
def stop_all(message):
    tracking.clear()
    bot.send_message(message.chat.id, "⏹ Stopped all signals.")

# === RESET ===
@bot.message_handler(func=lambda m: m.text == "🔄 RESET")
def reset(message):
    my_coins.clear()
    tracking.clear()
    bot.send_message(message.chat.id, "🔄 Reset done.")

# === BACK BUTTON ===
@bot.message_handler(func=lambda m: m.text == "🔙 BACK")
def back_to_menu(message):
    bot.send_message(message.chat.id, "🔙 Back to menu", reply_markup=main_menu())

# === BACKGROUND SIGNAL CHECKER ===
def signal_watcher():
    while True:
        try:
            for symbol in list(tracking.keys()):
                if tracking[symbol]:
                    result = technical_analysis(symbol, "1m")
                    if "BUY" in result or "SELL" in result:
                        bot.send_message(chat_id=list(bot.get_updates()[-1].message.chat.id), text=f"🚨 Signal Alert\n{result}")
            time.sleep(60)
        except Exception as e:
            print("[SIGNAL THREAD]", e)
            time.sleep(10)

threading.Thread(target=signal_watcher, daemon=True).start()

# === FLASK WEBHOOK ===
@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    update = request.stream.read().decode("utf-8")
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK", 200

@app.route('/')
def index():
    return "Bot running!"

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))






