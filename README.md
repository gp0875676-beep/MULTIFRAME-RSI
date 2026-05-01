# 🤖 Multi-Indicator Combo Trading Bot

**5 Indicators | ALL must agree | 1H & 4H | Telegram | 100% Free | Railway Ready**

---

## 🧠 Strategy Logic

This bot fires a signal **ONLY when all 5 indicators agree** — this is the strictest possible filter, meaning:
- ✅ Very few false signals
- ✅ High confidence entries
- ✅ Both BULLISH and BEARISH signals

### 🟢 BULLISH Signal (all 5 must be true)
| # | Indicator | Condition |
|---|---|---|
| 1 | RSI | ≤ 35 (oversold — potential bounce) |
| 2 | MACD Histogram | > 0 (momentum turning bullish) |
| 3 | Bollinger Bands | Price ≤ Lower Band (price stretched down) |
| 4 | EMA Cross | Fast EMA just crossed ABOVE Slow EMA |
| 5 | Volume | Current volume ≥ 1.5× average (confirmation) |

### 🔴 BEARISH Signal (all 5 must be true)
| # | Indicator | Condition |
|---|---|---|
| 1 | RSI | ≥ 65 (overbought — potential drop) |
| 2 | MACD Histogram | < 0 (momentum turning bearish) |
| 3 | Bollinger Bands | Price ≥ Upper Band (price stretched up) |
| 4 | EMA Cross | Fast EMA just crossed BELOW Slow EMA |
| 5 | Volume | Current volume ≥ 1.5× average (confirmation) |

---

## 📊 Sample Alert

```
🟢 COMBO SIGNAL — BULLISH 📈

🪙 Coin: SOLUSDT
⏱ Timeframe: 4H
💰 Price: 142.35

━━ Indicator Breakdown ━━
✅  RSI: 28.4
✅  MACD Hist: 0.00023
✅  BB Lower: 139.20
✅  EMA(9/21): 140.1 / 143.8
✅  Volume: 2.3x avg 🔥

⏰ 2025-01-15 14:30 UTC
#COMBO #SOLUSDT #4H #BULLISH
```

---

## 🛠 Setup

### Step 1: Telegram Bot (FREE)
1. Open Telegram → search `@BotFather`
2. Send `/newbot` → follow steps → copy **token**
3. Search `@userinfobot` → start it → copy your **Chat ID**

### Step 2: Deploy on Railway
1. Push all files to a **GitHub repo**
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add Variables:
   ```
   TELEGRAM_BOT_TOKEN = your_token
   TELEGRAM_CHAT_ID   = your_chat_id
   ```
4. Deploy → Done! 🚀

---

## ⚙️ All Settings (Railway Variables)

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Required | From @BotFather |
| `TELEGRAM_CHAT_ID` | Required | Your chat ID |
| `RSI_PERIOD` | 14 | RSI calculation period |
| `RSI_OVERSOLD` | 35 | RSI bull threshold |
| `RSI_OVERBOUGHT` | 65 | RSI bear threshold |
| `MACD_FAST` | 12 | MACD fast EMA |
| `MACD_SLOW` | 26 | MACD slow EMA |
| `MACD_SIGNAL` | 9 | MACD signal line |
| `BB_PERIOD` | 20 | Bollinger period |
| `BB_STD` | 2.0 | Bollinger std deviation |
| `EMA_FAST` | 9 | Fast EMA period |
| `EMA_SLOW` | 21 | Slow EMA period |
| `VOLUME_LOOKBACK` | 20 | Volume avg period |
| `VOLUME_MULTIPLIER` | 1.5 | Volume spike threshold |
| `SCAN_INTERVAL_MIN` | 30 | Minutes between scans |
| `MAX_CONCURRENT` | 40 | Parallel API calls |

---

## 🆓 100% Free Stack

| Component | Service | Cost |
|---|---|---|
| Market data | Binance Public API | FREE |
| Alerts | Telegram Bot API | FREE |
| Hosting | Railway hobby tier | FREE |
| **Total** | | **$0/month** |

---

## 🧪 Local Testing

```bash
pip install aiohttp

export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"

python bot.py
```

---

## ❓ FAQ

**Q: Why so few signals?**
All 5 indicators must agree simultaneously — this is by design. You get fewer but much higher quality signals.

**Q: Too few signals? How to get more?**
Relax thresholds: set `RSI_OVERSOLD=40`, `RSI_OVERBOUGHT=60`, `VOLUME_MULTIPLIER=1.2`

**Q: Too many signals?**
Tighten: `RSI_OVERSOLD=25`, `RSI_OVERBOUGHT=75`, `VOLUME_MULTIPLIER=2.0`

**Q: Can I add more timeframes?**
Yes — edit `TIMEFRAMES = ["1h", "4h"]` in bot.py to add `"15m"`, `"1d"`, etc.
