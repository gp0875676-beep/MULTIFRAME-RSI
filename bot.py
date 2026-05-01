"""
Multi-Indicator Combo Trading Bot — Production Grade
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Indicators : RSI + MACD + Bollinger Bands + EMA Cross + Volume Spike
Signal Mode: ALL 5 must agree (strictest — lowest false signals)
Timeframes : 1H and 4H
Coins      : All Binance USDT spot pairs (1500–2000+)
Alerts     : Telegram (FREE)
Deploy     : Railway ready
"""

import asyncio
import aiohttp
import os
import logging
import time
import statistics
from datetime import datetime

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

# RSI settings
RSI_PERIOD          = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD        = float(os.getenv("RSI_OVERSOLD", "35"))      # below = bullish
RSI_OVERBOUGHT      = float(os.getenv("RSI_OVERBOUGHT", "65"))    # above = bearish

# MACD settings
MACD_FAST           = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW           = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL         = int(os.getenv("MACD_SIGNAL", "9"))

# Bollinger Bands settings
BB_PERIOD           = int(os.getenv("BB_PERIOD", "20"))
BB_STD              = float(os.getenv("BB_STD", "2.0"))

# EMA Cross settings
EMA_FAST            = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW            = int(os.getenv("EMA_SLOW", "21"))

# Volume settings
VOLUME_LOOKBACK     = int(os.getenv("VOLUME_LOOKBACK", "20"))     # avg volume period
VOLUME_MULTIPLIER   = float(os.getenv("VOLUME_MULTIPLIER", "1.5")) # spike = 1.5x avg

# Scan settings
SCAN_INTERVAL_MIN   = int(os.getenv("SCAN_INTERVAL_MIN", "30"))
MAX_CONCURRENT      = int(os.getenv("MAX_CONCURRENT", "40"))
KLINES_LIMIT        = 100   # enough for all indicators
TIMEFRAMES          = ["1h", "4h"]
BINANCE_BASE        = "https://api.binance.com"

# ─── Rate Limiter ─────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, rate: int = 900, per: float = 60.0):
        self.rate       = rate
        self.per        = per
        self.allowance  = float(rate)
        self.last_check = time.monotonic()
        self._lock      = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now             = time.monotonic()
            elapsed         = now - self.last_check
            self.last_check = now
            self.allowance  = min(self.rate, self.allowance + elapsed * (self.rate / self.per))
            if self.allowance < 1:
                wait = (1 - self.allowance) / (self.rate / self.per)
                await asyncio.sleep(wait)
                self.allowance = 0.0
            else:
                self.allowance -= 1.0

_rl = RateLimiter()

# ─── Alert Deduplication ──────────────────────────────────────────────────────
class AlertTracker:
    def __init__(self, cooldown_hours: int = 4):
        self.cooldown = cooldown_hours * 3600
        self._store: dict = {}

    def should_alert(self, symbol: str, tf: str, direction: str) -> bool:
        key  = (symbol, tf, direction)
        now  = time.time()
        last = self._store.get(key, 0)
        if now - last >= self.cooldown:
            self._store[key] = now
            return True
        return False

    def cleanup(self):
        now     = time.time()
        expired = [k for k, v in self._store.items() if now - v > self.cooldown * 2]
        for k in expired:
            del self._store[k]

_tracker = AlertTracker(cooldown_hours=4)

