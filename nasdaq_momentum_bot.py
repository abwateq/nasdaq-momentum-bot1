"""
بوت تلجرام لتنبيهك عند وجود زخم (Momentum) على أسهم ناسداك
------------------------------------------------------------
التثبيت:
    pip install yfinance requests pandas

الإعداد (متغيرات بيئة):
    export TELEGRAM_BOT_TOKEN="123456:ABC..."   # من @BotFather
    export TELEGRAM_CHAT_ID="123456789"         # رقمك أو رقم القروب

التشغيل:
    python nasdaq_momentum_bot.py

منطق الزخم (على شموع 5 دقائق):
    - حجم التداول النسبي (RVOL): حجم آخر شمعة / متوسط آخر 20 شمعة
    - تغير السعر خلال آخر 15 دقيقة (3 شموع)
    - اختراق أعلى/أقل سعر لآخر 20 شمعة
    يُرسل التنبيه إذا تحقق شرط الحجم + شرط السعر معًا.
"""

import os
import time
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf

# ===================== الإعدادات =====================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_EVERY_SECONDS = 300      # فحص كل 5 دقائق
RVOL_MIN = 3.0                 # الحجم لازم يكون 3 أضعاف المتوسط أو أكثر
PRICE_CHANGE_MIN = 1.0         # حركة السعر % خلال آخر 15 دقيقة
LOOKBACK_BARS = 20             # عدد الشموع لحساب المتوسط والاختراق
MIN_PRICE = 2.0                # تجاهل الأسهم الأرخص من هذا السعر
COOLDOWN_MINUTES = 60          # لا تكرر تنبيه نفس السهم قبل هذه المدة

# قائمة المراقبة (أشهر أسهم ناسداك 100) - عدّلها كما تشاء
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "NFLX",
    "AMD", "ADBE", "PEP", "CSCO", "TMUS", "INTC", "QCOM", "INTU", "AMAT", "TXN",
    "CMCSA", "AMGN", "HON", "BKNG", "ISRG", "SBUX", "GILD", "ADI", "VRTX", "MU",
    "LRCX", "PANW", "REGN", "MDLZ", "KLAC", "SNPS", "CDNS", "ASML", "PYPL", "MELI",
    "CRWD", "ABNB", "MAR", "ORLY", "FTNT", "MRVL", "ADSK", "WDAY", "DASH", "TEAM",
    "PLTR", "ARM", "COIN", "SMCI", "MSTR", "APP", "DDOG", "ZS", "TTD", "LULU",
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
    except requests.RequestException as e:
        log.warning("Telegram request failed: %s", e)


def fetch_data() -> pd.DataFrame:
    return yf.download(
        tickers=WATCHLIST,
        period="2d",
        interval="5m",
        group_by="ticker",
        progress=False,
        threads=True,
        auto_adjust=True,
    )


def analyze(symbol: str, df: pd.DataFrame):
    """يرجع dict فيه تفاصيل الزخم أو None."""
    df = df.dropna()
    if len(df) < LOOKBACK_BARS + 5:
        return None

    # تجاهل الشمعة الأخيرة إذا لم تكتمل بعد
    last_ts = df.index[-1]
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize(NY)
    if (datetime.now(NY) - last_ts.to_pydatetime()).total_seconds() < 300:
        df = df.iloc[:-1]

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
        f"الحجم النسبي: <b>{s['rvol']:.1f}x</b>"
        f"{extra}\n"
        f"https://finance.yahoo.com/quote/{s['symbol']}"
    )


def scan_once() -> None:
    data = fetch_data()
    if data is None or data.empty:
        log.info("لا توجد بيانات")
        return

    now = datetime.now()
    found = 0
    for symbol in WATCHLIST:
        try:
            df = data[symbol]
        except KeyError:
            continue
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

    send_telegram("✅ بوت زخم ناسداك اشتغل. سأنبهك عند ظهور زخم على الأسهم.")
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
