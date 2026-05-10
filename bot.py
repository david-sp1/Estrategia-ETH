import os
import asyncio
import logging
from datetime import datetime, time
import pytz

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from strategy import run_all_assets, MercadoCerradoError, ASSETS

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
DAILY_HOUR     = int(os.environ.get("DAILY_HOUR", "8"))
TIMEZONE       = os.environ.get("TIMEZONE", "Europe/Madrid")

DECISION_EMOJI = {
    "COMPRAR":  "🟢",
    "VENDER":   "🔴",
    "MANTENER": "🟡",
    "ESPERAR":  "⏳",
    "ERROR":    "❌",
}

# Orden de prioridad para mostrar activos
ORDEN_DECISION = {"COMPRAR": 0, "VENDER": 1, "MANTENER": 2, "ESPERAR": 3, "ERROR": 4}


# ── Formato de un activo ──────────────────────────────────────────────────────

def format_asset(r: dict) -> str:
    if r["decision"] == "ERROR":
        return (
            f"🔷 *{r['name']} ({r['ticker']})*  ❌ *ERROR*\n"
            f"⚠️ _{r.get('error', 'Error desconocido')}_"
        )

    emoji = DECISION_EMOJI[r["decision"]]
    cur   = r["currency"]
    dec   = 4 if r["price"] < 100 else 2

    def fmt(v, d=None):
        d = d or dec
        return f"{v:,.{d}f}" if v is not None else "—"

    adx_label    = "💪 Fuerte" if r["adx"] > 25 else "😴 Lateral"
    price_vs_sma = "📈 Por encima" if r["price"] > r["sma200"] else "📉 Por debajo"

    lines = [
        f"🔷 *{r['name']} ({r['ticker']})*  {emoji} *{r['decision']}*",
        f"📅 Cierre: `{r['data_date']}`",
        f"",
        f"💰 Precio:        `{cur} {fmt(r['price'])}`",
        f"📏 SMA 200:       `{cur} {fmt(r['sma200'])}` — {price_vs_sma}",
        f"📡 ADX:           `{r['adx']:.1f}` — {adx_label}",
        f"🔼 Donchian High: `{cur} {fmt(r['donchian_high'])}`",
        f"🔽 Donchian Low:  `{cur} {fmt(r['donchian_low'])}`",
        f"📉 ATR 14:        `{cur} {fmt(r['atr'])}`",
        f"🛑 Stop Loss:     `{cur} {fmt(r['stop_loss'])}`",
    ]

    if r["position_open"] and r.get("entry_price"):
        pnl_pct   = (r["price"] - r["entry_price"]) / r["entry_price"] * 100
        pnl_emoji = "🤑" if pnl_pct > 0 else "😬"
        lines += [
            f"",
            f"📌 Entrada:       `{cur} {fmt(r['entry_price'])}`",
            f"{pnl_emoji} PnL:            `{pnl_pct:+.2f}%`",
        ]

    # Bloque ETF opcional
    etf = r.get("etf")
    if etf:
        ecur = etf["currency"]
        edec = 4 if (etf.get("price") or 0) < 100 else 2
        lines.append(f"")
        lines.append(f"📦 *{etf['ticker']} — ETF*")
        if etf["ok"]:
            lines += [
                f"💰 Precio ETF:    `{ecur} {etf['price']:,.{edec}f}`",
                f"🛑 Stop Loss ETF: `{ecur} {etf['stop_loss']:,.{edec}f}`",
            ]
        else:
            stop_txt = (f"`{ecur} {etf['stop_loss']:,.4f}` _(estimado)_"
                        if etf.get("stop_loss") else "—")
            lines += [
                f"⚠️ _Datos ETF no disponibles_",
                f"🛑 Stop Loss ETF: {stop_txt}",
            ]

    lines.append(f"")
    lines.append(f"💬 _{r['reason']}_")
    return "\n".join(lines)


# ── Mensaje completo ──────────────────────────────────────────────────────────

