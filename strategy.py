"""
Estrategia "Trend-Sustainer" — Sistema Rotacional

Lógica:
  - Solo una posición abierta a la vez
  - Entra en el activo con mayor momentum ROC(20) que cumpla las 3 condiciones
  - Rota si otro activo tiene momentum >15% superior al actual
  - Sale si Close < Stop Loss o Close < SMA200 × (1 - buffer)
  - Stop Loss: max(precio - ATR×3, Donchian_Low) con trinquete
  - ETH: indicadores sobre ETH-EUR, precio/stop traducido a ETHC.DE

Comandos CLI:
  python strategy.py                        → ejecuta el análisis
  python strategy.py /help                  → muestra esta ayuda
  python strategy.py /reset                 → resetea el estado y el historial
  python strategy.py /add <TICKER> <PRECIO_ENTRADA> [STOP_LOSS]
                                            → inicializa una posición ya comprada
"""

import argparse
import json
import logging
import os
import re
import sys
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
MULTIPLICADOR_ATR  = 3
UMBRAL_ADX         = 25
BUFFER_SMA         = 0.02   # 2% — precio debe estar 2% sobre/bajo SMA para señal
UMBRAL_ROTACION    = 0.2   # 20% — momentum del candidato debe superar al actual en 20%

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
    {"ticker": "MSFT",    "name": "Microsoft",  "currency": "USD"},
    {"ticker": "BTC-EUR", "name": "Bitcoin",    "currency": "EUR"},
]

# Mapa ticker → info de activo (para lookup rápido en /add)
ASSETS_MAP = {a["ticker"]: a for a in ASSETS}


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

def _download(ticker: str, period: str = "1y", retries: int = 3) -> pd.DataFrame:
    for attempt in range(retries):
        try:
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
        except Exception as e:
            if "RateLimit" in str(e) or "Too Many" in str(e):
                wait = 15 * (attempt + 1)  # 15s, 30s, 45s
                logger.warning(f"[{ticker}] Rate limit, reintentando en {wait}s... (intento {attempt+1}/{retries})")
                time.sleep(wait)
            else:
                raise
    raise ValueError(f"Rate limit persistente para {ticker} tras {retries} intentos")


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
            time.sleep(15)  # pausa entre activos para evitar rate limiting
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
        time.sleep(15)
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
    # Columna ADX_14 en v0.4.x, primera columna en versiones anteriores
    adx_col = "ADX_14" if "ADX_14" in adx_df.columns else adx_df.columns[0]
    df["adx"]           = adx_df[adx_col]

    df["atr"]           = ta.atr(df["high"], df["low"], df["close"], length=PERIODO_ATR)
    df["don_high_prev"] = df["high"].rolling(PERIODO_DONCHIAN).max().shift(1)
    df["don_low"]       = df["low"].rolling(PERIODO_DONCHIAN).min()

    roc_result          = ta.roc(df["close"], length=PERIODO_ROC)
    # roc puede devolver Series o DataFrame según versión
    if isinstance(roc_result, pd.DataFrame):
        df["roc20"] = roc_result.iloc[:, 0]
    else:
        df["roc20"] = roc_result

    return df


# ── Stop Loss ─────────────────────────────────────────────────────────────────

def calc_stop(price: float, atr: float, don_low: float,
              prev_stop: float | None) -> float:
    stop = max(price - atr * MULTIPLICADOR_ATR, don_low)
    if prev_stop is not None and stop < prev_stop:
        return prev_stop
    return stop


# ── Comandos CLI ──────────────────────────────────────────────────────────────

