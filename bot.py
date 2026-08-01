"""
ربات تلگرام بورس ایران - نسخه تک‌فایلی (با BrsApi)

نصب پیش‌نیازها:
    pip install python-telegram-bot aiohttp

اجرا:
    1) BOT_TOKEN و BRSAPI_KEY را در متغیر محیطی قرار دهید (یا مستقیم در کد پایین).
    2) python bot.py

⚠️ توکن‌ها را هرگز داخل کد به اشتراک نگذارید یا در گیت‌هاب پابلیک قرار ندهید.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

import aiohttp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ==================== تنظیمات ====================

TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# کلید رایگان BrsApi را از این صفحه بگیرید:
# https://brsapi.ir/tsetmc-exchange-free-bourse-api-key-request/
BRSAPI_KEY = os.getenv("BRSAPI_KEY", "")
BRSAPI_URL = "BRSAPI_URL = "https://Api.BrsApi.ir/Tsetmc/AllSymbols.php"

DB_PATH = "watchlist.db"

# ==================== دیتابیس ====================


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            chat_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            UNIQUE(chat_id, symbol)
        )
        """
    )
    conn.commit()
    conn.close()


def add_to_watchlist(chat_id: int, symbol: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO watchlist (chat_id, symbol) VALUES (?, ?)",
            (chat_id, symbol),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_from_watchlist(chat_id: int, symbol: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "DELETE FROM watchlist WHERE chat_id = ? AND symbol = ?", (chat_id, symbol)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_watchlist(chat_id: int) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT symbol FROM watchlist WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ==================== فرمت‌بندی ====================

_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_EN_TO_FA = str.maketrans("0123456789", _FA_DIGITS)
_FA_TO_EN = str.maketrans(_FA_DIGITS, "0123456789")


def fa_digits(text) -> str:
    return str(text).translate(_EN_TO_FA)


def fmt_number(value: float, decimals: int = 0) -> str:
    try:
        return fa_digits(f"{float(value):,.{decimals}f}")
    except (TypeError, ValueError):
        return fa_digits(str(value))


def fmt_percent(value: float) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    arrow = "🔺" if value > 0 else ("🔻" if value < 0 else "➖")
    sign = "+" if value > 0 else ""
    return f"{arrow} {fa_digits(f'{sign}{value:.2f}')}٪"


def fmt_toman(value: float) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    return f"{fmt_number(value / 10)} تومان"


def safe_float(text: str) -> float | None:
    try:
        cleaned = text.translate(_FA_TO_EN).replace(",", "").strip()
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


# ==================== داده بورس (BrsApi) ====================


class MarketDataError(RuntimeError):
    pass


TIMEOUT = aiohttp.ClientTimeout(total=12)

_cache: dict = {"data": None, "ts": 0.0}
_CACHE_TTL_SECONDS = 20


async def fetch_all_symbols() -> list[dict]:
    """
    کل دیتای لحظه‌ای بازار را از BrsApi می‌گیرد (با کش کوتاه‌مدت برای
    جلوگیری از ارسال درخواست‌های تکراری و زیاد).
    """
    if not BRSAPI_KEY:
        raise MarketDataError(
            "کلید BRSAPI_KEY تنظیم نشده. از brsapi.ir یک کلید رایگان بگیرید و "
            "در Variables ربات قرار دهید."
        )

    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["data"]

    url = f"{BRSAPI_URL}?key={BRSAPI_KEY}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"خطا در ارتباط با سرور داده بورس: {exc}") from exc

    if not isinstance(data, list):
        raise MarketDataError("پاسخ سرور داده بورس نامعتبر بود.")

    _cache["data"] = data
    _cache["ts"] = now
    return data


async def search_symbol(query: str) -> list[dict]:
    """جستجو در بین همه نمادها بر اساس نماد (l18) یا نام شرکت (l30)."""
    all_symbols = await fetch_all_symbols()
    query = query.strip()
    if not query:
        return []

    matches = [
        r
        for r in all_symbols
        if query in str(r.get("l18", "")) or query in str(r.get("l30", ""))
    ]
    return matches[:15]


async def get_symbol_by_ticker(symbol: str) -> dict:
    """رکورد کامل یک نماد را با تطبیق دقیق l18 برمی‌گرداند."""
    all_symbols = await fetch_all_symbols()
    for r in all_symbols:
        if str(r.get("l18", "")) == symbol:
            return r
    raise MarketDataError("داده‌ای برای این نماد یافت نشد.")


def summary_sentence(symbol: str, change_percent: float) -> str:
    try:
        change_percent = float(change_percent)
    except (TypeError, ValueError):
        change_percent = 0.0
    if change_percent > 2:
        trend = "با تقاضای مناسب و رشد مثبت"
    elif change_percent > 0:
        trend = "با رشد جزئی"
    elif change_percent < -2:
        trend = "با عرضه سنگین و افت قابل توجه"
    elif change_percent < 0:
        trend = "با افت جزئی"
    else:
        trend = "بدون تغییر محسوس"
    return f"امروز نماد {symbol} {trend} معامله می‌شود."


def format_symbol_message(r: dict) -> str:
    symbol = str(r.get("l18", "-"))
    name = str(r.get("l30", "-"))
    last_price = r.get("pl", 0)
    close_price = r.get("pc", 0)
    change_percent = r.get("pcp", 0)
    volume = r.get("tvol", 0)
    value = r.get("tval", 0)
    trade_count = r.get("tno", 0)
    min_price = r.get("tmin", 0)
    max_price = r.get("tmax", 0)

    return (
        f"📌 *{name}* ({symbol})\n\n"
        f"آخرین قیمت: {fmt_toman(last_price)}\n"
        f"قیمت پایانی: {fmt_toman(close_price)}\n"
        f"درصد تغییر: {fmt_percent(change_percent)}\n"
        f"حجم معاملات: {fmt_number(volume)}\n"
        f"ارزش معاملات: {fmt_toman(value)}\n"
        f"تعداد معاملات: {fmt_number(trade_count)}\n"
        f"دامنه نوسان: {fmt_toman(min_price)} تا {fmt_toman(max_price)}\n\n"
        f"💬 {summary_sentence(symbol, change_percent)}"
    )


def symbol_actions_keyboard(symbol: str, in_watchlist: bool) -> InlineKeyboardMarkup:
    wl_button = (
        InlineKeyboardButton("➖ حذف از واچ‌لیست", callback_data=f"wl_remove:{symbol}")
        if in_watchlist
        else InlineKeyboardButton("➕ افزودن به واچ‌لیست", callback_data=f"wl_add:{symbol}")
    )
    return InlineKeyboardMarkup([[wl_button]])


# ==================== منوی اصلی ====================

BTN_SEARCH = "🔍 جستجوی نماد / قیمت لحظه‌ای"
BTN_WATCHLIST = "⭐ واچ‌لیست من"
BTN_CALCULATOR = "🧮 ماشین‌حساب سود و زیان"
BTN_EDUCATION = "📖 آموزش بورس"
BTN_ABOUT = "ℹ️ درباره ربات"


def main_menu() -> ReplyKeyboardMarkup:
    layout = [
        [KeyboardButton(BTN_SEARCH)],
        [KeyboardButton(BTN_WATCHLIST), KeyboardButton(BTN_CALCULATOR)],
        [KeyboardButton(BTN_EDUCATION), KeyboardButton(BTN_ABOUT)],
    ]
    return ReplyKeyboardMarkup(layout, resize_keyboard=True)


# ==================== هندلرها: شروع ====================


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "کاربر"
    await update.message.reply_text(
        f"👋 سلام {name} عزیز!\n\n"
        "به ربات بورس ایران خوش آمدید. از منوی زیر استفاده کنید 👇",
        reply_markup=main_menu(),
    )


async def about_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ این ربات برای رصد لحظه‌ای بازار سرمایه ایران طراحی شده است.\n\n"
        "⚠️ اطلاعات ارائه‌شده صرفاً جنبه اطلاع‌رسانی دارد و توصیه سرمایه‌گذاری "
        "محسوب نمی‌شود."
    )


# ==================== هندلرها: جستجو ====================

WAITING_SYMBOL = 1


async def prompt_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🔍 نام شرکت یا نماد را وارد کنید (مثال: فولاد):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_SYMBOL


async def handle_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = (update.message.text or "").strip()
    try:
        results = await search_symbol(query)
    except MarketDataError as exc:
        await update.message.reply_text(f"⚠️ {exc}", reply_markup=main_menu())
        return ConversationHandler.END

    if not results:
        await update.message.reply_text(
            "چیزی پیدا نشد. دوباره تلاش کنید یا /cancel را بزنید."
        )
        return WAITING_SYMBOL

    if len(results) == 1:
        await send_symbol_detail(update, results[0])
        return ConversationHandler.END

    buttons = [
        [
            InlineKeyboardButton(
                f"{r.get('l18')} - {r.get('l30')}", callback_data=f"sym:{r.get('l18')}"
            )
        ]
        for r in results[:10]
    ]
    await update.message.reply_text(
        "چند نتیجه پیدا شد، یکی را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


async def symbol_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]
    try:
        record = await get_symbol_by_ticker(symbol)
    except MarketDataError as exc:
        await query.edit_message_text(f"⚠️ {exc}")
        return
    await send_symbol_detail(update, record, edit=True)


async def send_symbol_detail(update: Update, record: dict, edit: bool = False) -> None:
    chat_id = update.effective_chat.id
    symbol = str(record.get("l18", ""))
    in_watchlist = symbol in get_watchlist(chat_id)
    msg = format_symbol_message(record)
    kb = symbol_actions_keyboard(symbol, in_watchlist)

    if edit:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.effective_message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        await update.effective_message.reply_text("منوی اصلی:", reply_markup=main_menu())


async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("جستجو لغو شد.", reply_markup=main_menu())
    return ConversationHandler.END


# ==================== هندلرها: واچ‌لیست ====================


async def show_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    symbols = get_watchlist(chat_id)
    if not symbols:
        await update.message.reply_text(
            "⭐ واچ‌لیست شما خالی است.\n"
            "ابتدا از «🔍 جستجوی نماد» یک نماد را پیدا و به واچ‌لیست اضافه کنید."
        )
        return

    await update.message.reply_text(f"⭐ واچ‌لیست شما ({len(symbols)} نماد):")
    for symbol in symbols:
        try:
            record = await get_symbol_by_ticker(symbol)
            text = f"{symbol} — {fmt_percent(record.get('pcp', 0))}"
        except MarketDataError:
            text = f"{symbol} — ⚠️ خطا در دریافت قیمت"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"❌ حذف {symbol}", callback_data=f"wl_remove:{symbol}")]]
        )
        await update.message.reply_text(text, reply_markup=kb)


