
USER_COINS_FILE = os.path.join(DATA_DIR, "user_coins.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LAST_SIGNAL_FILE = os.path.join(DATA_DIR, "last_signals.json")
MUTED_COINS_FILE = os.path.join(DATA_DIR, "muted_coins.json")
COIN_INTERVALS_FILE = os.path.join(DATA_DIR, "coin_intervals.json")
TOP_COINS_CACHE_FILE = os.path.join(DATA_DIR, "top_coins_cache.json")

def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Save JSON error:", e)

# persistent state
coins = load_json(USER_COINS_FILE, [])                # list of symbols like BTCUSDT
settings = load_json(SETTINGS_FILE, {
    "rsi_buy": 25,
    "rsi_sell": 75,
    "signal_validity_min": 20,
    "use_sentiment": True
})
last_signals = load_json(LAST_SIGNAL_FILE, {})        # key -> timestamp
muted_coins = load_json(MUTED_COINS_FILE, [])        # list
coin_intervals = load_json(COIN_INTERVALS_FILE, {})  # {symbol: [intervals]}
top_coins_cache = load_json(TOP_COINS_CACHE_FILE, {"ts":0,"coins":[]})

# ============= UTILITIES =============
def normalize_symbol(user_input: str) -> str:
    """
    Convert user input like 'btc', 'BTC', 'btcusdt' to 'BTCUSDT'
    """
    if not user_input:
        return None
    s = user_input.strip().upper()
    # remove spaces or stray chars
    s = "".join(ch for ch in s if ch.isalnum() or ch in "-_.")
    if len(s) == 0:
        return None
    if not s.endswith("USDT"):
        s = s + "USDT"
    return s

def get_klines(symbol: str, interval: str = "15m", limit: int = 200):
    """
    Return list of close prices (floats) or None on error.
    """
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(KLINES_URL, params=params, timeout=10)
        data = r.json()
        # Binance returns dict with error when invalid
        if isinstance(data, dict) and data.get("code"):
            # print debug for developer
            # print("[BINANCE ERROR]", symbol, interval, data)
            return None
        closes = [float(k[4]) for k in data]
        return closes
    except Exception as e:
        # print("Klines error:", e)
        return None

def compute_rsi(closes, period=14):
    if len(closes) < period+1:
        return None
    arr = np.array(closes)
    delta = np.diff(arr)
    up = np.where(delta>0, delta, 0)
    down = np.where(delta<0, -delta, 0)
    # Wilder's smoothing
    roll_up = pd.Series(up).rolling(period).mean()
    roll_down = pd.Series(down).rolling(period).mean()
    rs = roll_up / (roll_down.replace(0, np.nan))
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def compute_ema(closes, period=50):
    s = pd.Series(closes)
    return float(s.ewm(span=period, adjust=False).mean().iloc[-1])

def compute_macd(closes):
    s = pd.Series(closes)
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal = macd_line.ewm(span=9, adjust=False).mean()
    return float(macd_line.iloc[-1]), float(signal.iloc[-1])

# CryptoPanic sentiment
def fetch_sentiment_for(symbol_short: str, limit=10):
    """
    Fetch recent CryptoPanic posts for the given coin short symbol (e.g., BTC)
    Returns sentiment score: -1..+1 (average), and count
    """
    try:
        if not CRYPTOPANIC_KEY:
            return 0.0, 0
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {"auth_token": CRYPTOPANIC_KEY, "currencies": symbol_short, "public": "true", "kind": "news"}
        r = requests.get(url, params=params, timeout=8)
        js = r.json()
        posts = js.get("results") or js.get("results", [])
        if not posts:
            return 0.0, 0
        score = 0
        count = 0
        for p in posts[:limit]:
            # CryptoPanic has 'votes' with 'positive' and 'negative'? fallback use sentiment field 'title' scan
            # Simpler approach: look for "positive"/"bull"/"up"/"buy" in title or 'sentiment' key
            title = (p.get("title") or "").lower()
            # heuristics
            if any(w in title for w in ["bull", "positive", "up", "surge", "rally", "breakout", "buy"]):
                score += 1
            elif any(w in title for w in ["bear", "negative", "down", "dump", "sell", "crash", "drop"]):
                score -= 1
            else:
                # neutral
                score += 0
            count += 1
        if count == 0:
            return 0.0, 0
        return score / count, count
    except Exception as e:
        # print("Sentiment fetch error:", e)
        return 0.0, 0

def get_top_coins(n=50, force_refresh=False):
    """
    Return list of top n USDT trading symbols by quoteVolume.
    Cache for 10 minutes in file to avoid rate limits.
    """
    global top_coins_cache
    now = time.time()
    cached = top_coins_cache
    if not force_refresh and cached.get("ts", 0) + 600 > now and cached.get("coins"):
        return cached["coins"][:n]
    try:
        r = requests.get(TICKER_24HR, timeout=10).json()
        # filter USDT pairs
        usdt = [x for x in r if x.get("symbol", "").endswith("USDT")]
        # sort by quoteVolume
        sorted_ = sorted(usdt, key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
        top = [x["symbol"] for x in sorted_[:n]]
        top_coins_cache = {"ts": now, "coins": top}
        save_json(TOP_COINS_CACHE_FILE, top_coins_cache)
        return top
    except Exception:
        return cached.get("coins", ["BTCUSDT","ETHUSDT","BNBUSDT"])

# ============= SIGNAL LOGIC =============
def generate_combined_signal(symbol: str, interval: str):
    """
    Returns a dict: {"type": "ULTRA BUY"|"ULTRA SELL"|"BUY"|"SELL"|"HOLD", "text": "...", "score": float}
    Combines RSI + MACD + EMA with optional sentiment.
    """
    closes = get_klines(symbol, interval, limit=200)
    if not closes or len(closes) < 30:
        return None
    try:
        rsi_val = compute_rsi(closes, period=14)
        macd_val, macd_signal = compute_macd(closes)
        ema50 = compute_ema(closes, period=50)
        last_price = closes[-1]
    except Exception as e:
        # print("Indicator error:", e)
        return None

    # indicator scoring
    score = 0
    # RSI extremes:
    if rsi_val is not None:
        if rsi_val < settings.get("rsi_buy", 25):
            score += 2
        elif rsi_val < settings.get("rsi_buy", 25) + 10:
            score += 1
        if rsi_val > settings.get("rsi_sell", 75):
            score -= 2
        elif rsi_val > settings.get("rsi_sell", 75) - 10:
            score -= 1

    # MACD momentum
    if macd_val is not None and macd_signal is not None:
        if macd_val > macd_signal:
            score += 1
        else:
            score -= 1

    # Price vs EMA
    if last_price > ema50:
        score += 1
    else:
        score -= 1

    # Sentiment optionally
    sentiment_score = 0.0
    sentiment_count = 0
    if settings.get("use_sentiment", True):
        # derive ticker short e.g., BTC from BTCUSDT
        short = symbol.replace("USDT","")
        sentiment_score, sentiment_count = fetch_sentiment_for(short, limit=6)
        # sentiment_score in -1..1 -> affect score
        score += int(np.sign(sentiment_score))  # +1, 0, or -1

    # Map total score to signal type
    # score can be roughly between -6 and +6
    text = (f"{symbol} {interval} | Price {last_price:.6f} | RSI {rsi_val:.2f} | MACD {macd_val:.6f}/{macd_signal:.6f} | EMA50 {ema50:.6f} | Sent({sentiment_count}) {sentiment_score:.2f}")
    signal_type = None
    if score >= 4:
        signal_type = "ULTRA BUY"
    elif score >= 2:
        signal_type = "BUY"
    elif score <= -4:
        signal_type = "ULTRA SELL"
    elif score <= -2:
        signal_type = "SELL"
    else:
        signal_type = "HOLD"
    return {"type": signal_type, "text": text, "score": score, "sentiment": sentiment_score, "sent_count": sentiment_count}

def send_signal_if_new(symbol, interval, signal):
    """
    Use last_signals persistence to avoid repeats.
    """
    try:
        key = f"{symbol}_{interval}"
        now_ts = time.time()
        cooldown = settings.get("signal_validity_min", 20) * 60
        if symbol in muted_coins:
            return False
        last = last_signals.get(key, 0)
        if now_ts - last > cooldown:
            # send
            msg = f"⚡ {signal['type']} {symbol}\n{signal['text']}\nScore: {signal['score']}"
            bot.send_message(chat_id=CHAT_ID, text=msg)
            last_signals[key] = now_ts
            save_json(LAST_SIGNAL_FILE, last_signals)
            return True
        return False
    except Exception as e:
        print("send_signal_if_new error:", e)
        return False

# ============= BACKGROUND SCANNER =============
auto_signals_enabled = True
tracked_single = None  # if user sets a particular coin to track right now

def background_signal_scanner():
    while True:
        try:
            if not auto_signals_enabled:
                time.sleep(5)
                continue
            # active coins: user coins if present else top 50
            active = list(coins) if coins else get_top_coins(50)
            if tracked_single:
                if tracked_single not in active:
                    active.append(tracked_single)
            # iterate coins
            for symbol in active:
                intervals = coin_intervals.get(symbol, ["1m","5m","15m","1h"])
                for interval in intervals:
                    sig = generate_combined_signal(symbol, interval)
                    if sig and sig["type"] in ("ULTRA BUY","ULTRA SELL","BUY","SELL"):
                        send_signal_if_new(symbol, interval, sig)
            # sleep - you can reduce or increase interval as desired
            time.sleep(30)
        except Exception as e:
            print("Background scanner error:", e)
            time.sleep(5)

threading.Thread(target=background_signal_scanner, daemon=True).start()

# ============= MARKUPS =============
def main_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Add Coin", "📊 My Coins")
    kb.add("📈 Top Movers", "📡 Signals")
    kb.add("🛑 Stop Signals", "🔄 Reset Settings")
    kb.add("⚙ Signal Settings", "🔍 Preview Signals")
    return kb

def back_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⬅ Back")
    return kb

# ============= BOT HANDLERS =============
CHAT_ID = 1263295916  # make sure this matches your admin/chat

@bot.message_handler(commands=["start"])
def handle_start(m):
    bot.send_message(m.chat.id, "🤖 Bot ready. Use menu below:", reply_markup=main_menu_kb())

# ----- Add Coin -----
@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def cmd_add_coin(m):
    msg = bot.send_message(m.chat.id, "Type coin (e.g. BTC or BTCUSDT). I'll normalize to USDT pair.", reply_markup=back_kb())
    bot.register_next_step_handler(msg, process_add_coin)

def process_add_coin(m):
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back to menu.", reply_markup=main_menu_kb())
        return
    sym = normalize_symbol(m.text)
    if not sym:
        bot.send_message(m.chat.id, "Invalid symbol. Try again.", reply_markup=main_menu_kb()); return
    if sym not in coins:
        coins.append(sym); save_json(USER_COINS_FILE, coins)
        bot.send_message(m.chat.id, f"✅ Added {sym}", reply_markup=main_menu_kb())
    else:
        bot.send_message(m.chat.id, f"⚠ {sym} already present.", reply_markup=main_menu_kb())

# ----- My Coins -----
@bot.message_handler(func=lambda msg: msg.text == "📊 My Coins")
def cmd_my_coins(m):
    if not coins:
        bot.send_message(m.chat.id, "No coins added. Use Add Coin.", reply_markup=main_menu_kb())
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: kb.add(c)
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, "Select a coin:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text in coins)
def handle_coin_selected(m):
    symbol = m.text
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in ["1m","5m","15m","1h","1d"]: kb.add(t)
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, f"Choose timeframe for {symbol}:", reply_markup=kb)
    bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: show_analysis_for(symbol, msg))

