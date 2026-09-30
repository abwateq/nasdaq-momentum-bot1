"""
بوت تلجرام لتنبيهك عند وجود زخم (Momentum) على أسهم ناسداك
مصدر البيانات: Alpaca Market Data API (الخطة المجانية - بيانات IEX اللحظية)
------------------------------------------------------------
التثبيت:
    pip install requests pandas

الإعداد (متغيرات بيئة):
    export TELEGRAM_BOT_TOKEN="123456:ABC..."     # من @BotFather
    export TELEGRAM_CHAT_ID="123456789"           # رقمك من @userinfobot
    export ALPACA_API_KEY_ID="PK..."              # من لوحة Alpaca
    export ALPACA_API_SECRET_KEY="..."            # من لوحة Alpaca

التشغيل:
    python nasdaq_momentum_bot.py

منطق الزخم (على شموع 5 دقائق):
    - حجم التداول النسبي (RVOL): حجم آخر شمعة / متوسط آخر 20 شمعة
    - تغير السعر خلال آخر 15 دقيقة (3 شموع)
    - اختراق أعلى/أقل سعر لآخر 20 شمعة
    يُرسل التنبيه إذا تحقق شرط الحجم + شرط السعر معًا.

ملاحظة: الخطة المجانية من Alpaca تعطي بيانات من بورصة IEX فقط (وليس كل
البورصات)، لذلك أرقام الحجم أقل من الحجم الفعلي الكلي في السوق، لكنها
تبقى مفيدة لحساب "الحجم النسبي" (نسبة إلى متوسط نفس المصدر).
"""

import os
import time
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
import pandas as pd

# ===================== الإعدادات =====================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ALPACA_KEY_ID = os.getenv("ALPACA_API_KEY_ID", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY", "")
ALPACA_FEED = "iex"   # الخطة المجانية: iex فقط. الخطة المدفوعة: sip
ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
ALPACA_TRADING_BASE = ("https://paper-api.alpaca.markets" if ALPACA_PAPER
                        else "https://api.alpaca.markets")
ALPACA_ASSETS_URL = f"{ALPACA_TRADING_BASE}/v2/assets"

# فحص كل سوق ناسداك تلقائيًا بدل قائمة يدوية (True/False)
SCAN_FULL_MARKET = os.getenv("SCAN_FULL_MARKET", "true").lower() == "true"
SYMBOLS_REFRESH_HOURS = 24     # كل كم ساعة يُحدَّث سجل رموز ناسداك من Alpaca

CHECK_EVERY_SECONDS = 300      # فحص كل 5 دقائق
RVOL_MIN = 3.0                 # الحجم لازم يكون 3 أضعاف المتوسط أو أكثر
PRICE_CHANGE_MIN = 1.0         # حركة السعر % خلال آخر 15 دقيقة
LOOKBACK_BARS = 20             # عدد الشموع لحساب المتوسط والاختراق
MIN_PRICE = 2.0                # تجاهل الأسهم الأرخص من هذا السعر
COOLDOWN_MINUTES = 60          # لا تكرر تنبيه نفس السهم قبل هذه المدة
BATCH_SIZE = 100               # عدد الأسهم في كل طلب لـ Alpaca

# حماية من تجاوز حد الخطة المجانية (200 طلب/دقيقة) - نبقى تحته بهامش أمان
MAX_REQUESTS_PER_MINUTE = 170
MIN_REQUEST_INTERVAL = 60.0 / MAX_REQUESTS_PER_MINUTE

# قائمة احتياطية تُستخدم فقط إذا SCAN_FULL_MARKET=False (تغطي تقريبًا ناسداك 100 + أسهم نشطة)
FALLBACK_WATCHLIST = [
    # ناسداك 100 (تقريبًا)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "COST",
    "NFLX", "AMD", "ADBE", "PEP", "CSCO", "TMUS", "INTC", "QCOM", "INTU", "AMAT",
    "TXN", "CMCSA", "AMGN", "HON", "BKNG", "ISRG", "SBUX", "GILD", "ADI", "VRTX",
    "MU", "LRCX", "PANW", "REGN", "MDLZ", "KLAC", "SNPS", "CDNS", "ASML", "PYPL",
    "MELI", "CRWD", "ABNB", "MAR", "ORLY", "FTNT", "MRVL", "ADSK", "WDAY", "DASH",
    "TEAM", "CTAS", "PCAR", "PAYX", "ROP", "NXPI", "MNST", "CHTR", "KDP", "AEP",
    "ODFL", "FAST", "EA", "EXC", "CSGP", "XEL", "CCEP", "CTSH", "DXCM", "BIIB",
    "IDXX", "ON", "GEHC", "VRSK", "FANG", "ANSS", "ZS", "TTD", "CDW", "DDOG",
    "ROST", "BKR", "GFS", "WBD", "ILMN", "MRNA", "LULU", "WBA", "SIRI", "EBAY",
    "ALGN", "ENPH", "SGEN", "JD", "PDD",
    # أسهم نشطة إضافية خارج المؤشر
    "PLTR", "ARM", "COIN", "SMCI", "MSTR", "APP", "RIVN", "LCID", "SOFI", "AFRM",
    "UPST", "RIOT", "MARA", "CVNA", "DKNG", "RBLX", "NIO", "SNOW", "NET", "SHOP",
]
# =====================================================

