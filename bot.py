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
CHAT_ID = os.environ["CHAT_ID"]
DAILY_HOUR = int(os.environ.get("DAILY_HOUR", "8"))   # hora UTC del informe diario
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Madrid")


def format_message(result: dict) -> str:
    decision_emoji = {
        "COMPRAR": "🟢",
        "VENDER": "🔴",
        "MANTENER": "🟡",
        "ESPERAR": "⏳",
    }.get(result["decision"], "❓")

    adx_label = "💪 Fuerte" if result["adx"] > 25 else "😴 Lateral"
    price_vs_sma = "📈 Por encima" if result["price"] > result["sma200"] else "📉 Por debajo"
    cur = result.get("currency", "EUR")
    ticker = result.get("ticker", "ETHC.DE")
    data_date = result.get("data_date", "—")

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 *{ticker} — Trend-Sustainer*",
        f"🕐 {result['timestamp']}",
        f"📅 Datos del cierre: `{data_date}`",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"💰 *Precio ETF:* `{cur} {result['price']:,.4f}`",
        f"📏 *SMA 200:* `{cur} {result['sma200']:,.4f}` — {price_vs_sma}",
        f"📡 *Fuerza ADX:* `{result['adx']:.1f}` — {adx_label}",
        f"🔼 *Donchian High:* `{cur} {result['donchian_high']:,.4f}`",
        f"🔽 *Donchian Low:* `{cur} {result['donchian_low']:,.4f}`",
        f"📉 *ATR 14:* `{cur} {result['atr']:,.4f}`",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"{decision_emoji} *DECISIÓN: {result['decision']}*",
        f"💬 *Por qué:* {result['reason']}",
        f"🛑 *Stop Loss:* `{cur} {result['stop_loss']:,.4f}`",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if result.get("position_open"):
        lines.append(f"\n📌 *Posición abierta desde:* `{cur} {result['entry_price']:,.4f}`")
        pnl_pct = ((result["price"] - result["entry_price"]) / result["entry_price"]) * 100
        pnl_emoji = "🤑" if pnl_pct > 0 else "😬"
        lines.append(f"{pnl_emoji} *PnL no realizado:* `{pnl_pct:+.2f}%`")

    return "\n".join(lines)


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /estado — devuelve el análisis en el momento."""
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
        f"• Informe diario automático a las {DAILY_HOUR}:00 UTC\n\n"
        "Usa /estado para empezar.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Job que se ejecuta cada día a la hora configurada.
    Se omite silenciosamente en fines de semana y festivos (mercado cerrado).
    """
    logger.info("Ejecutando informe diario...")
    try:
        result = await asyncio.to_thread(run_analysis)
        msg = "🌅 *Informe Diario — ETHC.DE*\n\n" + format_message(result)
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except MercadoCerradoError as e:
        # Fin de semana o festivo: no enviamos nada, solo log
        logger.info(f"Informe omitido: {e}")
    except Exception as e:
        logger.exception("Error en informe diario")
        await context.bot.send_message(chat_id=CHAT_ID, text=f"❌ Error en informe diario: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("estado", cmd_estado))

    # Informe diario usando JobQueue
    tz = pytz.timezone(TIMEZONE)
    report_time = time(hour=DAILY_HOUR, minute=0, tzinfo=tz)
    app.job_queue.run_daily(daily_report, time=report_time, name="daily_report")

    logger.info(f"Bot iniciado. Informe diario a las {DAILY_HOUR}:00 {TIMEZONE}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
