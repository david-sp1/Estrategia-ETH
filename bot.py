import os
import asyncio
import logging
from datetime import datetime, time
import pytz

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from strategy import run_analysis, MercadoCerradoError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
DAILY_HOUR     = int(os.environ.get("DAILY_HOUR", "8"))
TIMEZONE       = os.environ.get("TIMEZONE", "Europe/Madrid")


# ── Formato del mensaje ───────────────────────────────────────────────────────

def format_message(result: dict) -> str:
    decision_emoji = {
        "COMPRAR":  "🟢",
        "VENDER":   "🔴",
        "MANTENER": "🟡",
        "ESPERAR":  "⏳",
    }.get(result["decision"], "❓")

    idx = result["index"]
    etf = result["etf"]
    cur = idx["currency"]

    adx_label    = "💪 Fuerte" if idx["adx"] > 25 else "😴 Lateral"
    price_vs_sma = "📈 Por encima" if idx["price"] > idx["sma200"] else "📉 Por debajo"

    lines = []

    # ── Cabecera ──────────────────────────────────────────────────────────────
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 *Trend-Sustainer ETH*",
        f"🕐 {result['timestamp']}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"",
    ]

    # ── Bloque 1: Índice ETH-EUR ───────────────────────────────────────────────
    lines += [
        f"📈 *{idx['ticker']} — Índice*",
        f"📅 Cierre: `{idx['data_date']}`",
        f"",
        f"💰 *Precio:*          `{cur} {idx['price']:,.2f}`",
        f"📏 *SMA 200:*         `{cur} {idx['sma200']:,.2f}` — {price_vs_sma}",
        f"📡 *ADX:*             `{idx['adx']:.1f}` — {adx_label}",
        f"🔼 *Donchian High:*   `{cur} {idx['donchian_high']:,.2f}`",
        f"🔽 *Donchian Low:*    `{cur} {idx['donchian_low']:,.2f}`",
        f"📉 *ATR 14:*          `{cur} {idx['atr']:,.2f}`",
        f"🛑 *Stop Loss idx:*   `{cur} {idx['stop_loss']:,.2f}`",
    ]

    if result["position_open"] and idx.get("entry_price"):
        pnl_pct = ((idx["price"] - idx["entry_price"]) / idx["entry_price"]) * 100
        pnl_emoji = "🤑" if pnl_pct > 0 else "😬"
        lines += [
            f"📌 *Entrada (índice):* `{cur} {idx['entry_price']:,.2f}`",
            f"{pnl_emoji} *PnL no realizado:*  `{pnl_pct:+.2f}%`",
        ]

    lines.append("")

    # ── Bloque 2: ETF ETHC.DE ─────────────────────────────────────────────────
    lines.append(f"──────────────────────")
    lines.append(f"📦 *{etf['ticker']} — ETF*")

    if etf["ok"]:
        lines += [
            f"📅 Cierre: `{etf['data_date']}`",
            f"💰 *Precio ETF:*      `{cur} {etf['price']:,.4f}`",
            f"🛑 *Stop Loss ETF:*   `{cur} {etf['stop_loss']:,.4f}`",
        ]
    else:
        stop_txt = (f"`{cur} {etf['stop_loss']:,.4f}` _(estimado)_"
                    if etf["stop_loss"] else "—")
        lines += [
            f"⚠️ _Datos no disponibles en este momento_",
            f"🛑 *Stop Loss ETF:*   {stop_txt}",
        ]

    lines.append("")

    # ── Decisión ──────────────────────────────────────────────────────────────
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"{decision_emoji} *DECISIÓN: {result['decision']}*",
        f"💬 *Por qué:* {result['reason']}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analizando mercado, un momento...")
    try:
        result = await asyncio.to_thread(run_analysis)
        await update.message.reply_text(format_message(result), parse_mode="Markdown")
    except MercadoCerradoError as e:
        await update.message.reply_text(
            f"🏖️ *Mercado cerrado*\n\n{e}\n\nPrueba de lunes a viernes en horario Xetra.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("Error en /estado")
        await update.message.reply_text(f"❌ Error al analizar: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *ETH Trend-Sustainer Bot*\n\n"
        "Comandos disponibles:\n"
        "• /estado — análisis completo ahora mismo\n"
        f"• Informe diario automático a las {DAILY_HOUR}:00 {TIMEZONE}\n\n"
        "Fuentes: ETH-EUR (índice) + ETHC.DE (ETF)",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Ejecutando informe diario...")
    try:
        result = await asyncio.to_thread(run_analysis)
        msg = "🌅 *Informe Diario — Trend-Sustainer*\n\n" + format_message(result)
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except MercadoCerradoError as e:
        logger.info(f"Informe omitido: {e}")
    except Exception as e:
        logger.exception("Error en informe diario")
        await context.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error en informe diario: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("estado", cmd_estado))

    tz          = pytz.timezone(TIMEZONE)
    report_time = time(hour=DAILY_HOUR, minute=0, tzinfo=tz)
    app.job_queue.run_daily(daily_report, time=report_time, name="daily_report")

    logger.info(f"Bot iniciado. Informe diario a las {DAILY_HOUR}:00 {TIMEZONE}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