def show_analysis_for(symbol, m):
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back.", reply_markup=main_menu_kb()); return
    interval = m.text
    sig = generate_combined_signal(symbol, interval)
    if not sig:
        bot.send_message(m.chat.id, f"No data / no strong signal for {symbol} {interval}.", reply_markup=main_menu_kb())
    else:
        bot.send_message(m.chat.id, f"{sig['type']} - {symbol} {interval}\n{sig['text']}\nScore {sig['score']}", reply_markup=main_menu_kb())

# ----- Top Movers -----
@bot.message_handler(func=lambda msg: msg.text == "📈 Top Movers")
def cmd_top_movers(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("5m","1h","24h")
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, "Choose window for Top Movers:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text in ["5m","1h","24h"])
def top_movers_handler(m):
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back.", reply_markup=main_menu_kb()); return
    window = m.text
    # Binance supports intervals for klines but for 24h we use ticker 24hr change percent
    if window == "24h":
        data = requests.get(TICKER_24HR, timeout=10).json()
        usdt = [d for d in data if d["symbol"].endswith("USDT")]
        top = sorted(usdt, key=lambda x: float(x.get("priceChangePercent",0)), reverse=True)[:10]
        msg = "🚀 Top Movers 24h:\n" + "\n".join([f"{t['symbol']}: {t['priceChangePercent']}%" for t in top])
        bot.send_message(m.chat.id, msg, reply_markup=main_menu_kb())
        return
    # for 5m/1h we'll compute percent change from klines
    # map window to kline interval
    intv = "5m" if window == "5m" else "1h"
    top = []
    for sym in get_top_coins(50):
        closes = get_klines(sym, intv, limit=10)
        if not closes or len(closes)<2: continue
        pct = (closes[-1]-closes[0])/closes[0]*100
        top.append((sym, pct))
    top = sorted(top, key=lambda x: x[1], reverse=True)[:10]
    msg = f"🚀 Top Movers {window}:\n" + "\n".join([f"{s}: {p:.2f}%" for s,p in top])
    bot.send_message(m.chat.id, msg, reply_markup=main_menu_kb())

# ----- Signals ----- (submenu)
@bot.message_handler(func=lambda msg: msg.text == "📡 Signals")
def cmd_signals_menu(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("My Coins","All Coins","Particular Coin")
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, "Signals: choose source", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "My Coins")
def signals_mycoins(m):
    active = coins if coins else []
    if not active:
        bot.send_message(m.chat.id, "No coins in My Coins. Use Add Coin.", reply_markup=main_menu_kb()); return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in active: kb.add(c)
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, "Choose a coin to start realtime tracking (or preview):", reply_markup=kb)
    bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: choose_signal_action("my", msg))

