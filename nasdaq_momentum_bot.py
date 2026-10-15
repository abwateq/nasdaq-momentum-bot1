"""
فلتر الأسهم الأمريكية (Screener) - يرسل النتائج إلى تلجرام
----------------------------------------------------------
الفلاتر (على الشموع اليومية):
    1. الأداء: ارتفاع خلال أسبوع (آخر إغلاق أعلى من إغلاق قبل 5 أيام تداول)
    2. السعر أعلى من المتوسط المتحرك 20
    3. السعر أعلى من المتوسط المتحرك 50
    4. تسجيل أعلى سعر جديد خلال 20 يومًا
    5. الحجم النسبي (RVOL) أعلى من 1
    6. حجم التداول أكثر من 500,000
    7. الأسهم الحرة للتداول (Float): أقل من 100 مليون سهم (قابل للتغيير، انظر FLOAT_MODE)
    8. السعر أقل من 20 دولار (قابل للتغيير عبر MAX_PRICE)

التثبيت:
    pip install requests pandas yfinance

متغيرات البيئة المطلوبة:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY

متغيرات اختيارية (كلها لها قيم افتراضية):
    FLOAT_MODE        max = أقل من الحد (افتراضي) | min = أكبر من الحد | off = بدون فلتر
    FLOAT_LIMIT       الحد بالأسهم (افتراضي 100000000)
    MIN_VOLUME        أقل حجم تداول (افتراضي 500000)
    SCREEN_TIMES      أوقات التشغيل اليومية بتوقيت نيويورك، مفصولة بفاصلة (افتراضي 16:30)
    RUN_ON_START      true/false: تشغيل فلتر فور بدء البوت (افتراضي true)
    EXCHANGES         البورصات (افتراضي NASDAQ,NYSE,AMEX)
    MIN_PRICE / MAX_PRICE   حدود السعر (MAX_PRICE افتراضي 20، والقيمة 0 = بدون حد)
    NEW_HIGH_BASIS    high = أعلى سعر خلال اليوم (افتراضي) | close = سعر الإغلاق
    INCLUDE_NEWS      true/false: إضافة آخر خبر لكل سهم (افتراضي false)
    MAX_RESULTS       أقصى عدد رسائل أسهم في التشغيل الواحد (افتراضي 40)
    ALPACA_FEED       sip (افتراضي، ويتراجع تلقائيًا إلى iex إذا لم يُسمح به) | iex
"""

import os
import re
import time
import html
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf

try:
    yf.set_tz_cache_location("/tmp/py-yfinance-cache")