def cmd_help():
    """Muestra la ayuda de los comandos disponibles."""
    ayuda = """
╔══════════════════════════════════════════════════════════════════╗
║          Trend-Sustainer — Comandos disponibles                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  ( sin argumentos )                                              ║
║    Ejecuta el análisis rotacional completo.                      ║
║    Descarga datos, calcula indicadores y emite la decisión.      ║
║                                                                  ║
║  /help                                                           ║
║    Muestra esta pantalla de ayuda.                               ║
║                                                                  ║
║  /reset [--confirm]                                              ║
║    Borra el estado actual (posición abierta, stop loss, ratio    ║
║    ETF) y el historial de operaciones completo.                  ║
║    Requiere --confirm para ejecutarse (medida de seguridad).     ║
║                                                                  ║
║    Ejemplo:                                                      ║
║      python strategy.py /reset --confirm                         ║
║                                                                  ║
║  /add <TICKER> <PRECIO_ENTRADA> [STOP_LOSS]                      ║
║    Inicializa el estado con una posición ya comprada.            ║
║    Útil para sincronizar el sistema con una compra manual.       ║
║                                                                  ║
║    Argumentos:                                                    ║
║      TICKER         Ticker del activo (debe estar en el          ║
║                     catálogo): ETH-EUR, NVDA, GOOGL, MSFT,      ║
║                     BTC-EUR                                      ║
║      PRECIO_ENTRADA Precio al que se compró (float)              ║
║      STOP_LOSS      (Opcional) Stop loss inicial. Si se omite,   ║
║                     se calcula automáticamente con ATR×3       ║
║                     y Donchian Low descargando datos reales.     ║
║                                                                  ║
║    Ejemplos:                                                      ║
║      python strategy.py /add NVDA 950.50                         ║
║      python strategy.py /add ETH-EUR 2100.00 1850.00             ║
║      python strategy.py /add BTC-EUR 58000 52000                 ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""
    print(ayuda)


def cmd_reset(confirm: bool = False):
    """
    Resetea el estado y el historial.
    Requiere confirm=True como medida de seguridad.
    """
    if not confirm:
        print("⚠️  ATENCIÓN: Este comando borrará el estado y el historial completo.")
        print("   Para confirmar, ejecuta:")
        print("   python strategy.py /reset --confirm")
        return

    state_borrado    = STATE_FILE.exists()
    historial_borrado = HISTORIAL_FILE.exists()

    if state_borrado:
        STATE_FILE.unlink()
    if historial_borrado:
        HISTORIAL_FILE.unlink()

    # Guardar estado limpio
    estado_limpio = {
        "position_open": False,
        "ticker":        None,
        "name":          None,
        "currency":      None,
        "entry_price":   None,
        "stop_loss":     None,
        "etf_ratio":     None,
    }
    save_state(estado_limpio)
    save_historial([])

    print("✅ Reset completado.")
    if state_borrado:
        print(f"   · Estado borrado:    {STATE_FILE}")
    if historial_borrado:
        print(f"   · Historial borrado: {HISTORIAL_FILE}")
    print("   · Archivos reiniciados con valores por defecto.")


def cmd_add(ticker: str, entry_price: float, stop_loss: float | None = None):
    """
    Inicializa el estado con una posición ya comprada.
    Si no se proporciona stop_loss, lo calcula descargando datos reales.
    """
    ticker = ticker.upper()

    # Validar ticker
    if ticker not in ASSETS_MAP:
        tickers_validos = ", ".join(ASSETS_MAP.keys())
        print(f"❌ Error: ticker '{ticker}' no está en el catálogo.")
        print(f"   Tickers válidos: {tickers_validos}")
        sys.exit(1)

    asset = ASSETS_MAP[ticker]

    # Verificar que no haya posición abierta
    state = load_state()
    if state.get("position_open"):
        t_actual = state.get("ticker", "desconocido")
        print(f"⚠️  Ya hay una posición abierta en {t_actual}.")
        print("   Usa /reset --confirm antes de añadir una nueva posición.")
        sys.exit(1)

    # Calcular stop loss automático si no se proporcionó
    if stop_loss is None:
        print(f"ℹ️  Stop loss no proporcionado. Descargando datos de {ticker} para calcularlo...")
        try:
            time.sleep(2)
            df  = _download(ticker)
            df  = calc_indicators(df)
            row = df.iloc[-1]

            atr     = float(row["atr"])     if not pd.isna(row.get("atr",     float("nan"))) else None
            don_low = float(row["don_low"]) if not pd.isna(row.get("don_low", float("nan"))) else None

            if atr is None or don_low is None:
                print("⚠️  No se pudo calcular ATR/Donchian Low (datos insuficientes).")
                print("   Proporciona el stop loss manualmente:")
                print(f"   python strategy.py /add {ticker} {entry_price} <STOP_LOSS>")
                sys.exit(1)

            stop_loss = calc_stop(entry_price, atr, don_low, None)
            print(f"   ATR={atr:.4f}  Donchian Low={don_low:.4f}")
            print(f"   Stop Loss calculado: {stop_loss:.4f}")

        except MercadoCerradoError as e:
            print(f"⚠️  {e}")
            print("   Proporciona el stop loss manualmente:")
            print(f"   python strategy.py /add {ticker} {entry_price} <STOP_LOSS>")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error descargando datos para calcular stop loss: {e}")
            print("   Proporciona el stop loss manualmente:")
            print(f"   python strategy.py /add {ticker} {entry_price} <STOP_LOSS>")
            sys.exit(1)

    # Calcular ratio ETF si el ticker es ETH-EUR
    etf_ratio = None
    if ticker == ETH_TICKER:
        print(f"ℹ️  Ticker es ETH-EUR. Intentando obtener ratio con {ETF_TICKER}...")
        try:
            time.sleep(2)
            etf_data = fetch_etf_price()
            if etf_data:
                etf_ratio = etf_data["precio"] / entry_price
                stop_etf  = stop_loss * etf_ratio
                print(f"   {ETF_TICKER} precio: {etf_data['precio']:.4f}")
                print(f"   Ratio ETF/ETH: {etf_ratio:.6f}")
                print(f"   Stop Loss en {ETF_TICKER}: {stop_etf:.4f}")
            else:
                print(f"   No se pudo obtener el precio de {ETF_TICKER}. Se guardará sin ratio.")
        except Exception as e:
            print(f"   Advertencia: no se pudo calcular ratio ETF: {e}")

    # Guardar estado
    fecha_hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {
        "position_open": True,
        "ticker":        ticker,
        "name":          asset["name"],
        "currency":      asset["currency"],
        "entry_price":   entry_price,
        "stop_loss":     round(stop_loss, 6),
        "etf_ratio":     round(etf_ratio, 6) if etf_ratio else None,
    }
    save_state(state)

    # Registrar en historial
    add_to_historial({
        "fecha":    fecha_hoy,
        "accion":   ticker,
        "nombre":   asset["name"],
        "tipo":     "COMPRA (manual /add)",
        "precio":   round(entry_price, 4),
        "ganancia": "—",
        "currency": asset["currency"],
    })

    print(f"""
