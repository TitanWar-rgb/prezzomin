"""Modulo database per il tracciamento delle notifiche inviate.

Usa SQLite per memorizzare gli ASIN già notificati ed evitare duplicati
entro una finestra temporale configurabile.
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger(__name__)


def get_db_path() -> Path:
    """Restituisce il percorso del database SQLite per il worker corrente."""
    db_name = os.environ.get("DB_NAME", "prezzomin.db").strip()
    path = Path(__file__).parent / db_name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _connection() -> Generator[sqlite3.Connection, None, None]:
    """Context manager che apre e chiude una connessione SQLite."""
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Crea la tabella notified_asins se non esiste."""
    path = get_db_path()
    with _connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notified_asins (
                asin             TEXT PRIMARY KEY,
                title            TEXT,
                price            REAL,
                original_price   REAL,
                discount_percent REAL,
                notified_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    logger.info("Database inizializzato: %s", path)


def _get_all_db_paths() -> list[Path]:
    """Restituisce tutti i percorsi dei database SQLite: quello corrente e quelli degli altri worker."""
    current = get_db_path().resolve()
    db_paths = {current}

    # Cerca tutti i database presenti nella cartella db/ (es. worker_*.db)
    db_dir = Path(__file__).parent / "db"
    if db_dir.exists() and db_dir.is_dir():
        for p in db_dir.glob("*.db"):
            db_paths.add(p.resolve())

    # Includi eventuale prezzomin.db nella root se esiste
    root_db = (Path(__file__).parent / "prezzomin.db").resolve()
    if root_db.exists():
        db_paths.add(root_db)

    return list(db_paths)


def is_recently_notified(asin: str, hours: int = 24) -> bool:
    """Controlla se un ASIN è stato notificato nelle ultime *hours* ore
    nel database corrente o in qualsiasi altro database dei worker (cartella db/).

    Args:
        asin: Codice ASIN del prodotto.
        hours: Finestra temporale in ore.

    Returns:
        True se già notificato entro la finestra da qualsiasi worker, False altrimenti.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    db_paths = _get_all_db_paths()

    for path in db_paths:
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path), timeout=3.0)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT notified_at FROM notified_asins WHERE asin = ?",
                    (asin,),
                ).fetchone()
                if row and row["notified_at"]:
                    try:
                        notified = datetime.fromisoformat(str(row["notified_at"]))
                        if notified.tzinfo is None:
                            notified = notified.replace(tzinfo=timezone.utc)
                        if notified > cutoff:
                            logger.debug(
                                "ASIN %s già notificato nel database %s (il %s)",
                                asin, path.name, notified,
                            )
                            return True
                    except (ValueError, TypeError):
                        pass
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.debug("Errore lettura database %s durante deduplicazione: %s", path.name, exc)
            continue

    return False


def mark_notified(
    asin: str,
    title: str,
    price: float,
    original_price: float,
    discount_percent: float,
) -> None:
    """Inserisce o aggiorna un record di notifica per l'ASIN dato.

    Args:
        asin: Codice ASIN del prodotto.
        title: Titolo del prodotto.
        price: Prezzo corrente in euro.
        original_price: Prezzo originale in euro.
        discount_percent: Percentuale di sconto rilevata.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO notified_asins
                (asin, title, price, original_price, discount_percent, notified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (asin, title, price, original_price, discount_percent, now),
        )
        conn.commit()
    logger.debug("ASIN %s marcato come notificato", asin)


def cleanup_old_entries(hours: int = 72) -> None:
    """Elimina i record più vecchi di *hours* ore.

    Args:
        hours: Età massima dei record in ore.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM notified_asins WHERE notified_at < ?",
            (cutoff,),
        )
        conn.commit()
    deleted = cursor.rowcount
    if deleted:
        logger.info("Pulizia DB: rimossi %d record vecchi (>%dh)", deleted, hours)
