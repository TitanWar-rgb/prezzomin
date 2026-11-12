# PrezzoMin

**Monitoraggio automatico degli errori di prezzo su Amazon Italia.**

PrezzoMin è un bot che scansiona periodicamente le pagine di ricerca di Amazon.it alla ricerca di prodotti con sconti anomali (errori di prezzo). Quando trova un'offerta sospetta su un prodotto di marca, invia un alert istantaneo tramite Telegram.

## Funzionalità

- 🔍 Scansione automatica di categorie configurabili su Amazon.it
- 🏷️ Filtro per brand (Apple, Samsung, Logitech, ecc.)
- 🚫 Blacklist per escludere accessori, cover e ricambi
- 📊 Rilevamento anomalie basato su percentuale di sconto e prezzo assoluto
- 📱 Notifiche Telegram in tempo reale con link diretto al prodotto
- 🛡️ Anti-duplicati: ogni ASIN viene notificato al massimo una volta ogni 24 ore
- 🕵️ Navigazione stealth con Playwright per ridurre il rischio di blocco

## Funzionalità Avanzate (v2)

### ⚡ Resource Blocking per velocità
Le pagine vengono caricate bloccando automaticamente immagini, font, stylesheet e tracker pubblicitari (Google Analytics, Facebook, Criteo, Hotjar, ecc.). Questo riduce drasticamente il tempo di caricamento e il consumo di RAM.

### 📦 Supporto Amazon Warehouse Deals
PrezzoMin può scansionare anche le pagine Amazon Warehouse / Seconda Mano per trovare prodotti ricondizionati o in condizioni "Come nuovo" a prezzi eccezionali. La condizione del prodotto (Nuovo, Come nuovo, Ottime condizioni, ecc.) viene estratta e mostrata nell'alert. Soglia di sconto separata configurabile (`MIN_DISCOUNT_PERCENT_WAREHOUSE`).

### 🏪 Validazione prezzi con Idealo
Ogni prodotto candidato viene confrontato con il miglior prezzo disponibile su Idealo.it. Questo permette di distinguere un vero errore di prezzo (prezzo Amazon molto sotto il mercato) da un finto sconto con listino gonfiato. Il risparmio reale rispetto a Idealo viene calcolato e mostrato nell'alert.

### 🎯 Sistema di alert a due livelli
- **🚨 ERRORE DI PREZZO** — prodotto con prezzo ≥45% sotto il miglior prezzo Idealo, oppure prodotto di marca sotto la soglia di prezzo assoluto.
- **🔥 SUPER AFFARE** — prodotto con prezzo tra il 25% e il 45% sotto Idealo.
- Se Idealo non è disponibile, il sistema usa lo sconto Amazon come fallback.

### 🛡️ Auto-recovery da CAPTCHA
Se Amazon presenta un CAPTCHA, il bot:
1. Chiude il browser context corrente.
2. Attende 10-15 minuti (con countdown loggato).
3. Ricrea il context con un User-Agent e viewport diversi.
4. Riprende lo scraping dall'URL interrotto.
5. Se rileva 3+ CAPTCHA consecutivi, estende la pausa a 30 minuti.

## Prerequisiti

- **Python 3.10** o superiore
- Un **bot Telegram** (vedi sezione configurazione)
- Connessione a internet

## Installazione

1. Clona o scarica il progetto:
   ```bash
   git clone <url-del-repo>
   cd prezzomin
   ```

2. Installa le dipendenze Python:
   ```bash
   pip install -r requirements.txt
   ```

3. Installa il browser Chromium per Playwright:
   ```bash
   playwright install chromium
   ```

## Configurazione del Bot Telegram

