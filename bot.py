"""
Multi-Indicator Combo Trading Bot — v2 (Multi-Exchange)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fix: HTTP 451 (Binance geo-block on US servers)
Solution: Auto-fallback chain → Binance → KuCoin → Bybit → Gate.io
          First working exchange is used automatically.

Indicators : RSI + MACD + Bollinger Bands + EMA Cross + Volume Spike
Signal Mode: ALL 5 must agree (strictest)
Timeframes : 1H and 4H
Deploy     : Railway (set region = Europe for best results)
"""

import asyncio
import aiohttp
import os
import logging
import time
import statistics
from datetime import datetime

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

RSI_PERIOD          = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD        = float(os.getenv("RSI_OVERSOLD", "35"))
RSI_OVERBOUGHT      = float(os.getenv("RSI_OVERBOUGHT", "65"))
MACD_FAST           = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW           = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL_P       = int(os.getenv("MACD_SIGNAL", "9"))
BB_PERIOD           = int(os.getenv("BB_PERIOD", "20"))
BB_STD              = float(os.getenv("BB_STD", "2.0"))
EMA_FAST            = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW            = int(os.getenv("EMA_SLOW", "21"))
VOLUME_LOOKBACK     = int(os.getenv("VOLUME_LOOKBACK", "20"))
VOLUME_MULTIPLIER   = float(os.getenv("VOLUME_MULTIPLIER", "1.5"))
SCAN_INTERVAL_MIN   = int(os.getenv("SCAN_INTERVAL_MIN", "30"))
MAX_CONCURRENT      = int(os.getenv("MAX_CONCURRENT", "40"))
KLINES_LIMIT        = 120
TIMEFRAMES          = ["1h", "4h"]

# ─── Exchange Configs (priority order) ────────────────────────────────────────
# Bot auto-detects which one works and uses it
EXCHANGES = [
    {
        "name": "Binance",
        "symbols_url": "https://api.binance.com/api/v3/exchangeInfo",
        "klines_url":  "https://api.binance.com/api/v3/klines",
        "type": "binance",
    },
    {
        "name": "Binance Futures",
        "symbols_url": "https://fapi.binance.com/fapi/v1/exchangeInfo",
        "klines_url":  "https://fapi.binance.com/fapi/v1/klines",
        "type": "binance_futures",
    },
    {
        "name": "KuCoin",
        "symbols_url": "https://api.kucoin.com/api/v2/symbols",
        "klines_url":  "https://api.kucoin.com/api/v1/market/candles",
        "type": "kucoin",
    },
    {
        "name": "Bybit",
        "symbols_url": "https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000",
        "klines_url":  "https://api.bybit.com/v5/market/kline",
        "type": "bybit",
    },
    {
        "name": "Gate.io",
        "symbols_url": "https://api.gateio.ws/api/v4/spot/currency_pairs",
        "klines_url":  "https://api.gateio.ws/api/v4/spot/candlesticks",
        "type": "gate",
    },
]

# Active exchange (set after probe)
_active_exchange: dict | None = None

# ─── Rate Limiter ─────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, rate: int = 800, per: float = 60.0):
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
        if now - self._store.get(key, 0) >= self.cooldown:
            self._store[key] = now
            return True
        return False

    def cleanup(self):
        now     = time.time()
        expired = [k for k, v in self._store.items() if now - v > self.cooldown * 2]
        for k in expired:
            del self._store[k]

_tracker = AlertTracker(cooldown_hours=4)

# ─── Math ─────────────────────────────────────────────────────────────────────
def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k      = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

# ─── Indicators ───────────────────────────────────────────────────────────────
def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [max(d, 0.0) for d in deltas]
    losses   = [abs(min(d, 0.0)) for d in deltas]
    avg_g    = sum(gains[:period]) / period
    avg_l    = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)

def calc_macd(closes: list[float]):
    if len(closes) < MACD_SLOW + MACD_SIGNAL_P:
        return None
    ef   = _ema(closes, MACD_FAST)
    es   = _ema(closes, MACD_SLOW)
    ml   = [ef[-len(es)+i] - es[i] for i in range(len(es))]
    if len(ml) < MACD_SIGNAL_P:
        return None
    sl   = _ema(ml, MACD_SIGNAL_P)
    if not sl:
        return None
    return round(ml[-1], 8), round(sl[-1], 8), round(ml[-1] - sl[-1], 8)

def calc_bollinger(closes: list[float]):
    if len(closes) < BB_PERIOD:
        return None
    w   = closes[-BB_PERIOD:]
    mid = sum(w) / BB_PERIOD
    std = statistics.stdev(w)
    return round(mid + BB_STD * std, 8), round(mid, 8), round(mid - BB_STD * std, 8)

