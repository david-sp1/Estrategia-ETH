"""
Estrategia "Trend-Sustainer" — multi-activo

Cada activo se define con:
  - ticker      : símbolo Yahoo Finance para indicadores (ETH-EUR, NVDA...)
  - currency    : moneda de cotización (EUR, USD...)
  - etf_ticker  : ticker secundario opcional (ej. ETHC.DE) para precio/stop del ETF
  - etf_currency: moneda del ETF (normalmente igual que currency)
  - name        : nombre legible

El estado de posición se guarda por activo en /data/state_<ticker>.json
"""

import json
import logging
import os
import re
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

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

# ── Catálogo de activos ───────────────────────────────────────────────────────
# Añade aquí nuevos activos sin tocar ningún otro fichero.
ASSETS = [
    {
        "ticker":       "ETH-EUR",
        "currency":     "EUR",
        "name":         "Ethereum",
        "etf_ticker":   os.environ.get("ETF_TICKER", "ETHC.DE"),
        "etf_currency": "EUR",
    },
    {
        "ticker":   "NVDA",
        "currency": "USD",
        "name":     "NVIDIA",
        "etf_ticker":   None,   # sin ETF asociado
        "etf_currency": None,
    },
{
        "ticker":   "GOOGL",
        "currency": "USD",
        "name":     "ALPHABET",
        "etf_ticker":   None,   # sin ETF asociado
        "etf_currency": None,
    },
  
]


# ── Excepciones ───────────────────────────────────────────────────────────────

class MercadoCerradoError(Exception):
    """Fin de semana: mercados de valores cerrados."""
    pass


# ── Descarga de datos ─────────────────────────────────────────────────────────

