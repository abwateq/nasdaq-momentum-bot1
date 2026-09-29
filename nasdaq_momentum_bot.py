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

CHECK_EVERY_SECONDS = 300      # فحص كل 5 دقائق
RVOL_MIN = 3.0                 # الحجم لازم يكون 3 أضعاف المتوسط أو أكثر
PRICE_CHANGE_MIN = 1.0         # حركة السعر % خلال آخر 15 دقيقة
LOOKBACK_BARS = 20             # عدد الشموع لحساب المتوسط والاختراق
MIN_PRICE = 2.0                # تجاهل الأسهم الأرخص من هذا السعر
COOLDOWN_MINUTES = 60          # لا تكرر تنبيه نفس السهم قبل هذه المدة
BATCH_SIZE = 50                # عدد الأسهم في كل طلب لـ Alpaca

# قائمة المراقبة: تغطي تقريبًا كل مكوّنات ناسداك 100 + أسهم نشطة شائعة - عدّلها كما تشاء
WATCHLIST = [
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
        try:
            r = requests.get(ALPACA_DATA_URL, headers=_alpaca_headers(),
                              params=params, timeout=30)
        except requests.RequestException as e:
            log.warning("فشل طلب Alpaca: %s", e)
            break

        if not r.ok:
            log.warning("خطأ Alpaca (%d): %s", r.status_code, r.text[:300])
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
    """يجلب بيانات كل الأسهم من Alpaca على دفعات، ويعيد dict: symbol -> DataFrame."""
    frames: dict[str, pd.DataFrame] = {}

    for i in range(0, len(WATCHLIST), BATCH_SIZE):
        batch = WATCHLIST[i:i + BATCH_SIZE]
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

    missing = [s for s in WATCHLIST if s not in frames]
    if missing:
        log.info("لا توجد بيانات لـ: %s", ", ".join(missing))
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

    if rvol >= RVOL_MIN and abs(change_pct) >= PRICE_CHANGE_MIN:
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
    up = s["change"] > 0
    icon = "🚀" if up else "🔻"
    direction = "صاعد" if up else "هابط"
    extra = ""
    if s["breakout_up"]:
        extra = "\n💥 اختراق أعلى سعر لآخر 20 شمعة"
    elif s["breakout_down"]:
        extra = "\n⚠️ كسر أدنى سعر لآخر 20 شمعة"
    return (
        f"{icon} <b>زخم {direction} على ${s['symbol']}</b>\n"
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

    send_telegram("✅ بوت زخم ناسداك اشتغل (مصدر البيانات: Alpaca/IEX). "
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