except Exception:
    pass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ===================== الإعدادات =====================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ALPACA_KEY_ID = os.getenv("ALPACA_API_KEY_ID", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY", "")
ALPACA_PAPER = _env_bool("ALPACA_PAPER", True)
ALPACA_FEED_PREFERRED = os.getenv("ALPACA_FEED", "sip").strip().lower()

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
ALPACA_TRADING_BASE = ("https://paper-api.alpaca.markets" if ALPACA_PAPER
                        else "https://api.alpaca.markets")
ALPACA_ASSETS_URL = f"{ALPACA_TRADING_BASE}/v2/assets"

# الأسهم الأمريكية: البورصات المشمولة (بدون OTC. وبدون ARCA لتفادي صناديق ETF)
EXCHANGES = {e.strip().upper()
             for e in os.getenv("EXCHANGES", "NASDAQ,NYSE,AMEX").split(",") if e.strip()}

# ---------- الفلاتر ----------
WEEK_PERF_MIN_PCT = 0.0                        # الأداء الأسبوعي أكبر من هذه النسبة %
SMA_SHORT = 20                                 # المتوسط القصير
SMA_LONG = 50                                  # المتوسط الطويل
HIGH_DAYS = 20                                 # قمة جديدة خلال كم يوم
NEW_HIGH_BASIS = os.getenv("NEW_HIGH_BASIS", "high").strip().lower()   # high | close
RVOL_MIN = 1.0                                 # الحجم النسبي أكبر من
RVOL_DAYS = 20                                 # متوسط الحجم لكم يوم سابق
MIN_VOLUME = int(_env_float("MIN_VOLUME", 500_000))   # حجم التداول أكثر من
FLOAT_MODE = os.getenv("FLOAT_MODE", "max").strip().lower()   # max | min | off
FLOAT_LIMIT = int(_env_float("FLOAT_LIMIT", 100_000_000))
MIN_PRICE = _env_float("MIN_PRICE", 0.0)       # 0 = بدون حد أدنى
MAX_PRICE = _env_float("MAX_PRICE", 20.0)      # السعر أقل من هذا (0 = بدون حد أعلى)
# -----------------------------

HISTORY_DAYS = 130             # أيام تقويمية من التاريخ (تكفي لـ 50 يوم تداول وأكثر)
BATCH_SIZE = 100               # عدد الأسهم في كل طلب
MAX_REQUESTS_PER_MINUTE = 170  # نبقى تحت حد الخطة المجانية (200)
MIN_REQUEST_INTERVAL = 60.0 / MAX_REQUESTS_PER_MINUTE
FLOAT_LOOKUP_LIMIT = 300       # أقصى عدد أسهم نبحث لها عن Float في التشغيل الواحد
FLOAT_CACHE_DAYS = 7           # لا نعيد جلب Float لنفس السهم قبل هذه المدة
MAX_RESULTS = int(_env_float("MAX_RESULTS", 40))   # أقصى عدد رسائل أسهم في التشغيل الواحد
TELEGRAM_DELAY_SECONDS = 3.1   # فاصل بين الرسائل (حد تلجرام ~20 رسالة/دقيقة للقنوات)
INCLUDE_NEWS = _env_bool("INCLUDE_NEWS", False)
NEWS_LOOKBACK_HOURS = 48
RUN_ON_START = _env_bool("RUN_ON_START", True)
SYMBOLS_REFRESH_HOURS = 24

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _parse_times(raw: str) -> list[dtime]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, m = part.split(":")
            out.append(dtime(int(h), int(m)))
        except ValueError:
            pass
    return out or [dtime(16, 30)]


SCREEN_TIMES = _parse_times(os.getenv("SCREEN_TIMES", "16:30"))
# =====================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("screener-bot")


# ---------------------------- تلجرام ----------------------------
def _telegram_retry_after(r) -> float:
    try:
        return float(r.json().get("parameters", {}).get("retry_after", 5))
    except Exception:
        return 5.0


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=data, timeout=15)
        if r.status_code == 429:  # تلجرام يطلب التمهل (كثرة الرسائل)
            wait = _telegram_retry_after(r)
            log.warning("Telegram 429: أنتظر %.0f ثانية ثم أعيد الإرسال.", wait)
            time.sleep(wait + 1)
            r = requests.post(url, data=data, timeout=15)
        if r.status_code == 400 and "parse entities" in r.text:
            log.warning("Telegram رفض تنسيق HTML، سأعيد الإرسال كنص عادي.")
            plain = html.unescape(re.sub(r"</?b>", "", text))
            r = requests.post(
                url,
                data={"chat_id": CHAT_ID, "text": plain, "disable_web_page_preview": True},
                timeout=15,
            )
        if not r.ok:
            log.warning("Telegram error: %s", r.text)
            if r.status_code == 403:
                log.warning("تأكد أن TELEGRAM_CHAT_ID صحيح وأن البوت مشرف في القناة "
                            "(أو أنك أرسلت له رسالة إذا كانت محادثة خاصة).")
    except requests.RequestException as e:
        log.warning("Telegram request failed: %s", e)


