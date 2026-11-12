"""Server Web e runner per Hugging Face Spaces.

Avvia una dashboard HTTP leggera sulla porta 7860 (richiesta da Hugging Face)
e contestualmente esegue il loop di monitoraggio Amazon in background.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

# Importa i componenti del bot
from database import init_db
from main import _load_config, _main_loop, _setup_logging

# Stato in memoria condiviso per la dashboard web
BOT_STATS: dict[str, Any] = {
    "status": "Avvio in corso...",
    "start_time": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC"),
    "last_scan_time": "In attesa del primo ciclo...",
    "products_last_scan": 0,
    "alerts_sent_total": 0,
    "errors_count": 0,
}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PrezzoMin v2 — Amazon Price Error Monitor</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px; width: 100%; max-width: 650px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); }
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; border-bottom: 1px solid #334155; padding-bottom: 16px; }
        .title { font-size: 24px; font-weight: 700; color: #38bdf8; display: flex; align-items: center; gap: 10px; }
        .badge { background: #065f46; color: #34d399; padding: 6px 14px; border-radius: 9999px; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
        .stat-box { background: #0f172a; border: 1px solid #334155; border-radius: 12px; padding: 16px; }
        .stat-label { font-size: 13px; color: #94a3b8; text-transform: uppercase; margin-bottom: 6px; }
        .stat-value { font-size: 18px; font-weight: 600; color: #f1f5f9; }
        .footer { font-size: 13px; color: #64748b; text-align: center; border-top: 1px solid #334155; padding-top: 16px; }
        .tip { background: #172554; border-left: 4px solid #3b82f6; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; color: #bfdbfe; }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <div class="title">⚡ PrezzoMin v2</div>
            <div class="badge">● Online 24/7</div>
        </div>
        
        <div class="tip">
            💡 <strong>Stato Cloud:</strong> Il bot sta monitorando Amazon Italia e Idealo in tempo reale. Le notifiche vengono inviate direttamente su Telegram.
        </div>

        <div class="grid">
            <div class="stat-box">
                <div class="stat-label">Stato Monitoraggio</div>
                <div class="stat-value" id="status">Attivo</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Avvio Server</div>
                <div class="stat-value">{start_time}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Ultima Scansione</div>
                <div class="stat-value">{last_scan_time}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Prodotti Esaminati</div>
                <div class="stat-value">{products_last_scan}</div>
            </div>
        </div>

        <div class="footer">
            Hugging Face Spaces • Playwright Chromium • 0€ Bolletta
        </div>
    </div>
</body>
</html>
"""


async def handle_dashboard(request: web.Request) -> web.Response:
    """Restituisce la pagina HTML di stato."""
    html = HTML_TEMPLATE.format(
        start_time=BOT_STATS["start_time"],
        last_scan_time=BOT_STATS["last_scan_time"],
        products_last_scan=BOT_STATS["products_last_scan"],
    )
    return web.Response(text=html, content_type="text/html")


async def handle_health(request: web.Request) -> web.Response:
    """Endpoint JSON leggero per healthcheck e ping UptimeRobot."""
    return web.json_response({"status": "healthy", **BOT_STATS})


async def run_web_server(port: int = 7860) -> None:
    """Avvia il server aiohttp per la dashboard di Hugging Face."""
    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Dashboard Web attiva su http://0.0.0.0:%d", port)


async def main_async() -> None:
    """Esegue contemporaneamente la dashboard web e il loop del bot."""
    _setup_logging()
    logger = logging.getLogger("prezzomin")
    logger.info("Avvio PrezzoMin v2 su Hugging Face Spaces")

    config = _load_config()
    init_db()

    # Porta richiesta da Hugging Face Spaces (default 7860)
    port = int(os.environ.get("PORT", 7860))

    # Avvia la dashboard web in background
    await run_web_server(port)

    # Avvia il loop di monitoraggio Amazon
    await _main_loop(config)


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Arresto manuale.")