@bot.message_handler(func=lambda msg: msg.text == "All Coins")
def signals_allcoins(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Top 50","Top 100")
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, "Choose universe:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text in ["Top 50","Top 100"])
def signals_allcoins_choose(m):
    choice = m.text
    n = 50 if choice=="Top 50" else 100
    # ask timeframe
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in ["1m","5m","15m","1h"]: kb.add(t)
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, f"Universe {choice} chosen. Pick timeframe:", reply_markup=kb)
    bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: preview_universe_signals(n, msg))

def preview_universe_signals(n, m):
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back", reply_markup=main_menu_kb()); return
    tf = m.text
    top = get_top_coins(n)
    signals_out = []
    for sym in top[:50]:  # limit for speed
        s = generate_combined_signal(sym, tf)
        if s and s["type"] in ("ULTRA BUY","ULTRA SELL","BUY","SELL"):
            signals_out.append(f"{s['type']} {sym} | Score {s['score']}")
        if len(signals_out) >= 20: break
    if signals_out:
        bot.send_message(m.chat.id, "Signals:\n" + "\n".join(signals_out), reply_markup=main_menu_kb())
    else:
        bot.send_message(m.chat.id, "No strong signals found.", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda msg: msg.text == "Particular Coin")
def signals_particular(m):
    msg = bot.send_message(m.chat.id, "Enter coin symbol to track (e.g. BTC):", reply_markup=back_kb())
    bot.register_next_step_handler(msg, process_track_particular)