async def watchlist_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    symbol = query.data.split(":", 1)[1]
    added = add_to_watchlist(update.effective_chat.id, symbol)
    await query.answer("✅ اضافه شد." if added else "قبلاً در واچ‌لیست بود.")


async def watchlist_remove_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    symbol = query.data.split(":", 1)[1]
    removed = remove_from_watchlist(update.effective_chat.id, symbol)
    await query.answer("🗑 حذف شد." if removed else "در واچ‌لیست نبود.")
    if removed:
        try:
            await query.edit_message_text(f"❌ {symbol} از واچ‌لیست حذف شد.")
        except Exception:  # noqa: BLE001
            pass


# ==================== هندلرها: ماشین‌حساب سود و زیان ====================

ASK_BUY, ASK_SELL, ASK_QTY = range(2, 5)


async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "🧮 قیمت خرید هر سهم را به تومان وارد کنید:", reply_markup=ReplyKeyboardRemove()
    )
    return ASK_BUY


async def calc_ask_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    v = safe_float(update.message.text)
    if v is None or v <= 0:
        await update.message.reply_text("❗️عدد معتبر و بزرگ‌تر از صفر وارد کنید:")
        return ASK_BUY
    context.user_data["buy"] = v
    await update.message.reply_text("قیمت فروش هر سهم را به تومان وارد کنید:")
    return ASK_SELL


