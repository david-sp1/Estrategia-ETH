import os
import asyncio
import logging
from datetime import datetime, time
import pytz

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from strategy import run_analysis, MercadoCerradoError, ASSETS

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
DAILY_HOUR     = int(os.environ.get("DAILY_HOUR", "8"))
TIMEZONE       = os.environ.get("TIMEZONE", "Europe/Madrid")


# ── Helpers de formato ────────────────────────────────────────────────────────

def _dec(price: float) -> int:
    """Decimales según magnitud del precio."""
    if price is None:
        return 2
    if price < 10:    return 4
    if price < 1000:  return 2
    return 0


def _fmt(value: float, decimals: int = None) -> str:
    if value is None:
        return "—"
    d = decimals if decimals is not None else _dec(value)
    return f"{value:,.{d}f}"


def _pnl_emoji(pnl: float) -> str:
    return "🤑" if pnl > 0 else ("😬" if pnl < 0 else "➖")


# ── Bloque de posición activa (detalle completo) ──────────────────────────────

def format_position(pos: dict, decision: str, etf: dict | None) -> str:
    cur   = pos["currency"]
    dec   = _dec(pos["price"])
    emoji = {"COMPRAR": "🟢", "MANTENER": "🟡", "VENDER": "🔴", "ROTAR": "🔄"}.get(decision, "⏳")
    adx_label    = "💪 Fuerte"  if pos["adx"]   > 25             else "😴 Lateral"
    price_vs_sma = "📈 Por encima" if pos["price"] > pos["sma200"] else "📉 Por debajo"
    pnl           = pos.get("pnl_pct", 0.0) or 0.0

    lines = [
        f"🔷 *{pos['name']} ({pos['ticker']})*  {emoji} *{decision}*",
        f"📅 Cierre: `{pos['data_date']}`",
        f"",
        f"💰 Precio:        {cur} `{_fmt(pos['price'], dec)}`",
        f"📏 SMA 200:       {cur} `{_fmt(pos['sma200'], dec)}` — {price_vs_sma}",
        f"📡 ADX:           `{pos['adx']:.1f}` — {adx_label}",
        f"🔼 Donchian High: {cur} `{_fmt(pos['don_high'], dec)}`",
        f"🔽 Donchian Low:  {cur} `{_fmt(pos['don_low'], dec)}`",
        f"📉 ATR 14:        {cur} `{_fmt(pos['atr'], dec)}`",
        f"🛑 Stop Loss:     {cur} `{_fmt(pos.get('stop_loss'), dec)}`",
        f"",
        f"📌 Entrada:       {cur} `{_fmt(pos.get('entry_price'), dec)}`",
        f"{_pnl_emoji(pnl)} PnL:            `{pnl:+.2f}%`",
    ]

    # Bloque ETF si es ETH
    if etf:
        lines += [
            f"",
            f"📦 *{etf['ticker']} — ETF*",
        ]
        if etf["ok"]:
            edec = _dec(etf["price"])
            lines += [
                f"💰 Precio ETF:    EUR `{_fmt(etf['price'], edec)}`",
                f"📌 Entrada ETF:   EUR `{_fmt(etf.get('entry_price'), edec)}`",
                f"🛑 Stop Loss ETF: EUR `{_fmt(etf['stop_loss'], edec)}`",
            ]
        else:
            lines += [
                f"⚠️ _Datos ETF no disponibles_",
                f"🛑 Stop Loss ETF: EUR `{_fmt(etf.get('stop_loss'), 4)}` _(estimado)_",
            ]

    lines += [
        f"",
        f"💬 _{'Ruptura confirmada con fuerza de tendencia' if decision == 'COMPRAR' else 'Tendencia intacta' if decision == 'MANTENER' else 'Señal de salida activa'}_",
    ]
    return "\n".join(lines)


# ── Bloque de salida (cuando se vende) ────────────────────────────────────────

def format_salida(salida: dict) -> str:
    cur  = salida["currency"]
    dec  = _dec(salida["price"])
    pnl  = salida.get("pnl_pct", 0.0)
    tipo = salida.get("tipo", "VENTA")
    emoji = "🤑" if pnl > 0 else "😬"

    return "\n".join([
        f"🔻 *Cierre de posición — {salida['name']} ({salida['ticker']})*",
        f"📋 Tipo: _{tipo}_",
        f"💰 Precio salida: {cur} `{_fmt(salida['price'], dec)}`",
        f"📌 Precio entrada: {cur} `{_fmt(salida.get('entry_price'), dec)}`",
        f"{emoji} PnL: `{pnl:+.2f}%`",
    ])


# ── Ranking de momentum ───────────────────────────────────────────────────────

def format_ranking(ranking: list, pos_ticker: str | None) -> str:
    lines = ["📊 *Ranking Momentum (ROC 20 días)*", ""]
    for i, snap in enumerate(ranking, 1):
        ticker = snap["ticker"]
        roc    = snap["roc20"]
        ok     = snap["condiciones_ok"]

        if ticker == pos_ticker:
            marker = "🟡 _(posición actual)_"
        elif ok:
            marker = "✅"
        else:
            reasons = []
            if snap["price"] <= snap["sma200"]:
                reasons.append("bajo SMA200")
            if snap["adx"] <= 25:
                reasons.append("ADX<25")
            if not reasons:
                reasons.append("sin ruptura")
            marker = f"❌ {', '.join(reasons)}"

        sign = "+" if roc >= 0 else ""
        lines.append(f"`{i}.` *{snap['name']}* `{sign}{roc:.1f}%`  {marker}")

    return "\n".join(lines)


