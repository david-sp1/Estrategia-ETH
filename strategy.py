"""
Estrategia "Trend-Sustainer" — Sistema Rotacional

Lógica:
  - Solo una posición abierta a la vez
  - Entra en el activo con mayor momentum ROC(20) que cumpla las 3 condiciones
  - Rota si otro activo tiene momentum >15% superior al actual
  - Sale si Close < Stop Loss o Close < SMA200 × (1 - buffer)
  - Stop Loss: max(precio - ATR×3.5, Donchian_Low) con trinquete
  - ETH: indicadores sobre ETH-EUR, precio/stop traducido a ETHC.DE
"""

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_ta as ta

import yfinance as yf

logger = logging.getLogger(__name__)

# ── Parámetros ────────────────────────────────────────────────────────────────
PERIODO_DONCHIAN   = 50
PERIODO_SMA        = 200
PERIODO_ADX        = 14
PERIODO_ATR        = 14
PERIODO_ROC        = 20
MULTIPLICADOR_ATR  = 3.5
UMBRAL_ADX         = 25
BUFFER_SMA         = 0.02   # 2% — precio debe estar 2% sobre/bajo SMA para señal
UMBRAL_ROTACION    = 0.15   # 15% — momentum del candidato debe superar al actual en 15%

ETF_TICKER   = os.environ.get("ETF_TICKER",   "ETHC.DE")
ETH_TICKER   = "ETH-EUR"
DATA_DIR     = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE   = DATA_DIR / "state_rotacional.json"
HISTORIAL_FILE = DATA_DIR / "historial.json"

# ── Catálogo de activos ───────────────────────────────────────────────────────
ASSETS = [
    {"ticker": "ETH-EUR", "name": "Ethereum",  "currency": "EUR"},
    {"ticker": "NVDA",    "name": "NVIDIA",     "currency": "USD"},
    {"ticker": "GOOGL",   "name": "Alphabet",   "currency": "USD"},
    {"ticker": "AAPL",    "name": "Apple",      "currency": "USD"},
    {"ticker": "MSFT",    "name": "Microsoft",  "currency": "USD"},
    {"ticker": "BTC-EUR", "name": "Bitcoin",    "currency": "EUR"},
]


# ── Excepciones ───────────────────────────────────────────────────────────────

class MercadoCerradoError(Exception):
    pass


# ── Persistencia ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "position_open": False,
        "ticker":        None,
        "name":          None,
        "currency":      None,
        "entry_price":   None,
        "stop_loss":     None,
        "etf_ratio":     None,
    }


def save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_historial() -> list:
    if HISTORIAL_FILE.exists():
        with open(HISTORIAL_FILE) as f:
            return json.load(f)
    return []


def save_historial(historial: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORIAL_FILE, "w") as f:
        json.dump(historial[-100:], f, indent=2)  # guardamos últimas 100


def add_to_historial(entry: dict):
    h = load_historial()
    h.append(entry)
    save_historial(h)


# ── Descarga ──────────────────────────────────────────────────────────────────

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


def fetch_all_assets() -> dict[str, pd.DataFrame]:
    """Descarga todos los activos con pausa entre peticiones."""
    hoy = date.today()
    if hoy.weekday() >= 5:
        nombre = "sábado" if hoy.weekday() == 5 else "domingo"
        raise MercadoCerradoError(
            f"Hoy es {nombre} ({hoy}). Los mercados no operan."
        )

    data = {}
    for i, asset in enumerate(ASSETS):
        if i > 0:
            time.sleep(3)
        ticker = asset["ticker"]
        try:
            data[ticker] = _download(ticker)
            logger.info(f"[{ticker}] OK — cierre: {data[ticker]['close'].iloc[-1]:,.4f}")
        except Exception as e:
            logger.error(f"[{ticker}] Error descargando: {e}")

    return data