def calc_ema_cross(closes: list[float]):
    if len(closes) < EMA_SLOW + 2:
        return None
    ef = _ema(closes, EMA_FAST)
    es = _ema(closes, EMA_SLOW)
    if len(ef) < 2 or len(es) < 2:
        return None
    return ef[-1], es[-1], ef[-2], es[-2]

def calc_volume_spike(volumes: list[float]):
    if len(volumes) < VOLUME_LOOKBACK + 1:
        return None
    avg  = sum(volumes[-VOLUME_LOOKBACK-1:-1]) / VOLUME_LOOKBACK
    curr = volumes[-1]
    return round(curr, 2), round(avg, 2), curr >= avg * VOLUME_MULTIPLIER

def evaluate_signals(closes: list[float], volumes: list[float]) -> dict | None:
    rsi_v = calc_rsi(closes, RSI_PERIOD)
    if rsi_v is None: return None
    macd_r = calc_macd(closes)
    if macd_r is None: return None
    _, _, hist = macd_r
    bb_r = calc_bollinger(closes)
    if bb_r is None: return None
    bb_up, bb_mid, bb_lo = bb_r
    ema_r = calc_ema_cross(closes)
    if ema_r is None: return None
    ef, es, ef_p, es_p = ema_r
    vol_r = calc_volume_spike(volumes)
    if vol_r is None: return None
    curr_v, avg_v, spike = vol_r
    price = closes[-1]

    bull = (rsi_v <= RSI_OVERSOLD) and (hist > 0) and (price <= bb_lo) and (ef > es and ef_p <= es_p) and spike
    bear = (rsi_v >= RSI_OVERBOUGHT) and (hist < 0) and (price >= bb_up) and (ef < es and ef_p >= es_p) and spike

    if not (bull or bear):
        return None

    d = "BULLISH" if bull else "BEARISH"
    return {
        "direction": d,
        "price":     round(price, 8),
        "rsi":       rsi_v,
        "hist":      hist,
        "bb_up":     bb_up,
        "bb_lo":     bb_lo,
        "ema_f":     round(ef, 6),
        "ema_s":     round(es, 6),
        "curr_v":    curr_v,
        "avg_v":     avg_v,
        "vol_ratio": round(curr_v / avg_v, 2) if avg_v else 0,
        "checks": {
            "rsi":    "✅" if (rsi_v <= RSI_OVERSOLD if bull else rsi_v >= RSI_OVERBOUGHT) else "❌",
            "macd":   "✅" if (hist > 0 if bull else hist < 0) else "❌",
            "bb":     "✅" if (price <= bb_lo if bull else price >= bb_up) else "❌",
            "ema":    "✅" if (ef > es if bull else ef < es) else "❌",
            "volume": "✅" if spike else "❌",
        }
    }