# ── Tabla historial ───────────────────────────────────────────────────────────

def format_historial(historial: list) -> str:
    if not historial:
        return "📋 *Últimas operaciones*\n\n_Sin operaciones registradas aún_"

    lines = ["📋 *Últimas 10 operaciones*", ""]
    lines.append("`Fecha        Activo   Tipo               Precio      PnL`")
    lines.append("`─────────────────────────────────────────────────────────`")

    for h in reversed(historial[-10:]):
        fecha   = h["fecha"][:10]
        accion  = h["accion"].ljust(7)[:7]
        tipo    = h["tipo"].ljust(18)[:18]
        cur     = h.get("currency", "")
        precio  = f"{cur} {h['precio']}"
        ganancia = h["ganancia"]
        lines.append(f"`{fecha}  {accion}  {tipo}  {precio:<12}  {ganancia}`")

    return "\n".join(lines)


# ── Mensaje completo ──────────────────────────────────────────────────────────

def format_report(result: dict, titulo: str) -> str:
    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    decision       = result["decision"]
    decision_ticker = result.get("decision_ticker", "")
    pos            = result.get("position")
    salida         = result.get("salida")
    ranking        = result.get("ranking", [])
    etf            = result.get("etf")
    historial      = result.get("historial", [])

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 *{titulo}*",
        f"🕐 {now} · {TIMEZONE}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"",
    ]

    # ── Cabecera de señal ──────────────────────────────────────────────────────
    if decision == "ROTAR":
        lines += [
            f"🚨 *SEÑAL ACTIVA*",
            f"",
            f"🔄 *ROTACIÓN* — `{decision_ticker}`",
            f"",
        ]
    elif decision == "COMPRAR":
        lines += [
            f"🚨 *SEÑAL ACTIVA*",
            f"",
            f"🟢 *COMPRAR* — `{decision_ticker}`",
            f"",
        ]
    elif decision == "VENDER":
        lines += [
            f"🚨 *SEÑAL ACTIVA*",
            f"",
            f"🔴 *VENDER* — `{decision_ticker}`",
            f"",
        ]
    elif decision == "MANTENER":
        lines += [
            f"🟡 *MANTENER* — posición en `{decision_ticker}`",
            f"",
        ]
    else:
        lines += [
            f"⏳ *Sin posición abierta — esperando señal*",
            f"",
        ]

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    # ── Posición que se cierra (en rotación o venta técnica) ──────────────────
    if salida:
        lines.append(format_salida(salida))
        lines += ["", "──────────────────────", ""]

    # ── Posición activa ────────────────────────────────────────────────────────
    if pos:
        label = decision if decision in ("COMPRAR", "MANTENER", "ROTAR") else "MANTENER"
        lines.append(format_position(pos, label, etf))
        lines += ["", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    elif decision == "ESPERAR":
        lines += ["_Sin posición abierta. Esperando condiciones de entrada._", "", "━━━━━━━━━━━━━━━━━━━━━━", ""]

    # ── Ranking ────────────────────────────────────────────────────────────────
    pos_ticker = pos["ticker"] if pos else None
    lines.append(format_ranking(ranking, pos_ticker))
    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━", ""]

    # ── Historial ──────────────────────────────────────────────────────────────
    lines.append(format_historial(historial))
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analizando mercado...")
    try:
        result = await asyncio.to_thread(run_analysis)
        msg = format_report(result, "Trend-Sustainer Rotacional")
        await update.message.reply_text(msg, parse_mode="Markdown")
    except MercadoCerradoError as e:
        await update.message.reply_text(
            f"🏖️ *Mercado cerrado*\n\n{e}\n\nPrueba de lunes a viernes.",
            parse_mode="Markdown"
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error en /estado:\n{tb}")
        await update.message.reply_text(
            f"❌ Error:\n<pre>{tb[-1000:]}</pre>",
            parse_mode="HTML"
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activos = ", ".join(f"{a['name']} ({a['ticker']})" for a in ASSETS)
    await update.message.reply_text(
        f"👋 *Trend-Sustainer Rotacional*\n\n"
        f"Activos: {activos}\n\n"
        f"Comandos:\n"
        f"• /estado — análisis completo ahora mismo\n"
        f"• Informe diario a las {DAILY_HOUR}:00 ({TIMEZONE})",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Ejecutando informe diario...")
    try:
        result = await asyncio.to_thread(run_analysis)
        msg = format_report(result, "Informe Diario — Trend-Sustainer")
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except MercadoCerradoError as e:
        logger.info(f"Informe omitido: {e}")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error en informe diario:\n{tb}")
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"❌ Error en informe diario:\n<pre>{tb[-1000:]}</pre>",
            parse_mode="HTML"
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

    logger.info(f"Bot iniciado — {[a['ticker'] for a in ASSETS]}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
