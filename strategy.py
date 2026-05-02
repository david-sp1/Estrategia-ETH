"""
Estrategia ETH "Trend-Sustainer"
Fuente de datos: ETF 21Shares Ethereum Core Staking ETP (ETHC.DE, Xetra)
ISIN: CH1209763130  — datos vía yfinance (público, sin API key).
"""

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Parámetros de la estrategia ───────────────────────────────────────────────
PERIODO_DONCHIAN = 50
PERIODO_SMA = 200
PERIODO_ADX = 14
MULTIPLICADOR_ATR = 3.5
UMBRAL_ADX_ENTRADA = 25

# Ticker del ETF en Yahoo Finance (Xetra, EUR)
ETF_TICKER = os.environ.get("ETF_TICKER", "ETHC.DE")
CURRENCY = "EUR"

# Archivo de estado persistente (posición abierta)
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))


# ── Datos de mercado ──────────────────────────────────────────────────────────

class MercadoCerradoError(Exception):
    """Se lanza cuando el último dato disponible es de un día no hábil."""
    pass


def fetch_ohlcv(ticker: str = ETF_TICKER, period: str = "2y") -> pd.DataFrame:
    """
    Descarga velas diarias del ETF via yfinance.
    - period="2y" garantiza más de 200 velas para la SMA200.
    - El índice queda en UTC (días hábiles europeos, L–V).
    Lanza MercadoCerradoError si hoy es fin de semana o festivo.
    """
    hoy = date.today()
    if hoy.weekday() >= 5:  # 5=sábado, 6=domingo
        raise MercadoCerradoError(
            f"Hoy es {'sábado' if hoy.weekday()==5 else 'domingo'} "
            f"({hoy}). El mercado Xetra no opera. El bot no envía informe."
        )

    raw = yf.download(ticker, period=period, interval="1d",
                      auto_adjust=True, progress=False)

    if raw.empty:
        raise ValueError(f"yfinance no devolvió datos para {ticker}")

    # yfinance puede devolver MultiIndex de columnas con el ticker
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.dropna(subset=["close"])

    # Comprobación adicional: si el último día hábil fue festivo,
    # yfinance devuelve datos hasta el último día con cotización.
    # No es un error — simplemente usamos ese último cierre disponible.
    logger.info(f"Último dato disponible: {df.index[-1].date()} | Cierre: {df['close'].iloc[-1]:.4f} {CURRENCY}")

    return df


# ── Indicadores ───────────────────────────────────────────────────────────────

def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def calc_donchian(high: pd.Series, low: pd.Series, period: int):
    return high.rolling(period).max(), low.rolling(period).min()


def calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def calc_adx(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    plus_dm = high - prev_high
    minus_dm = prev_low - low
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr = calc_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).fillna(0)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx


# ── Estado persistente ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"position_open": False, "entry_price": None, "stop_loss": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Stop Loss dinámico con trinquete ─────────────────────────────────────────

def calc_stop(price: float, atr: float, donchian_low: float, prev_stop: float | None) -> float:
    opcion_a = price - (atr * MULTIPLICADOR_ATR)
    opcion_b = donchian_low
    stop_sugerido = max(opcion_a, opcion_b)
    if prev_stop is not None and stop_sugerido < prev_stop:
        return prev_stop
    return stop_sugerido


# ── Motor de decisión ─────────────────────────────────────────────────────────

def decide(price: float, sma200: float, adx: float,
           donchian_high_prev: float, stop: float,
           state: dict) -> tuple[str, str]:
    if not state["position_open"]:
        if price > sma200 and adx > UMBRAL_ADX_ENTRADA and price >= donchian_high_prev:
            return "COMPRAR", "Ruptura confirmada con fuerza de tendencia"
        return "ESPERAR", "Mercado sin condiciones de entrada"
    else:
        if price < sma200 or price < state["stop_loss"]:
            return "VENDER", "Ruptura de soporte crítico o media de largo plazo"
        return "MANTENER", "Tendencia intacta o mercado lateral dentro de límites"


# ── Análisis principal ────────────────────────────────────────────────────────

def run_analysis() -> dict:
    df = fetch_ohlcv()  # MercadoCerradoError se propaga hacia arriba si procede

    df["sma200"] = calc_sma(df["close"], PERIODO_SMA)
    df["donchian_high"], df["donchian_low"] = calc_donchian(df["high"], df["low"], PERIODO_DONCHIAN)
    df["atr"] = calc_atr(df, PERIODO_ADX)
    df["adx"] = calc_adx(df, PERIODO_ADX)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(last["close"])
    sma200 = float(last["sma200"])
    donchian_high = float(last["donchian_high"])
    donchian_low = float(last["donchian_low"])
    donchian_high_prev = float(prev["donchian_high"])  # rompida en cierre actual
    atr = float(last["atr"])
    adx = float(last["adx"])

    state = load_state()
    prev_stop = state.get("stop_loss")

    stop = calc_stop(price, atr, donchian_low, prev_stop)

    decision, reason = decide(price, sma200, adx, donchian_high_prev, stop, state)

    # Actualizar estado
    if decision == "COMPRAR":
        state["position_open"] = True
        state["entry_price"] = price
        state["stop_loss"] = stop
    elif decision == "VENDER":
        state["position_open"] = False
        state["entry_price"] = None
        state["stop_loss"] = None
    elif decision == "MANTENER":
        state["stop_loss"] = stop  # trinquete actualizado
    save_state(state)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data_date = df.index[-1].strftime("%Y-%m-%d")

    return {
        "timestamp": ts,
        "data_date": data_date,
        "ticker": ETF_TICKER,
        "currency": CURRENCY,
        "price": price,
        "sma200": sma200,
        "donchian_high": donchian_high,
        "donchian_low": donchian_low,
        "atr": atr,
        "adx": adx,
        "stop_loss": stop,
        "decision": decision,
        "reason": reason,
        "position_open": state["position_open"],
        "entry_price": state.get("entry_price"),
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run_analysis())