def process_track_particular(m):
    global tracked_single
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back", reply_markup=main_menu_kb()); return
    sym = normalize_symbol(m.text)
    if not sym:
        bot.send_message(m.chat.id, "Invalid symbol", reply_markup=main_menu_kb()); return
    tracked_single = sym
    bot.send_message(m.chat.id, f"🔭 Now tracking {sym} (background scanner).", reply_markup=main_menu_kb())

def choose_signal_action(source, m):
    # used by My Coins flow for extra actions (preview / start tracking etc.)
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back", reply_markup=main_menu_kb()); return
    sym = normalize_symbol(m.text)
    if not sym:
        bot.send_message(m.chat.id, "Invalid coin.", reply_markup=main_menu_kb()); return
    # ask timeframe
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in ["1m","5m","15m","1h"]: kb.add(t)
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, f"Choose timeframe for {sym}:", reply_markup=kb)
    bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: preview_or_start_for(sym, msg))

def preview_or_start_for(sym, m):
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back", reply_markup=main_menu_kb()); return
    tf = m.text
    s = generate_combined_signal(sym, tf)
    if not s:
        bot.send_message(m.chat.id, "No data / no signal.", reply_markup=main_menu_kb()); return
    bot.send_message(m.chat.id, f"{s['type']} {sym} {tf}\n{s['text']}\nScore {s['score']}", reply_markup=main_menu_kb())