async def calc_ask_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    v = safe_float(update.message.text)
    if v is None or v < 0:
        await update.message.reply_text("❗️عدد معتبر وارد کنید:")
        return ASK_SELL
    context.user_data["sell"] = v
    await update.message.reply_text("تعداد سهم را وارد کنید:")
    return ASK_QTY


async def calc_show_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    v = safe_float(update.message.text)
    if v is None or v <= 0 or v != int(v):
        await update.message.reply_text("❗️تعداد سهم را به‌صورت عدد صحیح وارد کنید:")
        return ASK_QTY

    buy, sell, qty = context.user_data["buy"], context.user_data["sell"], int(v)
    buy_value = buy * qty
    sell_value = sell * qty
    pl = sell_value - buy_value
    pl_percent = (pl / buy_value) * 100
    emoji = "🟢" if pl >= 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} *نتیجه محاسبه*\n\n"
        f"ارزش خرید: {fmt_number(buy_value)} تومان\n"
        f"ارزش فروش: {fmt_number(sell_value)} تومان\n"
        f"سود/زیان کل: {fmt_number(pl)} تومان ({fmt_percent(pl_percent)})\n"
        f"سود/زیان هر سهم: {fmt_number(sell - buy)} تومان",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def calc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("محاسبه لغو شد.", reply_markup=main_menu())
    return ConversationHandler.END