def fetch_etf_price() -> dict | None:
    """Precio actual del ETF ETHC.DE."""
    try:
        time.sleep(2)
        df = _download(ETF_TICKER, period="1y")
        return {
            "precio": float(df["close"].iloc[-1]),
            "fecha":  df.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception as e:
        logger.warning(f"[{ETF_TICKER}] No disponible: {e}")
        return None


# ── Indicadores ───────────────────────────────────────────────────────────────

def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma200"]        = ta.sma(df["close"], length=PERIODO_SMA)
    adx_df              = ta.adx(df["high"], df["low"], df["close"], length=PERIODO_ADX)
    df["adx"]           = adx_df.iloc[:, 0]
    df["atr"]           = ta.atr(df["high"], df["low"], df["close"], length=PERIODO_ATR)
    df["don_high_prev"] = df["high"].rolling(PERIODO_DONCHIAN).max().shift(1)
    df["don_low"]       = df["low"].rolling(PERIODO_DONCHIAN).min()
    df["roc20"]         = ta.roc(df["close"], length=PERIODO_ROC)
    return df


# ── Stop Loss ─────────────────────────────────────────────────────────────────

def calc_stop(price: float, atr: float, don_low: float,
              prev_stop: float | None) -> float:
    stop = max(price - atr * MULTIPLICADOR_ATR, don_low)
    if prev_stop is not None and stop < prev_stop:
        return prev_stop
    return stop


# ── Análisis principal ────────────────────────────────────────────────────────

def run_analysis() -> dict:
    # 1. Descargar todos los activos
    data_raw = fetch_all_assets()

    # 2. Calcular indicadores
    data = {}
    for ticker, df in data_raw.items():
        try:
            data[ticker] = calc_indicators(df)
        except Exception as e:
            logger.error(f"[{ticker}] Error calculando indicadores: {e}")

    if not data:
        raise ValueError("No se pudieron calcular indicadores para ningún activo")

    # 3. Estado actual
    state = load_state()

    # 4. Construir snapshot de cada activo en la última fecha disponible
    snapshots = {}
    for asset in ASSETS:
        ticker = asset["ticker"]
        if ticker not in data:
            continue
        df  = data[ticker]
        row = df.iloc[-1]

        # Saltar filas con NaN en indicadores clave
        if pd.isna(row["sma200"]) or pd.isna(row["adx"]) or pd.isna(row["roc20"]):
            continue

        price         = float(row["close"])
        sma200        = float(row["sma200"])
        adx           = float(row["adx"])
        atr           = float(row["atr"])
        don_high_prev = float(row["don_high_prev"]) if not pd.isna(row["don_high_prev"]) else price
        don_low       = float(row["don_low"])
        roc20         = float(row["roc20"])
        data_date     = df.index[-1].strftime("%Y-%m-%d")
        don_high      = float(df["high"].rolling(PERIODO_DONCHIAN).max().iloc[-1])

        condiciones_ok = (
            price > sma200 * (1 + BUFFER_SMA) and
            adx   > UMBRAL_ADX and
            float(row["high"]) >= don_high_prev
        )

        snapshots[ticker] = {
            "ticker":        ticker,
            "name":          asset["name"],
            "currency":      asset["currency"],
            "data_date":     data_date,
            "price":         price,
            "sma200":        sma200,
            "adx":           adx,
            "atr":           atr,
            "don_high":      don_high,
            "don_low":       don_low,
            "roc20":         roc20,
            "condiciones_ok": condiciones_ok,
        }

    # 5. Ranking por momentum
    ranking = sorted(
        snapshots.values(),
        key=lambda x: x["roc20"],
        reverse=True
    )

    # 6. Lógica rotacional
    decision       = "ESPERAR"
    decision_ticker = None
    tipo_operacion  = None
    candidato       = None   # activo candidato a entrar
    salida_info     = None   # info del activo que se vende

    # Candidato con más momentum que cumple condiciones
    for snap in ranking:
        if snap["condiciones_ok"]:
            candidato = snap
            break

    if state["position_open"]:
        t_inv    = state["ticker"]
        snap_inv = snapshots.get(t_inv)

        if snap_inv:
            # Actualizar stop con trinquete
            nuevo_stop = calc_stop(
                snap_inv["price"], snap_inv["atr"],
                snap_inv["don_low"], state["stop_loss"]
            )
            state["stop_loss"] = nuevo_stop

            mom_actual = snap_inv["roc20"]
            price_inv  = snap_inv["price"]

            # Condición de salida técnica
            salida_tecnica = (
                price_inv < snap_inv["sma200"] * (1 - BUFFER_SMA) or
                price_inv < state["stop_loss"]
            )

            # Condición de rotación
            debe_rotar = (
                candidato is not None and
                candidato["ticker"] != t_inv and
                candidato["roc20"] > mom_actual * (1 + UMBRAL_ROTACION)
            )

            if salida_tecnica or debe_rotar:
                tipo_operacion = "VENTA ROTACIÓN" if debe_rotar else "VENTA TÉCNICA"
                pnl_pct = (price_inv / state["entry_price"] - 1) * 100

                salida_info = {
                    **snap_inv,
                    "stop_loss":   state["stop_loss"],
                    "entry_price": state["entry_price"],
                    "pnl_pct":     round(pnl_pct, 2),
                    "tipo":        tipo_operacion,
                }

                add_to_historial({
                    "fecha":     snap_inv["data_date"],
                    "accion":    t_inv,
                    "nombre":    state["name"],
                    "tipo":      tipo_operacion,
                    "precio":    round(price_inv, 4),
                    "ganancia":  f"{pnl_pct:+.2f}%",
                    "currency":  state["currency"],
                })

                state["position_open"] = False
                state["ticker"]        = None
                state["entry_price"]   = None
                state["stop_loss"]     = None

                decision = "VENDER"
                decision_ticker = t_inv

                # Si es rotación, entramos en el candidato
                if debe_rotar and candidato:
                    stop_nuevo = calc_stop(
                        candidato["price"], candidato["atr"],
                        candidato["don_low"], None
                    )
                    state["position_open"] = True
                    state["ticker"]        = candidato["ticker"]
                    state["name"]          = candidato["name"]
                    state["currency"]      = candidato["currency"]
                    state["entry_price"]   = candidato["price"]
                    state["stop_loss"]     = stop_nuevo

                    add_to_historial({
                        "fecha":    candidato["data_date"],
                        "accion":   candidato["ticker"],
                        "nombre":   candidato["name"],
                        "tipo":     "COMPRA",
                        "precio":   round(candidato["price"], 4),
                        "ganancia": "—",
                        "currency": candidato["currency"],
                    })

                    decision        = "ROTAR"
                    decision_ticker = f"{t_inv} → {candidato['ticker']}"

            else:
                # Mantener posición
                decision        = "MANTENER"
                decision_ticker = t_inv

        else:
            # Activo en posición ya no tiene datos, cerramos
            state["position_open"] = False
            state["stop_loss"]     = None

    if not state["position_open"]:
        if candidato:
            stop_nuevo = calc_stop(
                candidato["price"], candidato["atr"],
                candidato["don_low"], None
            )
            state["position_open"] = True
            state["ticker"]        = candidato["ticker"]
            state["name"]          = candidato["name"]
            state["currency"]      = candidato["currency"]
            state["entry_price"]   = candidato["price"]
            state["stop_loss"]     = stop_nuevo

            add_to_historial({
                "fecha":    candidato["data_date"],
                "accion":   candidato["ticker"],
                "nombre":   candidato["name"],
                "tipo":     "COMPRA",
                "precio":   round(candidato["price"], 4),
                "ganancia": "—",
                "currency": candidato["currency"],
            })

            decision        = "COMPRAR"
            decision_ticker = candidato["ticker"]
        else:
            decision        = "ESPERAR"
            decision_ticker = None

    # 7. Stop loss del activo en posición (con ETF si es ETH)
    pos_snap = snapshots.get(state["ticker"]) if state["position_open"] else None

    if pos_snap:
        pos_snap["stop_loss"]   = state["stop_loss"]
        pos_snap["entry_price"] = state["entry_price"]
        if pos_snap["price"] and state["entry_price"]:
            pos_snap["pnl_pct"] = round(
                (pos_snap["price"] / state["entry_price"] - 1) * 100, 2
            )

    # 8. ETF (solo relevante cuando la posición es ETH-EUR)
    etf_result = None
    if state["position_open"] and state["ticker"] == ETH_TICKER:
        etf_data = fetch_etf_price()
        eth_price = pos_snap["price"] if pos_snap else None

        if etf_data and eth_price:
            ratio = etf_data["precio"] / eth_price
            state["etf_ratio"] = ratio
            etf_result = {
                "ticker":    ETF_TICKER,
                "price":     etf_data["precio"],
                "data_date": etf_data["fecha"],
                "stop_loss": round(state["stop_loss"] * ratio, 4),
                "entry_price": round(state["entry_price"] * ratio, 4) if state["entry_price"] else None,
                "ok":        True,
            }
        else:
            last_ratio = state.get("etf_ratio")
            etf_result = {
                "ticker":    ETF_TICKER,
                "price":     None,
                "data_date": None,
                "stop_loss": round(state["stop_loss"] * last_ratio, 4) if last_ratio and state["stop_loss"] else None,
                "entry_price": round(state["entry_price"] * last_ratio, 4) if last_ratio and state["entry_price"] else None,
                "ok":        False,
            }

    save_state(state)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "timestamp":       ts,
        "decision":        decision,
        "decision_ticker": decision_ticker,
        "position":        pos_snap,
        "salida":          salida_info,
        "ranking":         ranking,
        "etf":             etf_result,
        "historial":       load_historial()[-10:],
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run_analysis())
