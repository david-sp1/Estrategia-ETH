# ETH Trend-Sustainer Bot 🤖📈

Bot de Telegram que implementa el algoritmo "Trend-Sustainer" para ETH/USDT.
Datos tomados de **Binance API pública** (sin API key).

## Variables de entorno (Railway)

| Variable | Obligatoria | Descripción |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Token del bot (BotFather) |
| `CHAT_ID` | ✅ | Tu chat ID de Telegram |
| `DAILY_HOUR` | ❌ | Hora UTC del informe diario (default: `8`) |
| `TIMEZONE` | ❌ | Zona horaria (default: `Europe/Madrid`) |
| `STATE_FILE` | ❌ | Ruta del estado (default: `/data/state.json`) |

## Comandos Telegram

| Comando | Descripción |
|---|---|
| `/start` | Bienvenida e instrucciones |
| `/estado` | Análisis completo en tiempo real |
| `/help` | Ayuda |

## Cómo desplegar en Railway

1. **Crea el bot en Telegram:**
   - Habla con [@BotFather](https://t.me/BotFather)
   - `/newbot` → guarda el token

2. **Obtén tu Chat ID:**
   - Habla con [@userinfobot](https://t.me/userinfobot)
   - Te dirá tu ID numérico

3. **Sube a GitHub:**
   ```bash
   git init
   git add .
   git commit -m "ETH Trend-Sustainer bot"
   git remote add origin https://github.com/TU_USUARIO/eth-bot.git
   git push -u origin main
   ```

4. **En Railway:**
   - New Project → Deploy from GitHub repo
   - Selecciona tu repo
   - En Variables, añade `TELEGRAM_TOKEN` y `CHAT_ID`
   - Railway detectará el Dockerfile automáticamente

5. **Volumen persistente (opcional pero recomendado):**
   - En Railway → tu servicio → Volumes
   - Mount path: `/data`
   - Esto persiste el estado de tu posición entre reinicios

## Lógica del algoritmo

- **Entrada:** Precio > SMA200 + ADX > 25 + ruptura Donchian 50
- **Salida:** Precio < SMA200 O precio < Stop Loss
- **Stop Loss:** `max(Precio - ATR×3.5, Donchian Low)` con trinquete (nunca baja)
- **Datos:** Velas diarias ETHUSDT de Binance

## Fuente de datos

Binance API pública: `https://api.binance.com/api/v3/klines`
No requiere cuenta ni API key.