def _format_number(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


# ---------------------------- Alpaca ----------------------------
def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": ALPACA_KEY_ID,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


_last_request_time = 0.0


def _alpaca_get(url: str, params: dict, raw_errors: bool = False):
    """طلب GET لـ Alpaca مع احترام حد الطلبات وإعادة محاولة عند 429.
    raw_errors=True: يرجع الرد حتى لو كان خطأ (ليفحصه المستدعي)."""
    global _last_request_time
    for _ in range(5):
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

        if not r.ok and not raw_errors:
            log.warning("خطأ Alpaca (%d): %s", r.status_code, r.text[:300])
            return None
        return r
    return None


_symbols_cache: dict = {"symbols": [], "fetched_at": None}


def fetch_us_symbols() -> list[str]:
    """كل رموز الأسهم الأمريكية القابلة للتداول في البورصات المحددة (تخزين مؤقت يومي)."""
    now = datetime.now()
    fetched_at = _symbols_cache["fetched_at"]
    if (fetched_at and _symbols_cache["symbols"]
            and (now - fetched_at).total_seconds() < SYMBOLS_REFRESH_HOURS * 3600):
        return _symbols_cache["symbols"]

    r = _alpaca_get(ALPACA_ASSETS_URL, {"status": "active", "asset_class": "us_equity"})
    if r is None:
        log.warning(
            "تعذر جلب قائمة الأسهم من %s. إذا كان الخطأ 401/403 تأكد أن ALPACA_PAPER "
            "يطابق نوع مفاتيحك (true لمفاتيح Paper، false لمفاتيح Live).",
            ALPACA_ASSETS_URL)
        return _symbols_cache["symbols"]

    symbols = []
    for a in r.json():
        if a.get("exchange", "").upper() not in EXCHANGES:
            continue
        if not a.get("tradable"):
            continue
        sym = a.get("symbol", "")
        # تجاهل الرموز غير القياسية (تفضيلية/وارنتس/وحدات)
        if not sym.isalpha() or len(sym) > 5:
            continue
        symbols.append(sym)

    if symbols:
        _symbols_cache["symbols"] = symbols
        _symbols_cache["fetched_at"] = now
        log.info("قائمة الأسهم: %d رمز من %s", len(symbols), ", ".join(sorted(EXCHANGES)))
    return symbols


_feed = ALPACA_FEED_PREFERRED if ALPACA_FEED_PREFERRED in ("sip", "iex") else "sip"


def _fetch_daily_batch(symbols: list[str]) -> dict:
    """يجلب الشموع اليومية لمجموعة أسهم. يجرب SIP أولًا ثم يتراجع إلى IEX عند الرفض."""
    global _feed
    start = (datetime.now(UTC) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for _attempt in range(2):
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start,
            "limit": 10000,
            "feed": _feed,
            "adjustment": "split",
            "sort": "asc",
        }
        if _feed == "sip":
            # الخطة المجانية لا تسمح باستعلام بيانات SIP الأحدث من 15 دقيقة
            params["end"] = (datetime.now(UTC) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")

        all_bars: dict[str, list] = {s: [] for s in symbols}
        page_token = None
        switched = False

        for _page in range(10):
            if page_token:
                params["page_token"] = page_token
            r = _alpaca_get(ALPACA_DATA_URL, params, raw_errors=True)
            if r is None:
                return all_bars
            if not r.ok:
                if _feed == "sip" and r.status_code in (400, 401, 403, 422):
                    log.warning("Alpaca رفض بيانات SIP (%d): %s", r.status_code, r.text[:200])
                    log.warning("سأتحول إلى بيانات IEX. تنبيه: حجم IEX أقل بكثير من الحجم الحقيقي.")
                    _feed = "iex"
                    switched = True
                    break
                log.warning("خطأ Alpaca (%d): %s", r.status_code, r.text[:300])
                return all_bars

            data = r.json()
            for sym, bar_list in (data.get("bars") or {}).items():
                all_bars.setdefault(sym, []).extend(bar_list)
            page_token = data.get("next_page_token")
            if not page_token:
                break

        if not switched:
            return all_bars
    return {}


def _to_df(bars: list) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df = df.set_index("t").sort_index()
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_news(symbol: str) -> dict | None:
    params = {"symbols": symbol, "limit": 3, "sort": "desc", "include_content": "false"}
    r = _alpaca_get(ALPACA_NEWS_URL, params)
    if r is None:
        return None
    items = (r.json() or {}).get("news", [])
    if not items:
        return None
    latest = items[0]
    try:
        published = pd.to_datetime(latest["created_at"], utc=True).to_pydatetime()
    except (KeyError, ValueError):
        return None
    if (datetime.now(UTC) - published).total_seconds() / 3600 > NEWS_LOOKBACK_HOURS:
        return None
    return {"headline": latest.get("headline", "").strip(), "source": latest.get("source", "")}


# ---------------------------- Float (yfinance) ----------------------------
_float_cache: dict[str, tuple[float, datetime]] = {}


def get_float(symbol: str) -> float | None:
    """عدد الأسهم الحرة للتداول. يُستدعى فقط للأسهم التي اجتازت بقية الفلاتر."""
    cached = _float_cache.get(symbol)
    if cached and (datetime.now() - cached[1]) < timedelta(days=FLOAT_CACHE_DAYS):
        return cached[0]
    try:
        info = yf.Ticker(symbol).get_info()
        value = info.get("floatShares")
        if value:
            value = float(value)
            _float_cache[symbol] = (value, datetime.now())
            return value
    except Exception as e:
        log.warning("تعذر جلب Float لـ %s: %s", symbol, e)
    return None


# ---------------------------- منطق الفلاتر ----------------------------
def evaluate(symbol: str, df: pd.DataFrame) -> dict | None:
    """يطبق فلاتر الشموع اليومية. يرجع dict بالتفاصيل إذا اجتاز كل الفلاتر، وإلا None.
    (فلتر Float يُطبق لاحقًا لأنه يحتاج جلبًا من مصدر آخر)"""
    df = df.dropna()
    min_bars = max(SMA_LONG, HIGH_DAYS + 1, RVOL_DAYS + 1, 6)
    if len(df) < min_bars:
        return None

    last = df.iloc[-1]
    close = df["Close"]
    price = float(last["Close"])
    volume = float(last["Volume"])

    if MIN_PRICE and price < MIN_PRICE:
        return None
    if MAX_PRICE and price >= MAX_PRICE:
        return None

    # 6) حجم التداول أكثر من الحد
    if volume <= MIN_VOLUME:
        return None

    # 1) الأداء: ارتفاع خلال أسبوع (5 أيام تداول)
    week_ago = float(close.iloc[-6])
    if week_ago <= 0:
        return None
    week_perf = (price / week_ago - 1) * 100
    if week_perf <= WEEK_PERF_MIN_PCT:
        return None

    # 2) السعر أعلى من SMA20
    sma20 = float(close.iloc[-SMA_SHORT:].mean())
    if price <= sma20:
        return None

    # 3) السعر أعلى من SMA50
    sma50 = float(close.iloc[-SMA_LONG:].mean())
    if price <= sma50:
        return None

    # 4) أعلى سعر جديد خلال 20 يومًا (مقارنة بأعلى سعر في الـ 20 يومًا السابقة)
    prior_high = float(df["High"].iloc[-(HIGH_DAYS + 1):-1].max())
    test_price = price if NEW_HIGH_BASIS == "close" else float(last["High"])
    if test_price < prior_high:
        return None

    # 5) الحجم النسبي أعلى من 1 (مقارنة بمتوسط الأيام السابقة)
    avg_vol = float(df["Volume"].iloc[-(RVOL_DAYS + 1):-1].mean())
    if avg_vol <= 0:
        return None
    rvol = volume / avg_vol
    if rvol <= RVOL_MIN:
        return None

    return {
        "symbol": symbol,
        "price": price,
        "week_perf": week_perf,
        "sma20": sma20,
        "sma50": sma50,
        "rvol": rvol,
        "volume": volume,
        "last_date": df.index[-1].tz_convert(NY).date(),
        "float": None,
    }


def apply_float_filter(candidates: list[dict]) -> tuple[list[dict], int, bool]:
    """يطبق فلتر Float. يرجع (الأسهم المقبولة، عدد من تعذر معرفة Float له، هل قُطعت القائمة)."""
    if FLOAT_MODE == "off":
        return candidates, 0, False

    kept: list[dict] = []
    unknown = 0
    truncated = False
    for idx, c in enumerate(candidates):
        if idx >= FLOAT_LOOKUP_LIMIT:
            truncated = True
            break
        fl = get_float(c["symbol"])
        c["float"] = fl
        if fl is None:
            unknown += 1
            continue
        if FLOAT_MODE == "max" and fl >= FLOAT_LIMIT:
            continue
        if FLOAT_MODE == "min" and fl <= FLOAT_LIMIT:
            continue
        kept.append(c)
        time.sleep(0.2)
    return kept, unknown, truncated


# ---------------------------- الرسائل ----------------------------
def _format_stock_message(s: dict) -> str:
    """رسالة مستقلة لكل سهم."""
    fl = _format_number(s["float"]) if s.get("float") else "غير متوفر"
    msg = (
        f"📊 <b>${s['symbol']}</b>\n"
        f"السعر: <b>{s['price']:.2f}$</b>\n"
        f"الأداء الأسبوعي: <b>{s['week_perf']:+.1f}%</b>\n"
        f"الحجم النسبي: <b>{s['rvol']:.1f}x</b>\n"
        f"حجم التداول: <b>{_format_number(s['volume'])}</b>\n"
        f"الأسهم الحرة (Float): <b>{fl}</b>"
    )
    if _feed == "iex":
        msg += "\n⚠️ الحجم من بيانات IEX فقط (أقل من الحقيقي)"
    if INCLUDE_NEWS:
        news = fetch_news(s["symbol"])
        if news and news.get("headline"):
            src = f" ({html.escape(news['source'])})" if news.get("source") else ""
            msg += f"\n\n📰{src} {html.escape(news['headline'])}"
        else:
            msg += "\n\n📰 لا توجد أخبار"
    return msg


def send_results(final: list[dict], analyzed: int, unknown_float: int,
                 truncated: bool, last_date) -> None:
    """يرسل كل سهم مطابق في رسالة منفصلة. التفاصيل الإضافية تُكتب في الـ Logs."""
    log.info("آخر شمعة يومية: %s | فُحص %d سهم | مطابق: %d", last_date, analyzed, len(final))
    if _feed == "iex":
        log.warning("البيانات من IEX فقط: حجم التداول أقل من الحقيقي وقد يستبعد أسهمًا مؤهلة.")
    if unknown_float:
        log.info("استُبعد %d سهم لتعذر معرفة Float له.", unknown_float)
    if truncated:
        log.info("فُحص Float لأول %d سهم فقط (مرتبة حسب RVOL).", FLOAT_LOOKUP_LIMIT)

    if not final:
        send_telegram("لا توجد أسهم تطابق الفلاتر.")
        return

    shown = final[:MAX_RESULTS]
    for s in shown:
        send_telegram(_format_stock_message(s))
        time.sleep(TELEGRAM_DELAY_SECONDS)   # تلجرام يحدد عدد الرسائل في الدقيقة
    if len(final) > len(shown):
        send_telegram(f"ℹ️ يوجد {len(final) - len(shown)} سهم مطابق إضافي لم يُرسل (الحد الأقصى {MAX_RESULTS} رسالة).")


# ---------------------------- التشغيل ----------------------------
def run_screen() -> None:
    started = time.time()
    symbols = fetch_us_symbols()
    if not symbols:
        send_telegram("⚠️ تعذر جلب قائمة الأسهم من Alpaca. راجع مفاتيح API وإعداد ALPACA_PAPER.")
        return

    log.info("بدء الفلتر على %d سهم...", len(symbols))
    passed: list[dict] = []
    analyzed = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        raw = _fetch_daily_batch(batch)
        for sym, bars in raw.items():
            if not bars:
                continue
            analyzed += 1
            res = evaluate(sym, _to_df(bars))
            if res:
                passed.append(res)
        if (i // BATCH_SIZE) % 10 == 0:
            log.info("تقدم: %d/%d | مطابق حتى الآن: %d",
                     min(i + BATCH_SIZE, len(symbols)), len(symbols), len(passed))

    if analyzed == 0:
        send_telegram("⚠️ لم أحصل على أي بيانات أسعار من Alpaca. راجع الـ Logs.")
        return

    passed.sort(key=lambda s: s["rvol"], reverse=True)
    log.info("اجتاز فلاتر الأسعار: %d سهم. جاري فلتر Float...", len(passed))
    final, unknown_float, truncated = apply_float_filter(passed)
    last_date = max((s["last_date"] for s in final), default=None) or \
                max((s["last_date"] for s in passed), default=None)

    send_results(final, analyzed, unknown_float, truncated, last_date)
    log.info("انتهى الفلتر في %.0f ثانية | النتيجة: %d سهم", time.time() - started, len(final))


def run_screen_safe() -> None:
    try:
        run_screen()
    except Exception as e:  # لا نوقف البوت بسبب خطأ عابر
        log.exception("خطأ أثناء الفلتر: %s", e)
        send_telegram("⚠️ حدث خطأ أثناء تشغيل الفلتر. راجع الـ Logs في Render.")


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في متغيرات البيئة")
    if not ALPACA_KEY_ID or not ALPACA_SECRET_KEY:
        raise SystemExit("ضع ALPACA_API_KEY_ID و ALPACA_API_SECRET_KEY في متغيرات البيئة")

    send_telegram("فلتر الأسهم الأمريكية")

    ran: set = set()
    if RUN_ON_START:
        run_screen_safe()
        # لا نكرر أوقات اليوم التي مرّت بالفعل
        now = datetime.now(NY)
        for slot in SCREEN_TIMES:
            if now.time() >= slot:
                ran.add((now.date(), slot))

    while True:
        now = datetime.now(NY)
        if now.weekday() < 5:
            for slot in SCREEN_TIMES:
                key = (now.date(), slot)
                slot_dt = datetime.combine(now.date(), slot, tzinfo=NY)
                if key not in ran and slot_dt <= now < slot_dt + timedelta(hours=3):
                    ran.add(key)
                    run_screen_safe()
        time.sleep(30)


if __name__ == "__main__":
    main()