✅ Posición inicializada correctamente.
   Ticker:         {ticker} ({asset['name']})
   Moneda:         {asset['currency']}
   Precio entrada: {entry_price:.4f}
   Stop Loss:      {stop_loss:.4f}
   ETF Ratio:      {etf_ratio:.6f if etf_ratio else 'N/A'}
   Registrado en historial como COMPRA (manual /add).
""")


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
        key_cols = ["sma200", "adx", "atr", "don_high_prev", "don_low", "roc20"]
        if any(pd.isna(row.get(c, float("nan"))) for c in key_cols):
            logger.warning(f"[{ticker}] NaN en indicadores, saltando")
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
            price >= don_high_prev        # cierre >= máximo Donchian del día anterior
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


# ── Entrypoint CLI ────────────────────────────────────────────────────────────

def main():
    # Detectar si el primer argumento es un comando slash
    args = sys.argv[1:]

    if not args:
        # Análisis normal
        import pprint
        pprint.pprint(run_analysis())
        return

    comando = args[0].lower()

    # ── /help ──────────────────────────────────────────────────────────────
    if comando == "/help":
        cmd_help()

    # ── /reset ─────────────────────────────────────────────────────────────
    elif comando == "/reset":
        confirm = "--confirm" in args
        cmd_reset(confirm=confirm)

    # ── /add ───────────────────────────────────────────────────────────────
    elif comando == "/add":
        # Sintaxis: /add <TICKER> <PRECIO_ENTRADA> [STOP_LOSS]
        if len(args) < 3:
            print("❌ Uso: python strategy.py /add <TICKER> <PRECIO_ENTRADA> [STOP_LOSS]")
            print("   Ejemplo: python strategy.py /add NVDA 950.50")
            print("   Ejemplo: python strategy.py /add ETH-EUR 2100.00 1850.00")
            sys.exit(1)

        ticker_arg = args[1]
        try:
            entry_price_arg = float(args[2])
        except ValueError:
            print(f"❌ PRECIO_ENTRADA inválido: '{args[2]}'. Debe ser un número.")
            sys.exit(1)

        stop_loss_arg = None
        if len(args) >= 4:
            try:
                stop_loss_arg = float(args[3])
            except ValueError:
                print(f"❌ STOP_LOSS inválido: '{args[3]}'. Debe ser un número.")
                sys.exit(1)

        cmd_add(ticker_arg, entry_price_arg, stop_loss_arg)

    # ── Comando desconocido ────────────────────────────────────────────────
    else:
        print(f"❌ Comando desconocido: '{args[0]}'")
        print("   Ejecuta 'python strategy.py /help' para ver los comandos disponibles.")
        sys.exit(1)


if __name__ == "__main__":
    main()