NY = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("momentum-bot")

last_alert: dict[str, datetime] = {}


def market_is_open() -> bool:
    now = datetime.now(NY)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            log.warning("Telegram error: %s", r.text)
            if r.status_code == 403:
                log.warning("تأكد أن TELEGRAM_CHAT_ID هو رقمك أنت (من @userinfobot) "
                            "وليس رقم البوت، وأنك أرسلت رسالة للبوت أولًا.")
    except requests.RequestException as e:
        log.warning("Telegram request failed: %s", e)


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": ALPACA_KEY_ID,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


_last_request_time = 0.0


def _alpaca_get(url: str, params: dict):
    """طلب GET لـ Alpaca مع احترام حد الطلبات بالدقيقة وإعادة محاولة عند 429."""
    global _last_request_time
    for attempt in range(5):
        wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(url, headers=_alpaca_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            log.warning("فشل طلب Alpaca: %s", e)
            time.sleep(2)
            continue
        _last_request_time = time.time()

        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", 2))
            log.warning("تجاوزت حد الطلبات (429)، أنتظر %.1f ثانية...", retry_after)
            time.sleep(retry_after)
            continue

        if not r.ok:
            log.warning("خطأ Alpaca (%d): %s", r.status_code, r.text[:300])
            return None

        return r
    return None


_symbols_cache: dict = {"symbols": [], "fetched_at": None}


def fetch_nasdaq_symbols() -> list[str]:
    """يجلب كل رموز ناسداك القابلة للتداول من Alpaca، مع تخزين مؤقت يوميًا."""
    now = datetime.now()
    if (_symbols_cache["fetched_at"]
            and (now - _symbols_cache["fetched_at"]).total_seconds() < SYMBOLS_REFRESH_HOURS * 3600
            and _symbols_cache["symbols"]):
        return _symbols_cache["symbols"]

    params = {"status": "active", "asset_class": "us_equity"}
    r = _alpaca_get(ALPACA_ASSETS_URL, params)
    if r is None:
        log.warning(
            "تعذر جلب قائمة رموز ناسداك من %s. إذا كان الخطأ 401/403، تأكد أن "
            "ALPACA_PAPER يطابق نوع مفاتيحك (true لمفاتيح Paper، false لمفاتيح Live). "
            "سأستخدم آخر نسخة محفوظة أو القائمة الاحتياطية الآن.",
            ALPACA_ASSETS_URL,
        )
        return _symbols_cache["symbols"] or FALLBACK_WATCHLIST

    assets = r.json()
    symbols = []
    for a in assets:
        if a.get("exchange") != "NASDAQ":
            continue
        if not a.get("tradable"):
            continue
        sym = a.get("symbol", "")
        # تجاهل الرموز غير القياسية (تفضيلية/وارنتس/وحدات) لتقليل الضجيج
        if not sym.isalpha() or len(sym) > 5:
            continue
        symbols.append(sym)

    if not symbols:
        log.warning("قائمة الرموز الراجعة من Alpaca فارغة، سأستخدم القائمة الاحتياطية")
        return FALLBACK_WATCHLIST

    _symbols_cache["symbols"] = symbols
    _symbols_cache["fetched_at"] = now
    log.info("تم تحديث قائمة رموز ناسداك: %d رمز", len(symbols))
    return symbols


def _fetch_batch(symbols: list[str]) -> dict:
    """يجلب شموع 5 دقائق لمجموعة أسهم من Alpaca، مع تصفّح next_page_token."""
    start = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "5Min",
        "start": start,
        "limit": 10000,
        "feed": ALPACA_FEED,
        "adjustment": "raw",
        "sort": "asc",
    }

    all_bars: dict[str, list] = {s: [] for s in symbols}
    page_token = None

    for _ in range(10):  # حماية من حلقة لا نهائية
        if page_token:
            params["page_token"] = page_token

        r = _alpaca_get(ALPACA_DATA_URL, params)
        if r is None:
            break

        data = r.json()
        bars = data.get("bars", {}) or {}
        for sym, bar_list in bars.items():
            all_bars.setdefault(sym, []).extend(bar_list)

        page_token = data.get("next_page_token")
        if not page_token:
            break

    return all_bars