# ----- Stop Signals (mute coin) -----
@bot.message_handler(func=lambda msg: msg.text == "🛑 Stop Signals")
def cmd_stop_signals(m):
    if not coins:
        bot.send_message(m.chat.id, "No user coins saved.", reply_markup=main_menu_kb()); return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: kb.add(c)
    kb.add("⬅ Back")
    bot.send_message(m.chat.id, "Select coin to mute signals:", reply_markup=kb)
    bot.register_next_step_handler_by_chat_id(m.chat.id, process_mute_coin)

def process_mute_coin(m):
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back", reply_markup=main_menu_kb()); return
    sym = normalize_symbol(m.text)
    if sym and sym not in muted_coins:
        muted_coins.append(sym)
        save_json(MUTED_COINS_FILE, muted_coins)
    bot.send_message(m.chat.id, f"⛔ Muted {sym}", reply_markup=main_menu_kb())

# ----- Reset settings -----
@bot.message_handler(func=lambda msg: msg.text == "🔄 Reset Settings")
def cmd_reset(m):
    global coins, last_signals, muted_coins, coin_intervals
    coins = []
    last_signals = {}
    muted_coins = []
    coin_intervals = {}
    save_json(USER_COINS_FILE, coins)
    save_json(LAST_SIGNAL_FILE, last_signals)
    save_json(MUTED_COINS_FILE, muted_coins)
    save_json(COIN_INTERVALS_FILE, coin_intervals)
    bot.send_message(m.chat.id, "✅ All cleared.", reply_markup=main_menu_kb())

# ----- Signal Settings -----
@bot.message_handler(func=lambda msg: msg.text == "⚙ Signal Settings")
def cmd_signal_settings(m):
    bot.send_message(m.chat.id, f"Current: RSI Buy {settings['rsi_buy']}  RSI Sell {settings['rsi_sell']}  Validity(min) {settings['signal_validity_min']}  Sentiment {settings['use_sentiment']}\nSend: buy,sell,validity,use_sentiment(True/False)", reply_markup=back_kb())
    bot.register_next_step_handler_by_chat_id(m.chat.id, process_update_settings)

def process_update_settings(m):
    if m.text == "⬅ Back":
        bot.send_message(m.chat.id, "Back", reply_markup=main_menu_kb()); return
    try:
        parts = [x.strip() for x in m.text.split(",")]
        settings["rsi_buy"] = int(parts[0])
        settings["rsi_sell"] = int(parts[1])
        settings["signal_validity_min"] = int(parts[2])
        settings["use_sentiment"] = parts[3].lower() in ("true","1","yes","y")
        save_json(SETTINGS_FILE, settings)
        bot.send_message(m.chat.id, "✅ Settings updated.", reply_markup=main_menu_kb())
    except Exception:
        bot.send_message(m.chat.id, "Invalid format. Use: buy,sell,validity,True/False", reply_markup=main_menu_kb())

# ----- Preview Signals -----
@bot.message_handler(func=lambda msg: msg.text == "🔍 Preview Signals")
def cmd_preview_signals(m):
    active = coins if coins else get_top_coins(50)
    found = []
    for sym in active[:80]:
        # choose a small set of intervals to preview
        for tf in ["1m","5m","15m"]:
            s = generate_combined_signal(sym, tf)
            if s and s["type"] in ("ULTRA BUY","ULTRA SELL","BUY","SELL"):
                found.append(f"{s['type']} {sym} {tf} | Score {s['score']}")
            if len(found) >= 20:
                break
        if len(found) >= 20:
            break
    if found:
        bot.send_message(m.chat.id, "Preview signals:\n" + "\n".join(found), reply_markup=main_menu_kb())
    else:
        bot.send_message(m.chat.id, "No preview signals found right now.", reply_markup=main_menu_kb())

# ============= FLASK WEBHOOK =============
@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook():
    """
    Telegram will POST updates here. We parse and hand to pyTelegramBotAPI.
    """
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        print("Webhook processing error:", e)
    return "OK", 200

@app.route("/")
def index():
    return "BitBot running", 200

# ============= STARTUP =============
def set_webhook():
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        print("Webhook set to", WEBHOOK_URL)
    except Exception as e:
        print("Failed to set webhook:", e)

if __name__ == "__main__":
    # ensure saved state exists
    save_json(USER_COINS_FILE, coins)
    save_json(SETTINGS_FILE, settings)
    save_json(LAST_SIGNAL_FILE, last_signals)
    save_json(MUTED_COINS_FILE, muted_coins)
    save_json(COIN_INTERVALS_FILE, coin_intervals)
    # set webhook and run flask
    set_webhook()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)