# ─── Exchange: Probe which one works ──────────────────────────────────────────
async def probe_exchanges(session: aiohttp.ClientSession) -> dict | None:
    """Try each exchange in order, return first that responds OK"""
    global _active_exchange
    for ex in EXCHANGES:
        try:
            await _rl.acquire()
            async with session.get(
                ex["symbols_url"],
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status == 200:
                    log.info(f"✅ Exchange selected: {ex['name']}")
                    _active_exchange = ex
                    return ex
                else:
                    log.warning(f"⚠️  {ex['name']}: HTTP {resp.status} — trying next...")
        except Exception as e:
            log.warning(f"⚠️  {ex['name']}: {str(e)[:60]} — trying next...")
        await asyncio.sleep(0.5)

    log.error("❌ All exchanges failed!")
    return None

# ─── Exchange: Fetch Symbols ──────────────────────────────────────────────────
async def fetch_symbols(session: aiohttp.ClientSession, ex: dict) -> list[str]:
    try:
        await _rl.acquire()
        async with session.get(
            ex["symbols_url"],
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as resp:
            if resp.status != 200:
                log.error(f"fetch_symbols HTTP {resp.status} — will re-probe next scan")
                return []
            data = await resp.json(content_type=None)

            t = ex["type"]
            if t in ("binance", "binance_futures"):
                return [
                    s["symbol"] for s in data.get("symbols", [])
                    if s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"
                    and (s.get("isSpotTradingAllowed", False) or t == "binance_futures")
                ]
            elif t == "kucoin":
                return [
                    s["symbol"].replace("-", "") for s in data.get("data", [])
                    if s.get("quoteCurrency") == "USDT" and s.get("enableTrading")
                ]
            elif t == "bybit":
                items = data.get("result", {}).get("list", [])
                return [
                    s["symbol"] for s in items
                    if s["symbol"].endswith("USDT") and s.get("status") == "Trading"
                ]
            elif t == "gate":
                return [
                    s["id"].replace("_", "") for s in data
                    if s.get("quote") == "USDT" and s.get("trade_status") == "tradable"
                ]
            return []
    except Exception as e:
        log.error(f"fetch_symbols: {e}")
        return []

# ─── Exchange: Fetch Klines ───────────────────────────────────────────────────
async def fetch_klines(
    session: aiohttp.ClientSession,
    ex: dict,
    symbol: str,
    interval: str,
) -> tuple[list[float], list[float]] | None:
    """Returns (closes, volumes) normalised from any exchange format"""
    t = ex["type"]

    # Build params per exchange
    if t in ("binance", "binance_futures"):
        params = {"symbol": symbol, "interval": interval, "limit": KLINES_LIMIT}
    elif t == "kucoin":
        # KuCoin uses base-quote format: BTC-USDT
        base   = symbol.replace("USDT", "")
        ku_sym = f"{base}-USDT"
        tf_map = {"1h": "1hour", "4h": "4hour", "15m": "15min", "1d": "1day"}
        params = {"symbol": ku_sym, "type": tf_map.get(interval, "1hour")}
    elif t == "bybit":
        tf_map = {"1h": "60", "4h": "240", "15m": "15", "1d": "D"}
        params = {"category": "spot", "symbol": symbol, "interval": tf_map.get(interval, "60"), "limit": KLINES_LIMIT}
    elif t == "gate":
        base   = symbol.replace("USDT", "")
        tf_map = {"1h": "1h", "4h": "4h", "15m": "15m", "1d": "1d"}
        params = {"currency_pair": f"{base}_USDT", "interval": tf_map.get(interval, "1h"), "limit": KLINES_LIMIT}
    else:
        return None

    try:
        await _rl.acquire()
        async with session.get(
            ex["klines_url"],
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as resp:
            if resp.status in (400, 404):
                return None
            if resp.status != 200:
                return None
            raw = await resp.json(content_type=None)

            # Normalise to (closes, volumes)
            closes  = []
            volumes = []

            if t in ("binance", "binance_futures"):
                # [[open_time, open, high, low, close, volume, ...], ...]
                for c in raw:
                    closes.append(float(c[4]))
                    volumes.append(float(c[5]))

            elif t == "kucoin":
                # {"data": [[time, open, close, high, low, vol, turnover], ...]} newest first
                rows = raw.get("data", [])
                rows = list(reversed(rows))
                for r in rows:
                    closes.append(float(r[2]))
                    volumes.append(float(r[5]))

            elif t == "bybit":
                # {"result": {"list": [[time, open, high, low, close, volume, turnover], ...]}} newest first
                rows = raw.get("result", {}).get("list", [])
                rows = list(reversed(rows))
                for r in rows:
                    closes.append(float(r[4]))
                    volumes.append(float(r[5]))

            elif t == "gate":
                # [{"t": time, "o": open, "h": high, "l": low, "c": close, "v": volume}, ...]
                for c in raw:
                    closes.append(float(c["c"]))
                    volumes.append(float(c["v"]))

            min_needed = max(MACD_SLOW + MACD_SIGNAL_P, BB_PERIOD, EMA_SLOW + 2, VOLUME_LOOKBACK + 1, RSI_PERIOD + 1)
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
        log.info(f"[NO TG] {text[:100]}")
        return False
    try:
        async with session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return True
            log.error(f"Telegram HTTP {resp.status}: {await resp.text()}")
            return False
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

def build_message(symbol: str, tf: str, sig: dict, exchange_name: str) -> str:
    d   = sig["direction"]
    emj = "🟢" if d == "BULLISH" else "🔴"
    arr = "📈" if d == "BULLISH" else "📉"
    c   = sig["checks"]
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{emj} <b>COMBO SIGNAL — {d}</b> {arr}\n\n"
        f"🪙 <b>Coin:</b> <code>{symbol}</code>\n"
        f"⏱ <b>Timeframe:</b> {tf.upper()}\n"
        f"💰 <b>Price:</b> {sig['price']}\n"
        f"🏦 <b>Exchange:</b> {exchange_name}\n\n"
        f"<b>━━ Indicator Breakdown ━━</b>\n"
        f"{c['rsi']}  RSI({RSI_PERIOD}): <b>{sig['rsi']}</b>\n"
        f"{c['macd']}  MACD Histogram: <b>{sig['hist']}</b>\n"
        f"{c['bb']}  BB {'Lower' if d=='BULLISH' else 'Upper'}: <b>{sig['bb_lo'] if d=='BULLISH' else sig['bb_up']}</b>\n"
        f"{c['ema']}  EMA({EMA_FAST}/{EMA_SLOW}): <b>{sig['ema_f']} / {sig['ema_s']}</b>\n"
        f"{c['volume']}  Volume Spike: <b>{sig['vol_ratio']}× avg</b> 🔥\n\n"
        f"⏰ {now}\n"
        f"#COMBO #{symbol} #{tf.upper()} #{d}"
    )

# ─── Worker ───────────────────────────────────────────────────────────────────
async def process_symbol(
    session: aiohttp.ClientSession,
    ex: dict,
    symbol: str,
    sem: asyncio.Semaphore,
) -> list[dict]:
    alerts = []
    async with sem:
        for tf in TIMEFRAMES:
            result = await fetch_klines(session, ex, symbol, tf)
            if result is None:
                continue
            closes, volumes = result
            sig = evaluate_signals(closes, volumes)
            if sig and _tracker.should_alert(symbol, tf, sig["direction"]):
                sig.update({"symbol": symbol, "tf": tf})
                alerts.append(sig)
                log.info(f"🔔 {symbol} [{tf}] {sig['direction']} | RSI={sig['rsi']} Vol={sig['vol_ratio']}x")
    return alerts

# ─── Scan ─────────────────────────────────────────────────────────────────────
async def run_scan():
    global _active_exchange
    start = time.monotonic()
    log.info("═" * 65)
    log.info("🚀 Starting Multi-Indicator Combo Scan...")

    connector = aiohttp.TCPConnector(limit=200, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:

        # Re-probe if no active exchange or on each scan to handle failures
        ex = _active_exchange
        if ex is None:
            ex = await probe_exchanges(session)
        if ex is None:
            log.error("No working exchange found — retrying next scan")
            return

        symbols = await fetch_symbols(session, ex)
        if not symbols:
            log.warning(f"{ex['name']} returned 0 symbols — re-probing next scan")
            _active_exchange = None
            return

        log.info(f"Exchange: {ex['name']} | Symbols: {len(symbols)} | TFs: {TIMEFRAMES}")

        sem        = asyncio.Semaphore(MAX_CONCURRENT)
        all_alerts = []
        BATCH      = 300

        for i in range(0, len(symbols), BATCH):
            chunk   = symbols[i : i + BATCH]
            tasks   = [process_symbol(session, ex, s, sem) for s in chunk]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    all_alerts.extend(r)
            log.info(f"Progress: {min(i+BATCH, len(symbols))}/{len(symbols)} | Signals: {len(all_alerts)}")

        if all_alerts:
            log.info(f"🔔 Sending {len(all_alerts)} alerts...")
            for a in all_alerts:
                msg = build_message(a["symbol"], a["tf"], a, ex["name"])
                await send_telegram(session, msg)
                await asyncio.sleep(0.35)
        else:
            log.info("✅ No signals (all 5 indicators didn't align)")

        _tracker.cleanup()
        log.info(f"✅ Scan done in {time.monotonic()-start:.1f}s | {len(all_alerts)} signals sent")
        log.info("═" * 65)

# ─── Startup ──────────────────────────────────────────────────────────────────
async def send_startup(session: aiohttp.ClientSession, ex_name: str):
    msg = (
        f"✅ <b>Multi-Indicator Combo Bot v2 Started!</b>\n\n"
        f"🏦 <b>Exchange:</b> {ex_name}\n\n"
        f"<b>━━ Strategy ━━</b>\n"
        f"📊 RSI({RSI_PERIOD}): Bull≤{RSI_OVERSOLD} Bear≥{RSI_OVERBOUGHT}\n"
        f"📈 MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL_P}\n"
        f"📉 Bollinger Bands: {BB_PERIOD}p {BB_STD}σ\n"
        f"〽️ EMA Cross: {EMA_FAST}/{EMA_SLOW}\n"
        f"🔊 Volume Spike: {VOLUME_MULTIPLIER}× avg\n\n"
        f"⚡ Mode: ALL 5 must agree\n"
        f"⏱ Timeframes: 1H + 4H\n"
        f"🔄 Scan every: {SCAN_INTERVAL_MIN} min\n\n"
        f"Bot is live! 🚀"
    )
    await send_telegram(session, msg)

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    global _active_exchange
    log.info("Multi-Indicator Combo Bot v2 — Initializing")

    # Initial probe
    async with aiohttp.ClientSession() as s:
        ex = await probe_exchanges(s)
        if ex:
            await send_startup(s, ex["name"])
        else:
            log.error("Could not connect to any exchange on startup — will retry each scan")

    while True:
        try:
            await run_scan()
        except Exception as e:
            log.error(f"Scan crashed (auto-retry): {e}", exc_info=True)
            _active_exchange = None  # Force re-probe
        log.info(f"💤 Sleeping {SCAN_INTERVAL_MIN} min...")
        await asyncio.sleep(SCAN_INTERVAL_MIN * 60)

if __name__ == "__main__":
    asyncio.run(main())