def fetch_data() -> dict:
    """يجلب بيانات كل أسهم القائمة من Alpaca على دفعات، ويعيد dict: symbol -> DataFrame."""
    watchlist = fetch_nasdaq_symbols() if SCAN_FULL_MARKET else FALLBACK_WATCHLIST
    frames: dict[str, pd.DataFrame] = {}

    for i in range(0, len(watchlist), BATCH_SIZE):
        batch = watchlist[i:i + BATCH_SIZE]
        raw = _fetch_batch(batch)

        for sym, bar_list in raw.items():
            if not bar_list:
                continue
            df = pd.DataFrame(bar_list)
            if df.empty:
                continue
            df["t"] = pd.to_datetime(df["t"], utc=True)
            df = df.rename(columns={
                "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume",
            }).set_index("t").sort_index()
            frames[sym] = df[["Open", "High", "Low", "Close", "Volume"]]

    log.info("تم جلب بيانات %d/%d رمز", len(frames), len(watchlist))
    return frames


def analyze(symbol: str, df: pd.DataFrame):
    """يرجع dict فيه تفاصيل الزخم أو None."""
    df = df.dropna()
    if len(df) < LOOKBACK_BARS + 5:
        return None

    # تجاهل الشمعة الأخيرة إذا لم تكتمل بعد
    last_ts = df.index[-1].to_pydatetime()
    if (datetime.now(NY) - last_ts).total_seconds() < 300:
        df = df.iloc[:-1]
        if len(df) < LOOKBACK_BARS + 5:
            return None

    last = df.iloc[-1]
    prev = df.iloc[-1 - LOOKBACK_BARS:-1]
    price = float(last["Close"])
    if price < MIN_PRICE:
        return None

    avg_vol = float(prev["Volume"].mean())
    if avg_vol <= 0:
        return None
    rvol = float(last["Volume"]) / avg_vol

    price_3_bars_ago = float(df.iloc[-4]["Close"])
    change_pct = (price / price_3_bars_ago - 1) * 100

    breakout_up = price > float(prev["High"].max())
    breakout_down = price < float(prev["Low"].min())

    if rvol >= RVOL_MIN and change_pct >= PRICE_CHANGE_MIN:
        return {
            "symbol": symbol,
            "price": price,
            "rvol": rvol,
            "change": change_pct,
            "breakout_up": breakout_up,
            "breakout_down": breakout_down,
        }
    return None


def format_alert(s: dict) -> str:
    extra = "\n💥 اختراق أعلى سعر لآخر 20 شمعة" if s["breakout_up"] else ""
    return (
        f"🚀 <b>زخم صاعد على ${s['symbol']}</b>\n"
        f"السعر: <b>{s['price']:.2f}$</b>\n"
        f"التغير (15 دقيقة): <b>{s['change']:+.2f}%</b>\n"
        f"الحجم النسبي (IEX): <b>{s['rvol']:.1f}x</b>"
        f"{extra}\n"
        f"https://finance.yahoo.com/quote/{s['symbol']}"
    )


def scan_once() -> None:
    data = fetch_data()
    if not data:
        log.info("لا توجد بيانات")
        return

    now = datetime.now()
    found = 0
    for symbol, df in data.items():
        result = analyze(symbol, df)
        if not result:
            continue

        # منع التكرار
        last = last_alert.get(symbol)
        if last and (now - last).total_seconds() < COOLDOWN_MINUTES * 60:
            continue

        send_telegram(format_alert(result))
        last_alert[symbol] = now
        found += 1
        log.info("تنبيه: %s", symbol)

    log.info("انتهى الفحص - تنبيهات جديدة: %d", found)


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في متغيرات البيئة")
    if not ALPACA_KEY_ID or not ALPACA_SECRET_KEY:
        raise SystemExit("ضع ALPACA_API_KEY_ID و ALPACA_API_SECRET_KEY في متغيرات البيئة")

    mode = "كل سوق ناسداك" if SCAN_FULL_MARKET else f"{len(FALLBACK_WATCHLIST)} سهمًا محددًا"
    send_telegram(f"✅ بوت زخم ناسداك اشتغل (مصدر البيانات: Alpaca/IEX، الفحص: {mode}). "
                  "سأنبهك عند ظهور زخم على الأسهم.")
    while True:
        try:
            if market_is_open():
                scan_once()
            else:
                log.info("السوق مغلق")
        except Exception as e:  # لا نوقف البوت بسبب خطأ عابر
            log.exception("خطأ أثناء الفحص: %s", e)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