# ─── Math Helpers ─────────────────────────────────────────────────────────────
def ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average"""
    if len(values) < period:
        return []
    k      = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def sma(values: list[float], period: int) -> list[float]:
    """Simple Moving Average"""
    return [
        sum(values[i : i + period]) / period
        for i in range(len(values) - period + 1)
    ]

# ─── Indicator Calculations ───────────────────────────────────────────────────
def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI — matches TradingView exactly"""
    if len(closes) < period + 1:
        return None
    deltas   = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains    = [max(d, 0.0) for d in deltas]
    losses   = [abs(min(d, 0.0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def calc_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """
    Returns (macd_line, signal_line, histogram) — all as latest values
    Returns None if not enough data
    """
    if len(closes) < slow + signal:
        return None
    ema_fast   = ema(closes, fast)
    ema_slow   = ema(closes, slow)
    # Align lengths
    min_len    = min(len(ema_fast), len(ema_slow))
    macd_line  = [ema_fast[-min_len + i] - ema_slow[-min_len + i] for i in range(min_len)]
    if len(macd_line) < signal:
        return None
    signal_line = ema(macd_line, signal)
    if not signal_line:
        return None
    hist = macd_line[-1] - signal_line[-1]
    return round(macd_line[-1], 8), round(signal_line[-1], 8), round(hist, 8)


def calc_bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0):
    """
    Returns (upper, middle, lower) as latest values
    Returns None if not enough data
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid    = sum(window) / period
    std    = statistics.stdev(window)
    return round(mid + std_mult * std, 8), round(mid, 8), round(mid - std_mult * std, 8)


def calc_ema_cross(closes: list[float], fast: int = 9, slow: int = 21):
    """
    Returns (ema_fast_val, ema_slow_val, prev_ema_fast, prev_ema_slow)
    Returns None if not enough data
    """
    if len(closes) < slow + 2:
        return None
    ema_f = ema(closes, fast)
    ema_s = ema(closes, slow)
    if len(ema_f) < 2 or len(ema_s) < 2:
        return None
    return ema_f[-1], ema_s[-1], ema_f[-2], ema_s[-2]


def calc_volume_spike(volumes: list[float], lookback: int = 20, multiplier: float = 1.5):
    """
    Returns (current_vol, avg_vol, is_spike: bool)
    Returns None if not enough data
    """
    if len(volumes) < lookback + 1:
        return None
    avg_vol  = sum(volumes[-lookback - 1 : -1]) / lookback
    curr_vol = volumes[-1]
    is_spike = curr_vol >= avg_vol * multiplier
    return round(curr_vol, 2), round(avg_vol, 2), is_spike

# ─── Signal Logic ─────────────────────────────────────────────────────────────
def evaluate_signals(closes: list[float], volumes: list[float]) -> dict | None:
    """
    Evaluates all 5 indicators and checks if ALL agree on BULLISH or BEARISH.
    Returns signal dict or None.

    BULLISH conditions (all must be true):
      RSI        < RSI_OVERSOLD          (oversold = potential bounce)
      MACD hist  > 0                     (momentum turning up)
      Price      < BB lower band         (price below lower band)
      EMA cross  : fast crossed above slow (or fast > slow after being below)
      Volume     : spike present

    BEARISH conditions (all must be true):
      RSI        > RSI_OVERBOUGHT        (overbought = potential drop)
      MACD hist  < 0                     (momentum turning down)
      Price      > BB upper band         (price above upper band)
      EMA cross  : fast crossed below slow
      Volume     : spike present
    """

    # ── Compute all indicators ────────────────────────────────────────────────
    rsi_val = calc_rsi(closes, RSI_PERIOD)
    if rsi_val is None:
        return None

    macd_res = calc_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if macd_res is None:
        return None
    macd_line, signal_line, histogram = macd_res

    bb_res = calc_bollinger(closes, BB_PERIOD, BB_STD)
    if bb_res is None:
        return None
    bb_upper, bb_mid, bb_lower = bb_res

    ema_res = calc_ema_cross(closes, EMA_FAST, EMA_SLOW)
    if ema_res is None:
        return None
    ema_f_now, ema_s_now, ema_f_prev, ema_s_prev = ema_res

    vol_res = calc_volume_spike(volumes, VOLUME_LOOKBACK, VOLUME_MULTIPLIER)
    if vol_res is None:
        return None
    curr_vol, avg_vol, is_vol_spike = vol_res

    price = closes[-1]

    # ── Evaluate individual conditions ────────────────────────────────────────
    # BULLISH
    rsi_bull   = rsi_val <= RSI_OVERSOLD
    macd_bull  = histogram > 0
    bb_bull    = price <= bb_lower
    ema_bull   = (ema_f_now > ema_s_now) and (ema_f_prev <= ema_s_prev)  # fresh cross up
    vol_bull   = is_vol_spike

    # BEARISH
    rsi_bear   = rsi_val >= RSI_OVERBOUGHT
    macd_bear  = histogram < 0
    bb_bear    = price >= bb_upper
    ema_bear   = (ema_f_now < ema_s_now) and (ema_f_prev >= ema_s_prev)  # fresh cross down
    vol_bear   = is_vol_spike

    # ── All-agree check ───────────────────────────────────────────────────────
    all_bull = rsi_bull and macd_bull and bb_bull and ema_bull and vol_bull
    all_bear = rsi_bear and macd_bear and bb_bear and ema_bear and vol_bear

    if not (all_bull or all_bear):
        return None

    direction = "BULLISH" if all_bull else "BEARISH"

    return {
        "direction"  : direction,
        "price"      : round(price, 8),
        "rsi"        : rsi_val,
        "macd_hist"  : histogram,
        "bb_upper"   : bb_upper,
        "bb_lower"   : bb_lower,
        "ema_fast"   : round(ema_f_now, 6),
        "ema_slow"   : round(ema_s_now, 6),
        "curr_vol"   : curr_vol,
        "avg_vol"    : avg_vol,
        # Per-indicator breakdown
        "checks": {
            "rsi"    : "✅" if (rsi_bull if direction == "BULLISH" else rsi_bear) else "❌",
            "macd"   : "✅" if (macd_bull if direction == "BULLISH" else macd_bear) else "❌",
            "bb"     : "✅" if (bb_bull if direction == "BULLISH" else bb_bear) else "❌",
            "ema"    : "✅" if (ema_bull if direction == "BULLISH" else ema_bear) else "❌",
            "volume" : "✅" if is_vol_spike else "❌",
        }
    }

# ─── Binance API ──────────────────────────────────────────────────────────────
async def fetch_symbols(session: aiohttp.ClientSession) -> list[str]:
    try:
        await _rl.acquire()
        async with session.get(
            f"{BINANCE_BASE}/api/v3/exchangeInfo",
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                log.error(f"exchangeInfo HTTP {resp.status}")
                return []
            data = await resp.json()
            syms = [
                s["symbol"]
                for s in data.get("symbols", [])
                if s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
                and s.get("isSpotTradingAllowed", False)
            ]
            log.info(f"Fetched {len(syms)} USDT pairs")
            return syms
    except Exception as e:
        log.error(f"fetch_symbols: {e}")
        return []


async def fetch_klines(
    session: aiohttp.ClientSession, symbol: str, interval: str
) -> tuple[list[float], list[float]] | None:
    """Returns (closes, volumes) or None"""
    try:
        await _rl.acquire()
        async with session.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": KLINES_LIMIT},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 400:
                return None
            if resp.status != 200:
                return None
            data    = await resp.json()
            closes  = [float(c[4]) for c in data]
            volumes = [float(c[5]) for c in data]
            min_needed = max(MACD_SLOW + MACD_SIGNAL, BB_PERIOD, EMA_SLOW + 2, VOLUME_LOOKBACK + 1, RSI_PERIOD + 1)
            if len(closes) < min_needed:
                return None
            return closes, volumes
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None

# ─── Telegram ─────────────────────────────────────────────────────────────────
async def send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[NO TG] {text[:80]}")
        return False
    try:
        async with session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def build_message(symbol: str, tf: str, sig: dict) -> str:
    d       = sig["direction"]
    emoji   = "🟢" if d == "BULLISH" else "🔴"
    arrow   = "📈" if d == "BULLISH" else "📉"
    chk     = sig["checks"]
    vol_ratio = round(sig["curr_vol"] / sig["avg_vol"], 2) if sig["avg_vol"] else 0
    now     = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"{emoji} <b>COMBO SIGNAL — {d}</b> {arrow}\n\n"
        f"🪙 <b>Coin:</b> <code>{symbol}</code>\n"
        f"⏱ <b>Timeframe:</b> {tf.upper()}\n"
        f"💰 <b>Price:</b> {sig['price']}\n\n"
        f"<b>━━ Indicator Breakdown ━━</b>\n"
        f"{chk['rsi']}  RSI: <b>{sig['rsi']}</b>\n"
        f"{chk['macd']}  MACD Hist: <b>{sig['macd_hist']}</b>\n"
        f"{chk['bb']}  BB {'Lower' if d=='BULLISH' else 'Upper'}: <b>{sig['bb_lower'] if d=='BULLISH' else sig['bb_upper']}</b>\n"
        f"{chk['ema']}  EMA({EMA_FAST}/{EMA_SLOW}): <b>{sig['ema_fast']} / {sig['ema_slow']}</b>\n"
        f"{chk['volume']}  Volume: <b>{vol_ratio}x avg</b> 🔥\n\n"
        f"⏰ <b>{now}</b>\n"
        f"#COMBO #{symbol} #{tf.upper()} #{d}"
    )

# ─── Worker ───────────────────────────────────────────────────────────────────
async def process_symbol(
    session: aiohttp.ClientSession,
    symbol: str,
    sem: asyncio.Semaphore,
) -> list[dict]:
    alerts = []
    async with sem:
        for tf in TIMEFRAMES:
            result = await fetch_klines(session, symbol, tf)
            if result is None:
                continue
            closes, volumes = result
            sig = evaluate_signals(closes, volumes)
            if sig and _tracker.should_alert(symbol, tf, sig["direction"]):
                sig["symbol"] = symbol
                sig["tf"]     = tf
                alerts.append(sig)
                log.info(f"🔔 {symbol} [{tf}] → {sig['direction']} | RSI={sig['rsi']}")
    return alerts

# ─── Scan ─────────────────────────────────────────────────────────────────────
async def run_scan():
    start = time.monotonic()
    log.info("═" * 65)
    log.info("🚀 Starting Multi-Indicator Combo Scan...")

    connector = aiohttp.TCPConnector(limit=200, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        symbols = await fetch_symbols(session)
        if not symbols:
            log.error("No symbols — aborting scan")
            return

        sem        = asyncio.Semaphore(MAX_CONCURRENT)
        all_alerts = []
        BATCH      = 300

        for i in range(0, len(symbols), BATCH):
            chunk   = symbols[i : i + BATCH]
            tasks   = [process_symbol(session, s, sem) for s in chunk]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    all_alerts.extend(r)
            done = min(i + BATCH, len(symbols))
            log.info(f"Progress: {done}/{len(symbols)} | Signals so far: {len(all_alerts)}")

        # Send alerts
        if all_alerts:
            log.info(f"🔔 Sending {len(all_alerts)} alerts...")
            for a in all_alerts:
                msg = build_message(a["symbol"], a["tf"], a)
                await send_telegram(session, msg)
                await asyncio.sleep(0.35)   # Telegram flood protection
        else:
            log.info("✅ No signals this scan (all 5 indicators didn't agree)")

        _tracker.cleanup()
        elapsed = time.monotonic() - start
        log.info(f"✅ Scan done in {elapsed:.1f}s — {len(all_alerts)} signals sent")
        log.info("═" * 65)

# ─── Startup ──────────────────────────────────────────────────────────────────
async def send_startup(session: aiohttp.ClientSession):
    msg = (
        "✅ <b>Multi-Indicator Combo Bot Started!</b>\n\n"
        "<b>━━ Strategy Config ━━</b>\n"
        f"📊 RSI({RSI_PERIOD}): Bull ≤{RSI_OVERSOLD} | Bear ≥{RSI_OVERBOUGHT}\n"
        f"📈 MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}\n"
        f"📉 Bollinger Bands: {BB_PERIOD} period, {BB_STD}σ\n"
        f"〽️ EMA Cross: {EMA_FAST} / {EMA_SLOW}\n"
        f"🔊 Volume Spike: {VOLUME_MULTIPLIER}x average\n\n"
        f"⚡ Mode: ALL 5 indicators must agree\n"
        f"⏱ Timeframes: 1H + 4H\n"
        f"🔄 Scan every: {SCAN_INTERVAL_MIN} minutes\n\n"
        "Scanning all Binance USDT pairs... 🔍"
    )
    await send_telegram(session, msg)

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    log.info("Multi-Indicator Combo Bot — Initializing")
    async with aiohttp.ClientSession() as s:
        await send_startup(s)

    while True:
        try:
            await run_scan()
        except Exception as e:
            log.error(f"Scan error (auto-retry): {e}", exc_info=True)
        log.info(f"💤 Sleeping {SCAN_INTERVAL_MIN} min...")
        await asyncio.sleep(SCAN_INTERVAL_MIN * 60)


if __name__ == "__main__":
    asyncio.run(main())