def _download(ticker: str, period: str = "5y") -> pd.DataFrame:
    raw = yf.download(ticker, period=period, interval="1d",
                      auto_adjust=True, progress=False)
    if raw.empty:
        raise ValueError(f"Sin datos para {ticker}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.index, utc=True)
    return df.dropna(subset=["close"])


def fetch_ohlcv(ticker: str, period: str = "5y") -> pd.DataFrame:
    """Descarga datos. Lanza MercadoCerradoError en fin de semana."""
    hoy = date.today()
    if hoy.weekday() >= 5:
        nombre = "sábado" if hoy.weekday() == 5 else "domingo"
        raise MercadoCerradoError(
            f"Hoy es {nombre} ({hoy}). Los mercados no operan."
        )
    df = _download(ticker, period)
    logger.info(f"[{ticker}] {df.index[-1].date()} | cierre: {df['close'].iloc[-1]:,.4f}")
    return df


def fetch_etf_price(etf_ticker: str) -> dict | None:
    """Precio del ETF secundario. Devuelve None si falla."""
    try:
        df = _download(etf_ticker, period="1y")
        return {
            "precio": float(df["close"].iloc[-1]),
            "fecha":  df.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception as e:
        logger.warning(f"[{etf_ticker}] No disponible: {e}")
        return None


# ── Indicadores ───────────────────────────────────────────────────────────────

def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def calc_donchian(high: pd.Series, low: pd.Series, period: int):
    return high.rolling(period).max(), low.rolling(period).min()


def calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def calc_adx(df: pd.DataFrame, period: int) -> pd.Series:
    high, low = df["high"], df["low"]
    plus_dm  = high - high.shift(1)
    minus_dm = low.shift(1) - low
    plus_dm  = plus_dm.where( (plus_dm  > minus_dm) & (plus_dm  > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm)  & (minus_dm > 0), 0.0)
    atr      = calc_atr(df, period)
    plus_di  = 100 * (plus_dm.ewm( span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).fillna(0)
    return dx.ewm(span=period, adjust=False).mean()


# ── Estado persistente por activo ─────────────────────────────────────────────

def _state_path(ticker: str) -> Path:
    safe = re.sub(r"[^\w\-]", "_", ticker)
    return DATA_DIR / f"state_{safe}.json"


def load_state(ticker: str) -> dict:
    p = _state_path(ticker)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {"position_open": False, "entry_price": None,
            "stop_loss": None, "etf_ratio": None}


def save_state(ticker: str, state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_state_path(ticker), "w") as f:
        json.dump(state, f, indent=2)


# ── Stop Loss ─────────────────────────────────────────────────────────────────

def calc_stop(price: float, atr: float, donchian_low: float,
              prev_stop: float | None) -> float:
    stop = max(price - atr * MULTIPLICADOR_ATR, donchian_low)
    if prev_stop is not None and stop < prev_stop:
        return prev_stop
    return stop


# ── Motor de decisión ─────────────────────────────────────────────────────────

def decide(price: float, sma200: float, adx: float,
           donchian_high_prev: float, state: dict) -> tuple[str, str]:
    if not state["position_open"]:
        if price > sma200 and adx > UMBRAL_ADX_ENTRADA and price >= donchian_high_prev:
            return "COMPRAR", "Ruptura confirmada con fuerza de tendencia"
        return "ESPERAR", "Mercado sin condiciones de entrada"
    else:
        if price < sma200 or price < state["stop_loss"]:
            return "VENDER", "Ruptura de soporte crítico o media de largo plazo"
        return "MANTENER", "Tendencia intacta o mercado lateral dentro de límites"


# ── Análisis de un activo ─────────────────────────────────────────────────────

def analyze_asset(asset: dict) -> dict:
    """
    Ejecuta la estrategia sobre un activo.
    Devuelve un dict con todos los datos listos para formatear.
    Puede lanzar MercadoCerradoError o ValueError.
    """
    ticker   = asset["ticker"]
    currency = asset["currency"]
    name     = asset["name"]

    # Indicadores
    df = fetch_ohlcv(ticker)
    df["sma200"]                           = calc_sma(df["close"], PERIODO_SMA)
    df["donchian_high"], df["donchian_low"] = calc_donchian(df["high"], df["low"], PERIODO_DONCHIAN)
    df["atr"] = calc_atr(df, PERIODO_ADX)
    df["adx"] = calc_adx(df, PERIODO_ADX)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price         = float(last["close"])
    sma200        = float(last["sma200"])
    don_high      = float(last["donchian_high"])
    don_low       = float(last["donchian_low"])
    don_high_prev = float(prev["donchian_high"])
    atr           = float(last["atr"])
    adx           = float(last["adx"])
    data_date     = df.index[-1].strftime("%Y-%m-%d")

    # Estado y decisión
    state     = load_state(ticker)
    prev_stop = state.get("stop_loss")
    stop      = calc_stop(price, atr, don_low, prev_stop)
    decision, reason = decide(price, sma200, adx, don_high_prev, state)

    # ETF secundario (opcional)
    etf_result = None
    if asset.get("etf_ticker"):
        time.sleep (3)
        etf_data = fetch_etf_price(asset["etf_ticker"])
        if etf_data:
            ratio     = etf_data["precio"] / price
            stop_etf  = stop * ratio
            state["etf_ratio"] = ratio
            etf_result = {
                "ticker":    asset["etf_ticker"],
                "currency":  asset["etf_currency"],
                "price":     etf_data["precio"],
                "data_date": etf_data["fecha"],
                "stop_loss": round(stop_etf, 4),
                "ok":        True,
            }
        else:
            last_ratio = state.get("etf_ratio")
            etf_result = {
                "ticker":    asset["etf_ticker"],
                "currency":  asset["etf_currency"],
                "price":     None,
                "data_date": None,
                "stop_loss": round(stop * last_ratio, 4) if last_ratio else None,
                "ok":        False,
            }

    # Actualizar estado
    if decision == "COMPRAR":
        state.update({"position_open": True, "entry_price": price, "stop_loss": stop})
    elif decision == "VENDER":
        state.update({"position_open": False, "entry_price": None, "stop_loss": None})
    elif decision == "MANTENER":
        state["stop_loss"] = stop
    save_state(ticker, state)

    return {
        "ticker":        ticker,
        "name":          name,
        "currency":      currency,
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_date":     data_date,
        "decision":      decision,
        "reason":        reason,
        "position_open": state["position_open"],
        "price":         price,
        "sma200":        sma200,
        "donchian_high": don_high,
        "donchian_low":  don_low,
        "atr":           atr,
        "adx":           adx,
        "stop_loss":     stop,
        "entry_price":   state.get("entry_price"),
        "etf":           etf_result,   # None si no hay ETF configurado
    }


def run_all_assets() -> list[dict]:
    """
    Analiza todos los activos del catálogo.
    Lanza MercadoCerradoError si es fin de semana (antes de cualquier descarga).
    Los errores por activo individual se capturan y se incluyen en el resultado.
    """
    hoy = date.today()
    if hoy.weekday() >= 5:
        nombre = "sábado" if hoy.weekday() == 5 else "domingo"
        raise MercadoCerradoError(
            f"Hoy es {nombre} ({hoy}). Los mercados no operan."
       )

    results = []
    for asset in ASSETS:
        time.sleep (3)
        try:
            results.append(analyze_asset(asset))
        except MercadoCerradoError:
            raise
        except Exception as e:
            logger.error(f"Error analizando {asset['ticker']}: {e}")
            results.append({
                "ticker":   asset["ticker"],
                "name":     asset["name"],
                "error":    str(e),
                "decision": "ERROR",
            })
    return results


if __name__ == "__main__":
    import pprint
    for r in run_all_assets():
        pprint.pprint(r)
        print()
