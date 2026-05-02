"""
Multi-Indicator Combo Trading Bot — v3 (SMC + Volume)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Strategy : Volume Spike + SMC (BOS + CHoCH + FVG)
Signal   : 2 out of 3 SMC conditions + Volume must agree
Timeframes: 1H and 4H
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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

VOLUME_LOOKBACK    = int(os.getenv("VOLUME_LOOKBACK", "20"))
VOLUME_MULTIPLIER  = float(os.getenv("VOLUME_MULTIPLIER", "1.5"))
SWING_LOOKBACK     = int(os.getenv("SWING_LOOKBACK", "3"))   # candles each side for swing
SCAN_INTERVAL_MIN  = int(os.getenv("SCAN_INTERVAL_MIN", "30"))
MAX_CONCURRENT     = int(os.getenv("MAX_CONCURRENT", "40"))
KLINES_LIMIT       = 120
TIMEFRAMES         = ["1h", "4h"]
MIN_SMC_CONDITIONS = 2   # 2 out of 3 SMC (BOS, CHoCH, FVG) must agree

# ─── Exchange Configs ─────────────────────────────────────────────────────────
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
        key = (symbol, tf, direction)
        now = time.time()
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

# ─── SMC: Swing Points ────────────────────────────────────────────────────────
def detect_swings(highs: list[float], lows: list[float], lookback: int = 3):
    """
    Returns lists of (index, price) for swing highs and lows.
    lookback = candles on each side that must be lower/higher.
    """
    swing_highs = []
    swing_lows  = []
    n = len(highs)

    for i in range(lookback, n - lookback):
        # Swing High: highest in window
        if highs[i] == max(highs[i - lookback: i + lookback + 1]):
            swing_highs.append((i, highs[i]))
        # Swing Low: lowest in window
        if lows[i] == min(lows[i - lookback: i + lookback + 1]):
            swing_lows.append((i, lows[i]))

    return swing_highs, swing_lows

# ─── SMC: BOS ────────────────────────────────────────────────────────────────
def calc_bos(closes: list[float], swing_highs: list, swing_lows: list) -> str | None:
    """
    Bullish BOS : current close breaks above last swing high
    Bearish BOS : current close breaks below last swing low
    """
    if not swing_highs or not swing_lows:
        return None

    price         = closes[-1]
    last_sh_price = swing_highs[-1][1]
    last_sl_price = swing_lows[-1][1]

    if price > last_sh_price:
        return "bullish"
    elif price < last_sl_price:
        return "bearish"
    return None

# ─── SMC: CHoCH ──────────────────────────────────────────────────────────────
def calc_choch(swing_highs: list, swing_lows: list, closes: list[float]) -> str | None:
    """
    Needs at least 2 swing highs and 2 swing lows.
    Uptrend   = HH + HL  → bearish CHoCH if price breaks below last HL
    Downtrend = LH + LL  → bullish CHoCH if price breaks above last LH
    """
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    sh1, sh2 = swing_highs[-2][1], swing_highs[-1][1]
    sl1, sl2 = swing_lows[-2][1],  swing_lows[-1][1]
    price    = closes[-1]

    uptrend   = sh2 > sh1 and sl2 > sl1   # HH + HL
    downtrend = sh2 < sh1 and sl2 < sl1   # LH + LL

    if downtrend and price > sh2:          # breaks above last LH → reversal up
        return "bullish"
    if uptrend and price < sl2:            # breaks below last HL → reversal down
        return "bearish"
    return None

# ─── SMC: FVG ────────────────────────────────────────────────────────────────
def calc_fvg(highs: list[float], lows: list[float]) -> str | None:
    """
    Scans last 15 candles for a Fair Value Gap.
    Bullish FVG : high[i-2] < low[i]
    Bearish FVG : low[i-2]  > high[i]
    Returns direction of most recent FVG found.
    """
    n = len(highs)
    if n < 3:
        return None

    for i in range(n - 1, 1, -1):
        if highs[i - 2] < lows[i]:
            return "bullish"
        if lows[i - 2] > highs[i]:
            return "bearish"
        if (n - 1 - i) >= 15:
            break

    return None

# ─── Volume Spike ─────────────────────────────────────────────────────────────
def calc_volume_spike(volumes: list[float]) -> tuple[float, float, bool] | None:
    if len(volumes) < VOLUME_LOOKBACK + 1:
        return None
    avg  = sum(volumes[-VOLUME_LOOKBACK - 1:-1]) / VOLUME_LOOKBACK
    curr = volumes[-1]
    return round(curr, 2), round(avg, 2), curr >= avg * VOLUME_MULTIPLIER

# ─── Main Signal Evaluator ────────────────────────────────────────────────────
def evaluate_signals(
    closes:  list[float],
    volumes: list[float],
    highs:   list[float],
    lows:    list[float],
) -> dict | None:
    """
    Signal fires when:
      ✅ Volume Spike confirmed  (mandatory)
      ✅ 2 out of 3 SMC agree   (BOS + CHoCH + FVG)
    """
    # ── Safety guard ──────────────────────────────────────────────────────────
    min_needed = SWING_LOOKBACK * 2 + 5
    if len(closes) < min_needed or len(highs) < min_needed:
        return None

    # ── Volume (mandatory) ───────────────────────────────────────────────────
    vol_r = calc_volume_spike(volumes)
    if vol_r is None:
        return None
    curr_v, avg_v, spike = vol_r
    if not spike:
        return None   # No volume → no signal

    # ── SMC ──────────────────────────────────────────────────────────────────
    swing_highs, swing_lows = detect_swings(highs, lows, SWING_LOOKBACK)

    bos   = calc_bos(closes, swing_highs, swing_lows)
    choch = calc_choch(swing_highs, swing_lows, closes)
    fvg   = calc_fvg(highs, lows)

    price = closes[-1]

    for direction in ("bullish", "bearish"):
        smc_checks = {
            "BOS":   bos   == direction,
            "CHoCH": choch == direction,
            "FVG":   fvg   == direction,
        }
        smc_score = sum(smc_checks.values())

        if smc_score >= MIN_SMC_CONDITIONS:
            d = direction.upper()
            return {
                "direction": d,
                "price":     round(price, 8),
                "bos":       bos,
                "choch":     choch,
                "fvg":       fvg,
                "curr_v":    curr_v,
                "avg_v":     avg_v,
                "vol_ratio": round(curr_v / avg_v, 2) if avg_v else 0,
                "checks": {
                    "volume": "✅",
                    "bos":    "✅" if smc_checks["BOS"]   else "❌",
                    "choch":  "✅" if smc_checks["CHoCH"] else "❌",
                    "fvg":    "✅" if smc_checks["FVG"]   else "❌",
                },
                "smc_score": smc_score,
            }

    return None

# ─── Exchange: Probe ──────────────────────────────────────────────────────────
async def probe_exchanges(session: aiohttp.ClientSession) -> dict | None:
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
                log.warning(f"⚠️  {ex['name']}: HTTP {resp.status}")
        except Exception as e:
            log.warning(f"⚠️  {ex['name']}: {str(e)[:60]}")
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
                return []
            data = await resp.json(content_type=None)
            t = ex["type"]
            if t in ("binance", "binance_futures"):
                return [
                    s["symbol"] for s in data.get("symbols", [])
                    if s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"
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

# ─── Exchange: Fetch Klines (returns closes, volumes, highs, lows) ────────────
async def fetch_klines(
    session:  aiohttp.ClientSession,
    ex:       dict,
    symbol:   str,
    interval: str,
) -> tuple[list[float], list[float], list[float], list[float]] | None:
    """Returns (closes, volumes, highs, lows)"""
    t = ex["type"]

    if t in ("binance", "binance_futures"):
        params = {"symbol": symbol, "interval": interval, "limit": KLINES_LIMIT}
    elif t == "kucoin":
        base   = symbol.replace("USDT", "")
        ku_sym = f"{base}-USDT"
        tf_map = {"1h": "1hour", "4h": "4hour"}
        params = {"symbol": ku_sym, "type": tf_map.get(interval, "1hour")}
    elif t == "bybit":
        tf_map = {"1h": "60", "4h": "240"}
        params = {"category": "spot", "symbol": symbol, "interval": tf_map.get(interval, "60"), "limit": KLINES_LIMIT}
    elif t == "gate":
        base   = symbol.replace("USDT", "")
        tf_map = {"1h": "1h", "4h": "4h"}
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
            if resp.status != 200:
                return None
            raw = await resp.json(content_type=None)

            closes  = []
            volumes = []
            highs   = []
            lows    = []

            if t in ("binance", "binance_futures"):
                # [open_time, open, high, low, close, volume, ...]
                for c in raw:
                    highs.append(float(c[2]))
                    lows.append(float(c[3]))
                    closes.append(float(c[4]))
                    volumes.append(float(c[5]))

            elif t == "kucoin":
                # [time, open, close, high, low, vol, turnover] newest first
                rows = list(reversed(raw.get("data", [])))
                for r in rows:
                    highs.append(float(r[3]))
                    lows.append(float(r[4]))
                    closes.append(float(r[2]))
                    volumes.append(float(r[5]))

            elif t == "bybit":
                # [time, open, high, low, close, volume] newest first
                rows = list(reversed(raw.get("result", {}).get("list", [])))
                for r in rows:
                    highs.append(float(r[2]))
                    lows.append(float(r[3]))
                    closes.append(float(r[4]))
                    volumes.append(float(r[5]))

            elif t == "gate":
                # {"t", "o", "h", "l", "c", "v"}
                for c in raw:
                    highs.append(float(c["h"]))
                    lows.append(float(c["l"]))
                    closes.append(float(c["c"]))
                    volumes.append(float(c["v"]))

            min_needed = SWING_LOOKBACK * 2 + 5
            if len(closes) < min_needed:
                return None
            return closes, volumes, highs, lows

    except asyncio.TimeoutError:
        return None
    except Exception:
        return None

# ─── Telegram ─────────────────────────────────────────────────────────────────
async def send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[NO TG] {text[:120]}")
        return False
    try:
        async with session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"Telegram: {e}")
        return False

def build_message(symbol: str, tf: str, sig: dict, exchange_name: str) -> str:
    d   = sig["direction"]
    emj = "🟢" if d == "BULLISH" else "🔴"
    arr = "📈" if d == "BULLISH" else "📉"
    c   = sig["checks"]
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{emj} <b>SMC SIGNAL — {d}</b> {arr}\n\n"
        f"🪙 <b>Coin:</b> <code>{symbol}</code>\n"
        f"⏱ <b>Timeframe:</b> {tf.upper()}\n"
        f"💰 <b>Price:</b> {sig['price']}\n"
        f"🏦 <b>Exchange:</b> {exchange_name}\n\n"
        f"<b>━━ Signal Breakdown ━━</b>\n"
        f"{c['volume']}  Volume Spike: <b>{sig['vol_ratio']}× avg</b> 🔥\n"
        f"{c['bos']}    BOS  (Break of Structure)\n"
        f"{c['choch']}  CHoCH (Trend Change)\n"
        f"{c['fvg']}    FVG  (Fair Value Gap)\n\n"
        f"📊 SMC Score: <b>{sig['smc_score']}/3</b>\n"
        f"⏰ {now}\n"
        f"#SMC #{symbol} #{tf.upper()} #{d}"
    )

# ─── Worker ───────────────────────────────────────────────────────────────────
async def process_symbol(
    session: aiohttp.ClientSession,
    ex:      dict,
    symbol:  str,
    sem:     asyncio.Semaphore,
) -> list[dict]:
    alerts = []
    async with sem:
        for tf in TIMEFRAMES:
            result = await fetch_klines(session, ex, symbol, tf)
            if result is None:
                continue
            closes, volumes, highs, lows = result
            sig = evaluate_signals(closes, volumes, highs, lows)
            if sig and _tracker.should_alert(symbol, tf, sig["direction"]):
                sig.update({"symbol": symbol, "tf": tf})
                alerts.append(sig)
                log.info(
                    f"🔔 {symbol} [{tf}] {sig['direction']} | "
                    f"SMC={sig['smc_score']}/3 Vol={sig['vol_ratio']}x"
                )
    return alerts

# ─── Scan ─────────────────────────────────────────────────────────────────────
async def run_scan():
    global _active_exchange
    start = time.monotonic()
    log.info("═" * 65)
    log.info("🚀 SMC + Volume Scan starting...")

    connector = aiohttp.TCPConnector(limit=200, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        ex = _active_exchange
        if ex is None:
            ex = await probe_exchanges(session)
        if ex is None:
            log.error("No working exchange — retrying next scan")
            return

        symbols = await fetch_symbols(session, ex)
        if not symbols:
            log.warning("0 symbols — re-probing next scan")
            _active_exchange = None
            return

        log.info(f"Exchange: {ex['name']} | Symbols: {len(symbols)} | TFs: {TIMEFRAMES}")

        sem        = asyncio.Semaphore(MAX_CONCURRENT)
        all_alerts = []
        BATCH      = 300

        for i in range(0, len(symbols), BATCH):
            chunk   = symbols[i: i + BATCH]
            tasks   = [process_symbol(session, ex, s, sem) for s in chunk]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    all_alerts.extend(r)
            log.info(f"Progress: {min(i+BATCH, len(symbols))}/{len(symbols)} | Signals so far: {len(all_alerts)}")

        if all_alerts:
            log.info(f"🔔 Sending {len(all_alerts)} alerts...")
            for a in all_alerts:
                msg = build_message(a["symbol"], a["tf"], a, ex["name"])
                await send_telegram(session, msg)
                await asyncio.sleep(0.35)
        else:
            log.info("✅ No signals this scan (volume+SMC didn't align)")

        _tracker.cleanup()
        log.info(f"✅ Done in {time.monotonic()-start:.1f}s | {len(all_alerts)} signals sent")
        log.info("═" * 65)

# ─── Startup Message ──────────────────────────────────────────────────────────
async def send_startup(session: aiohttp.ClientSession, ex_name: str):
    msg = (
        f"✅ <b>SMC + Volume Bot Started!</b>\n\n"
        f"🏦 <b>Exchange:</b> {ex_name}\n\n"
        f"<b>━━ Strategy ━━</b>\n"
        f"🔊 Volume Spike: {VOLUME_MULTIPLIER}× avg  ← mandatory\n"
        f"🔥 BOS  (Break of Structure)\n"
        f"🔄 CHoCH (Trend Change)\n"
        f"⚡ FVG  (Fair Value Gap)\n\n"
        f"📊 Signal = Volume + 2/3 SMC agree\n"
        f"⏱ Timeframes: 1H + 4H\n"
        f"🔄 Scan every: {SCAN_INTERVAL_MIN} min\n\n"
        f"Bot is live! 🚀"
    )
    await send_telegram(session, msg)

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    global _active_exchange
    log.info("SMC + Volume Bot — Initializing")

    async with aiohttp.ClientSession() as s:
        ex = await probe_exchanges(s)
        if ex:
            await send_startup(s, ex["name"])
        else:
            log.error("No exchange on startup — will retry each scan")

    while True:
        try:
            await run_scan()
        except Exception as e:
            log.error(f"Scan crashed (auto-retry): {e}", exc_info=True)
            _active_exchange = None
        log.info(f"💤 Sleeping {SCAN_INTERVAL_MIN} min...")
        await asyncio.sleep(SCAN_INTERVAL_MIN * 60)

if __name__ == "__main__":
    asyncio.run(main())
