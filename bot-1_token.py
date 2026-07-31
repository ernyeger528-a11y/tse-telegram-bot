"""
ربات تلگرام بورس ایران - نسخه تک‌فایلی

نصب پیش‌نیازها:
    pip install python-telegram-bot aiohttp

اجرا:
    1) مقدار TOKEN را در پایین (یا متغیر محیطی BOT_TOKEN) قرار دهید.
    2) python bot.py

⚠️ توکن را هرگز داخل کد به اشتراک نگذارید یا در گیت‌هاب پابلیک قرار ندهید.
"""

from __future__ import annotations

import os
import sqlite3
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

# توکن را از متغیر محیطی بخوان، یا اینجا مستقیم قرار بده (فقط برای تست محلی).
TOKEN = os.getenv("BOT_TOKEN", "8722348219:AAEFPdVHY6G86nMYXspUnxQjpFnpBxGw_5g")

DB_PATH = "watchlist.db"
TSETMC_BASE_URL = "http://cdn.tsetmc.com"

# ==================== دیتابیس ====================


def init_db() -> None:
    """جدول واچ‌لیست را در صورت نبود می‌سازد."""
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
            (chat_id, symbol.upper()),
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
        "DELETE FROM watchlist WHERE chat_id = ? AND symbol = ?",
        (chat_id, symbol.upper()),
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
    return fa_digits(f"{value:,.{decimals}f}")


def fmt_percent(value: float) -> str:
    arrow = "🔺" if value > 0 else ("🔻" if value < 0 else "➖")
    sign = "+" if value > 0 else ""
    return f"{arrow} {fa_digits(f'{sign}{value:.2f}')}٪"


def fmt_toman(value: float) -> str:
    return f"{fmt_number(value / 10)} تومان"


def safe_float(text: str) -> float | None:
    try:
        cleaned = text.translate(_FA_TO_EN).replace(",", "").strip()
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


# ==================== داده بورس (TSETMC) ====================


@dataclass
class SymbolInfo:
    symbol: str
    name: str
    last_price: float
    close_price: float
    prev_close: float
    change_percent: float
    volume: int
    value: float
    trade_count: int
    min_price: float
    max_price: float

    @property
    def summary_sentence(self) -> str:
        if self.change_percent > 2:
            trend = "با تقاضای مناسب و رشد مثبت"
        elif self.change_percent > 0:
            trend = "با رشد جزئی"
        elif self.change_percent < -2:
            trend = "با عرضه سنگین و افت قابل توجه"
        elif self.change_percent < 0:
            trend = "با افت جزئی"
        else:
            trend = "بدون تغییر محسوس"
        return f"امروز نماد {self.symbol} {trend} معامله می‌شود."


class MarketDataError(RuntimeError):
    pass


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TSEBot/1.0)"}
TIMEOUT = aiohttp.ClientTimeout(total=10)


async def search_symbol(query: str) -> list[dict]:
    """جستجوی نماد بر اساس نام یا بخشی از آن."""
    url = f"{TSETMC_BASE_URL}/tsev2/data/search.aspx?skey={query}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS, timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                raw_text = await resp.text()
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"خطا در ارتباط با سرور داده بورس: {exc}") from exc

    results = []
    for line in raw_text.strip().split(";"):
        if not line:
            continue
        parts = line.split(",")
        if len(parts) >= 5:
            results.append({"ins_code": parts[0], "symbol": parts[1], "name": parts[2]})
    return results


async def get_symbol_info(ins_code: str) -> SymbolInfo:
    """اطلاعات لحظه‌ای یک نماد را با کد داخلی آن دریافت می‌کند."""
    url = f"{TSETMC_BASE_URL}/tsev2/data/instinfofast.aspx?i={ins_code}&c=۲۳"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS, timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                raw_text = await resp.text()
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"خطا در دریافت اطلاعات نماد: {exc}") from exc

    sections = raw_text.strip().split(";")
    if not sections or not sections[0]:
        raise MarketDataError("داده‌ای برای این نماد یافت نشد.")

    f = sections[0].split(",")
    try:
        close = float(f[6]) if len(f) > 6 else 0.0
        prev = float(f[7]) if len(f) > 7 else 0.0
        return SymbolInfo(
            symbol=f[2] if len(f) > 2 else "",
            name=f[3] if len(f) > 3 else "",
            last_price=float(f[5]) if len(f) > 5 else 0.0,
            close_price=close,
            prev_close=prev,
            change_percent=((close - prev) / prev * 100) if prev else 0.0,
            volume=int(float(f[8])) if len(f) > 8 else 0,
            value=float(f[9]) if len(f) > 9 else 0.0,
            trade_count=int(float(f[10])) if len(f) > 10 else 0,
            min_price=float(f[11]) if len(f) > 11 else 0.0,
            max_price=float(f[12]) if len(f) > 12 else 0.0,
        )
    except (ValueError, IndexError) as exc:
        raise MarketDataError("خطا در تجزیه داده نماد (ساختار پاسخ نامعتبر است).") from exc


def format_symbol_message(info: SymbolInfo) -> str:
    return (
        f"📌 *{info.name}* ({info.symbol})\n\n"
        f"آخرین قیمت: {fmt_toman(info.last_price)}\n"
        f"قیمت پایانی: {fmt_toman(info.close_price)}\n"
        f"درصد تغییر: {fmt_percent(info.change_percent)}\n"
        f"حجم معاملات: {fmt_number(info.volume)}\n"
        f"ارزش معاملات: {fmt_toman(info.value)}\n"
        f"تعداد معاملات: {fmt_number(info.trade_count)}\n"
        f"دامنه نوسان: {fmt_toman(info.min_price)} تا {fmt_toman(info.max_price)}\n\n"
        f"💬 {info.summary_sentence}"
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
        await send_symbol_detail(update, results[0]["ins_code"])
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(f"{r['symbol']} - {r['name']}", callback_data=f"sym:{r['ins_code']}")]
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
    ins_code = query.data.split(":", 1)[1]
    await send_symbol_detail(update, ins_code, edit=True)


async def send_symbol_detail(update: Update, ins_code: str, edit: bool = False) -> None:
    chat_id = update.effective_chat.id
    try:
        info = await get_symbol_info(ins_code)
    except MarketDataError as exc:
        text = f"⚠️ {exc}"
        if edit:
            await update.callback_query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text, reply_markup=main_menu())
        return

    in_watchlist = info.symbol.upper() in get_watchlist(chat_id)
    msg = format_symbol_message(info)
    kb = symbol_actions_keyboard(info.symbol, in_watchlist)

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
            results = await search_symbol(symbol)
            if not results:
                await update.message.reply_text(f"{symbol}: اطلاعاتی یافت نشد.")
                continue
            info = await get_symbol_info(results[0]["ins_code"])
            text = f"{info.symbol} — {fmt_percent(info.change_percent)}"
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
    if not TOKEN or TOKEN == "8722348219:AAEFPdVHY6G86nMYXspUnxQjpFnpBxGw_5g":
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