def format_full_report(results: list[dict], titulo: str) -> str:
    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    # Ordenar: COMPRAR → VENDER → MANTENER → ESPERAR → ERROR
    ordered = sorted(results, key=lambda r: ORDEN_DECISION.get(r["decision"], 9))

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 *{titulo}*",
        f"🕐 {now} · {TIMEZONE}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Resumen de señales activas (COMPRAR y VENDER únicamente)
    señales = [r for r in results if r["decision"] in ("COMPRAR", "VENDER")]
    if señales:
        lines += ["", "🚨 *SEÑALES ACTIVAS*", ""]
        for r in sorted(señales, key=lambda r: ORDEN_DECISION[r["decision"]]):
            emoji = DECISION_EMOJI[r["decision"]]
            lines.append(f"{emoji} *{r['decision']}* — {r['name']} ({r['ticker']})")
        lines.append("")
    else:
        lines += ["", "✅ _Sin señales de compra o venta hoy_", ""]

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    # Detalle de cada activo
    for i, r in enumerate(ordered):
        lines.append("")
        lines.append(format_asset(r))
        if i < len(ordered) - 1:
            lines += ["", "──────────────────────"]

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


def format_daily_report(results: list[dict]) -> str:
    """
    Informe diario: todos los activos, pero los que están en ESPERAR
    se muestran en versión compacta para no saturar.
    """
    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    ordered = sorted(results, key=lambda r: ORDEN_DECISION.get(r["decision"], 9))

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🌅 *Informe Diario — Trend-Sustainer*",
        f"🕐 {now} · {TIMEZONE}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Resumen de señales activas
    señales = [r for r in results if r["decision"] in ("COMPRAR", "VENDER")]
    if señales:
        lines += ["", "🚨 *SEÑALES ACTIVAS*", ""]
        for r in sorted(señales, key=lambda r: ORDEN_DECISION[r["decision"]]):
            emoji = DECISION_EMOJI[r["decision"]]
            lines.append(f"{emoji} *{r['decision']}* — {r['name']} ({r['ticker']})")
        lines.append("")
    else:
        lines += ["", "✅ _Sin señales de compra o venta hoy_", ""]

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    # Activos con señal activa: bloque completo
    activos = [r for r in ordered if r["decision"] != "ESPERAR"]
    espera  = [r for r in ordered if r["decision"] == "ESPERAR"]

    for i, r in enumerate(activos):
        lines.append("")
        lines.append(format_asset(r))
        if i < len(activos) - 1:
            lines += ["", "──────────────────────"]

    # Activos en ESPERAR: versión compacta
    if espera:
        if activos:
            lines += ["", "──────────────────────"]
        lines += ["", "⏳ *En espera (sin condiciones de entrada)*", ""]
        for r in espera:
            cur = r["currency"]
            dec = 4 if r["price"] < 100 else 2
            adx_label = "💪" if r["adx"] > 25 else "😴"
            vs_sma = "📈" if r["price"] > r["sma200"] else "📉"
            lines.append(
                f"• *{r['name']}* `{cur} {r['price']:,.{dec}f}` "
                f"SMA {vs_sma}  ADX {r['adx']:.0f} {adx_label}"
            )

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/estado — informe completo de todos los activos."""
    await update.message.reply_text("⏳ Analizando todos los activos...")
    try:
        results = await asyncio.to_thread(run_all_assets)
        msg = format_full_report(results, "Trend-Sustainer — Estado actual")
        await update.message.reply_text(msg, parse_mode="Markdown")
    except MercadoCerradoError as e:
        await update.message.reply_text(
            f"🏖️ *Mercado cerrado*\n\n{e}\n\nPrueba de lunes a viernes.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("Error en /estado")
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activos = ", ".join(f"{a['name']} ({a['ticker']})" for a in ASSETS)
    await update.message.reply_text(
        f"👋 *Trend-Sustainer Bot*\n\n"
        f"Activos monitorizados:\n{activos}\n\n"
        f"Comandos:\n"
        f"• /estado — análisis completo ahora mismo\n"
        f"• Informe diario automático a las {DAILY_HOUR}:00 ({TIMEZONE})",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Ejecutando informe diario...")
    try:
        results = await asyncio.to_thread(run_all_assets)
        msg = format_daily_report(results)
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except MercadoCerradoError as e:
        logger.info(f"Informe omitido: {e}")
    except Exception as e:
        logger.exception("Error en informe diario")
        await context.bot.send_message(
            chat_id=CHAT_ID, text=f"❌ Error en informe diario: {e}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("estado", cmd_estado))

    tz          = pytz.timezone(TIMEZONE)
    report_time = time(hour=DAILY_HOUR, minute=0, tzinfo=tz)
    app.job_queue.run_daily(daily_report, time=report_time, name="daily_report")

    logger.info(f"Bot iniciado. Activos: {[a['ticker'] for a in ASSETS]}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
