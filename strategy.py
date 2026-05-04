"""
Estrategia ETH "Trend-Sustainer"

Fuentes de datos:
  - Índice : ETH-EUR  (Yahoo Finance) — serie larga, todos los indicadores
  - ETF    : ETHC.DE  (Yahoo Finance, ISIN CH1209763130) — precio y stop loss

El stop loss se calcula en precio del índice ETH-EUR y se traduce al ETF
usando el ratio actual entre ambos. Si el ETF no está disponible, se
informa con el stop estimado y el mensaje se envía igualmente.
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
PERIODO_DONCHIAN   = 50
PERIODO_SMA        = 200
PERIODO_ADX        = 14
MULTIPLICADOR_ATR  = 3.5
UMBRAL_ADX_ENTRADA = 25

INDEX_TICKER = os.environ.get("INDEX_TICKER", "ETH-EUR")
ETF_TICKER   = os.environ.get("ETF_TICKER",   "ETHC.DE")
CURRENCY     = "EUR"

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))


# ── Excepciones ───────────────────────────────────────────────────────────────

class MercadoCerradoError(Exception):
    """Fin de semana o festivo: no hay datos nuevos."""
    pass


# ── Descarga de datos ─────────────────────────────────────────────────────────

def _download(ticker: str, period: str = "5y") -> pd.DataFrame:
    """Descarga OHLCV de yfinance y normaliza columnas."""
    raw = yf.download(ticker, period=period, interval="1d",
                      auto_adjust=True, progress=False)
    if raw.empty:
        raise ValueError(f"yfinance no devolvio datos para {ticker}")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.dropna(subset=["close"])
    return df


def fetch_index(period: str = "5y") -> pd.DataFrame:
    """
    Descarga el indice ETH-EUR. Lanza MercadoCerradoError en fin de semana
    (referencia al horario del ETF en Xetra).
    """
    hoy = date.today()
    if hoy.weekday() >= 5:
        nombre = "sabado" if hoy.weekday() == 5 else "domingo"
        raise MercadoCerradoError(
            f"Hoy es {nombre} ({hoy}). El mercado Xetra no opera."
        )
    df = _download(INDEX_TICKER, period)
    logger.info(f"[{INDEX_TICKER}] Ultimo dato: {df.index[-1].date()} | "
                f"Cierre: {df['close'].iloc[-1]:,.2f} {CURRENCY}")
    return df


def fetch_etf() -> dict | None:
    """
    Descarga el precio mas reciente de ETHC.DE.
    Devuelve dict con precio y fecha, o None si falla.
    """
    try:
        df = _download(ETF_TICKER, period="1y")
        precio = float(df["close"].iloc[-1])
        fecha  = df.index[-1].strftime("%Y-%m-%d")
        logger.info(f"[{ETF_TICKER}] Ultimo dato: {fecha} | Cierre: {precio:.4f} {CURRENCY}")
        return {"precio": precio, "fecha": fecha}
    except Exception as e:
        logger.warning(f"[{ETF_TICKER}] No disponible: {e}")
        return None


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
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def calc_adx(df: pd.DataFrame, period: int) -> pd.Series:
    high, low = df["high"], df["low"]
    prev_high = high.shift(1)
    prev_low  = low.shift(1)

    plus_dm  = high - prev_high
    minus_dm = prev_low - low
    plus_dm  = plus_dm.where( (plus_dm  > minus_dm) & (plus_dm  > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm)  & (minus_dm > 0), 0.0)

    atr      = calc_atr(df, period)
    plus_di  = 100 * (plus_dm.ewm( span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).fillna(0)
    return dx.ewm(span=period, adjust=False).mean()


# ── Estado persistente ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"position_open": False, "entry_price": None,
            "stop_loss": None, "stop_loss_etf": None, "etf_ratio": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Stop Loss dinamico con trinquete ─────────────────────────────────────────

def calc_stop(price: float, atr: float, donchian_low: float,
              prev_stop: float | None) -> float:
    opcion_a      = price - (atr * MULTIPLICADOR_ATR)
    opcion_b      = donchian_low
    stop_sugerido = max(opcion_a, opcion_b)
    if prev_stop is not None and stop_sugerido < prev_stop:
        return prev_stop
    return stop_sugerido


def traducir_stop_a_etf(stop_index: float, precio_index: float,
                         precio_etf: float) -> float:
    """Convierte el stop del indice al precio equivalente del ETF."""
    ratio = precio_etf / precio_index
    return stop_index * ratio


# ── Motor de decision ─────────────────────────────────────────────────────────

def decide(price: float, sma200: float, adx: float,
           donchian_high_prev: float, state: dict) -> tuple[str, str]:
    if not state["position_open"]:
        if (price > sma200 and adx > UMBRAL_ADX_ENTRADA
                and price >= donchian_high_prev):
            return "COMPRAR", "Ruptura confirmada con fuerza de tendencia"
        return "ESPERAR", "Mercado sin condiciones de entrada"
    else:
        if price < sma200 or price < state["stop_loss"]:
            return "VENDER", "Ruptura de soporte critico o media de largo plazo"
        return "MANTENER", "Tendencia intacta o mercado lateral dentro de limites"


# ── Analisis principal ────────────────────────────────────────────────────────

def run_analysis() -> dict:
    # ── 1. Indice ETH-EUR ─────────────────────────────────────────────────────
    df = fetch_index()  # lanza MercadoCerradoError si procede

    df["sma200"]                           = calc_sma(df["close"], PERIODO_SMA)
    df["donchian_high"], df["donchian_low"] = calc_donchian(
        df["high"], df["low"], PERIODO_DONCHIAN)
    df["atr"] = calc_atr(df, PERIODO_ADX)
    df["adx"] = calc_adx(df, PERIODO_ADX)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price_idx          = float(last["close"])
    sma200             = float(last["sma200"])
    donchian_high      = float(last["donchian_high"])
    donchian_low       = float(last["donchian_low"])
    donchian_high_prev = float(prev["donchian_high"])
    atr                = float(last["atr"])
    adx                = float(last["adx"])
    data_date_idx      = df.index[-1].strftime("%Y-%m-%d")

    # ── 2. Estado y stop loss en precio del indice ────────────────────────────
    state     = load_state()
    prev_stop = state.get("stop_loss")

    stop_idx         = calc_stop(price_idx, atr, donchian_low, prev_stop)
    decision, reason = decide(price_idx, sma200, adx, donchian_high_prev, state)

    # ── 3. ETF ETHC.DE ────────────────────────────────────────────────────────
    etf_data = fetch_etf()  # None si no disponible

    if etf_data is not None:
        precio_etf = etf_data["precio"]
        fecha_etf  = etf_data["fecha"]
        stop_etf   = traducir_stop_a_etf(stop_idx, price_idx, precio_etf)
        etf_ok     = True
        state["etf_ratio"] = precio_etf / price_idx  # actualizamos ratio
    else:
        precio_etf = None
        fecha_etf  = None
        last_ratio = state.get("etf_ratio")
        stop_etf   = round(stop_idx * last_ratio, 4) if last_ratio else None
        etf_ok     = False

    # ── 4. Actualizar estado ──────────────────────────────────────────────────
    if decision == "COMPRAR":
        state["position_open"] = True
        state["entry_price"]   = price_idx
        state["stop_loss"]     = stop_idx
        state["stop_loss_etf"] = stop_etf
    elif decision == "VENDER":
        state["position_open"] = False
        state["entry_price"]   = None
        state["stop_loss"]     = None
        state["stop_loss_etf"] = None
    elif decision == "MANTENER":
        state["stop_loss"]     = stop_idx
        state["stop_loss_etf"] = stop_etf

    save_state(state)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return {
        # — Meta —
        "timestamp":     ts,
        "decision":      decision,
        "reason":        reason,
        "position_open": state["position_open"],

        # — Indice ETH-EUR —
        "index": {
            "ticker":        INDEX_TICKER,
            "currency":      CURRENCY,
            "data_date":     data_date_idx,
            "price":         price_idx,
            "sma200":        sma200,
            "donchian_high": donchian_high,
            "donchian_low":  donchian_low,
            "atr":           atr,
            "adx":           adx,
            "stop_loss":     stop_idx,
            "entry_price":   state.get("entry_price"),
        },

        # — ETF ETHC.DE —
        "etf": {
            "ticker":    ETF_TICKER,
            "currency":  CURRENCY,
            "data_date": fecha_etf,
            "price":     precio_etf,
            "stop_loss": stop_etf,
            "ok":        etf_ok,
        },
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run_analysis())