1. Apri Telegram e cerca **@BotFather** (https://t.me/BotFather).
2. Invia il comando `/newbot` e segui le istruzioni per creare un nuovo bot.
3. Copia il **token** fornito da BotFather (es. `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`).
4. Per ottenere il tuo **Chat ID**:
   - Invia un messaggio qualsiasi al tuo bot.
   - Apri nel browser: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - Cerca il campo `"chat":{"id": XXXXXXX}` — quel numero è il tuo Chat ID.
5. Apri `config.json` e inserisci il token e il Chat ID:
   ```json
   {
     "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
     "TELEGRAM_CHAT_ID": "987654321"
   }
   ```

## Configurazione di `config.json`

| Parametro                      | Descrizione                                                         |
| ------------------------------ | ------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`          | Token del bot Telegram                                              |
| `TELEGRAM_CHAT_ID`            | ID della chat dove inviare le notifiche                             |
| `MIN_DISCOUNT_PERCENT`        | Sconto minimo (%) per considerare un prodotto anomalo               |
| `MAX_ABS_PRICE_FOR_BRANDS`    | Prezzo massimo (€) sotto il quale il prodotto è comunque segnalato  |
| `SCAN_INTERVAL_MIN`           | Intervallo minimo tra le scansioni (secondi)                        |
| `SCAN_INTERVAL_MAX`           | Intervallo massimo tra le scansioni (secondi)                       |
| `NOTIFICATION_COOLDOWN_HOURS` | Ore minime tra due notifiche per lo stesso ASIN                     |
| `SEARCH_URLS`                 | Lista di URL di ricerca Amazon da monitorare                        |
| `BRAND_WHITELIST`             | Lista di brand da monitorare                                        |
| `BLACKLIST_KEYWORDS`          | Parole chiave per escludere prodotti indesiderati                   |
| `ENABLE_WAREHOUSE_DEALS`      | Abilita scansione Amazon Warehouse (true/false)                     |
| `WAREHOUSE_SEARCH_URLS`       | URL di ricerca per prodotti Warehouse / Seconda Mano                |
| `MIN_DISCOUNT_PERCENT_WAREHOUSE` | Sconto minimo (%) per prodotti Warehouse                         |

## Avvio

```bash
python main.py
```

Il bot invierà un messaggio di conferma su Telegram e inizierà la scansione.

## Esecuzione in background

### Windows

- Usa `pythonw main.py` per eseguire senza finestra della console.
- In alternativa, configura un'**Attività pianificata** (Task Scheduler) per l'avvio automatico:
  1. Apri *Utilità di pianificazione*.
  2. Crea una nuova attività di base.
  3. Imposta il trigger (all'avvio del sistema o a un orario specifico).
  4. Come azione seleziona *Avvia un programma*: `pythonw.exe` con argomento `main.py` e directory di avvio la cartella del progetto.

### macOS / Linux

```bash
nohup python main.py &
```

Oppure crea un servizio systemd:

```ini
# /etc/systemd/system/prezzomin.service
[Unit]
Description=PrezzoMin - Monitoraggio Errori di Prezzo
After=network.target

[Service]
Type=simple
User=tuo_utente
WorkingDirectory=/percorso/a/prezzomin
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Poi attiva il servizio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable prezzomin
sudo systemctl start prezzomin
```

### Cloud 24/7 Gratis (Hugging Face Spaces - Senza carta di credito)

1. Registrati gratis su [huggingface.co](https://huggingface.co).
2. Crea un **New Space** selezionando l'SDK **Docker** (Blank).
3. In **Settings -> Variables and secrets** aggiungi `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`.
4. Carica tutti i file del progetto nella scheda **Files**.
5. Il bot si avvierà automaticamente e mostrerà la dashboard web sulla porta 7860.

## Struttura del progetto

```
prezzomin/
├── app.py                # Web dashboard e runner per Hugging Face Spaces
├── main.py               # Entry point e loop principale
├── scraper.py            # Scraper Playwright per Amazon.it
├── price_validator.py    # Validatore prezzi tramite Idealo.it
├── notifier.py           # Invio notifiche Telegram
├── database.py           # Persistenza SQLite per anti-duplicati
├── Dockerfile            # Container per Hugging Face / Docker
├── config.json           # Configurazione (token, URL, filtri)
├── requirements.txt      # Dipendenze Python
└── README.md             # Questa documentazione
```

## Disclaimer

> **Questo progetto è solo a scopo educativo.**
> L'uso deve rispettare i [Termini di Servizio di Amazon](https://www.amazon.it/gp/help/customer/display.html?nodeId=201909000).
> L'autore non si assume alcuna responsabilità per usi impropri o violazioni dei ToS.