# ==================== هندلرها: آموزش ====================

GLOSSARY = {
    "pe": ("P/E (نسبت قیمت به سود)", "نسبت قیمت روز سهم به سود سالانه هر سهم (EPS) است."),
    "eps": ("EPS (سود هر سهم)", "سودی که به ازای هر سهم شرکت طی یک دوره مالی به دست آمده است."),
    "queue_buy": ("صف خرید", "زمانی که تقاضا برای خرید سهم بسیار بیشتر از عرضه است و قیمت به سقف نوسان رسیده."),
    "queue_sell": ("صف فروش", "زمانی که عرضه سهم بسیار بیشتر از تقاضاست و قیمت به کف نوسان رسیده."),
    "base_volume": ("حجم مبنا", "حداقل تعداد سهم لازم برای جابه‌جایی در یک روز تا سهم بتواند کل دامنه نوسان را تغییر کند."),
    "price_range": ("دامنه نوسان", "محدوده مجاز تغییر قیمت یک سهم در یک روز معاملاتی نسبت به روز قبل."),
    "market_cap": ("ارزش بازار", "حاصل‌ضرب قیمت روز سهم در تعداد کل سهام منتشرشده شرکت."),
    "ipo": ("عرضه اولیه (IPO)", "نخستین باری که سهام یک شرکت برای عموم در بورس عرضه می‌شود."),
}


async def education_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buttons = [[InlineKeyboardButton(v[0], callback_data=f"edu:{k}")] for k, v in GLOSSARY.items()]
    await update.message.reply_text(
        "📖 یکی از اصطلاحات زیر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def education_topic_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    entry = GLOSSARY.get(key)
    if entry:
        await query.message.reply_text(f"📘 *{entry[0]}*\n\n{entry[1]}", parse_mode="Markdown")


# ==================== خطای سراسری ====================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"خطا: {context.error}")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ خطایی رخ داد. دوباره تلاش کنید یا /start را بزنید."
            )
        except Exception:  # noqa: BLE001
            pass


# ==================== ساخت اپلیکیشن ====================


def main() -> None:
    if not TOKEN or TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError(
            "توکن ربات تنظیم نشده. متغیر محیطی BOT_TOKEN را ست کنید یا مقدار TOKEN را در کد قرار دهید."
        )

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_ABOUT}$"), about_handler))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_WATCHLIST}$"), show_watchlist))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_EDUCATION}$"), education_menu))

    search_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_SEARCH}$"), prompt_search),
            CommandHandler("search", prompt_search),
        ],
        states={WAITING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_query)]},
        fallbacks=[CommandHandler("cancel", cancel_search)],
    )
    app.add_handler(search_conv)

    calc_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_CALCULATOR}$"), calc_start),
            CommandHandler("calc", calc_start),
        ],
        states={
            ASK_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_ask_sell)],
            ASK_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_ask_qty)],
            ASK_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_show_result)],
        },
        fallbacks=[CommandHandler("cancel", calc_cancel)],
    )
    app.add_handler(calc_conv)

    app.add_handler(CallbackQueryHandler(symbol_selected_callback, pattern=r"^sym:"))
    app.add_handler(CallbackQueryHandler(watchlist_add_cb, pattern=r"^wl_add:"))
    app.add_handler(CallbackQueryHandler(watchlist_remove_cb, pattern=r"^wl_remove:"))
    app.add_handler(CallbackQueryHandler(education_topic_cb, pattern=r"^edu:"))

    app.add_error_handler(error_handler)

    print("ربات در حال اجراست... (برای توقف Ctrl+C بزنید)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
    
