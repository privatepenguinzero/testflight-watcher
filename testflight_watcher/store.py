"""Persistenza dello stato su file JSON.

Il bot (thread asyncio) e il monitor (thread separato) scrivono sullo stesso
file. Senza lock il monitor sovrascriveva le aggiunte e rimozioni fatte
dall'utente durante un giro di controlli, che sparivano senza traccia.

Le scritture sono atomiche (file temporaneo + fsync + os.replace): un crash a
metà dump lascerebbe altrimenti un JSON troncato, che al riavvio verrebbe letto
come "file corrotto" e azzerato.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from threading import RLock

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank_app(name: str) -> dict:
    return {
        "name": name,
        "state": None,
        "detail": "",
        "last_checked": None,
        "anomaly_fingerprint": None,
    }


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = RLock()

    # ── Lettura e scrittura ────────────────────────────────────────────────
    def load(self) -> dict:
        with self._lock:
            if not os.path.exists(self.path):
                db = {"version": SCHEMA_VERSION, "apps": {}}
                self._save(db)
                return db

            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    db = json.load(f)
            except (OSError, ValueError) as e:
                # Messo da parte, non cancellato: se è un bug nostro il file
                # dell'utente è l'unica copia dei suoi ID.
                log.error("DB illeggibile (%s); lo sposto in .corrupt", e)
                try:
                    os.replace(self.path, self.path + ".corrupt")
                except OSError:
                    pass
                db = {"version": SCHEMA_VERSION, "apps": {}}
                self._save(db)
                return db

            if not isinstance(db, dict) or not isinstance(db.get("apps"), dict):
                log.error("DB con struttura inattesa; ne creo uno nuovo")
                db = {"version": SCHEMA_VERSION, "apps": {}}
                self._save(db)
            return db

    def _save(self, data: dict) -> None:
        with self._lock:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".data-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def apps(self) -> dict:
        return self.load()["apps"]

    # ── Migrazione di schema ───────────────────────────────────────────────
    def migrate(self) -> bool:
        """Porta il file allo schema corrente. Idempotente.

        v0: {"apps": {"ID": {"name": ..., "available": bool}}}
        v1: {"apps": {"ID": {"name": ..., "state": str}}}
        v2: aggiunge version, detail, last_checked, anomaly_fingerprint

        Prima di convertire salva una copia: la conversione è a senso unico e
        il file contiene gli ID che l'utente ha inserito a mano.
        """
        with self._lock:
            db = self.load()
            version = db.get("version", 0)
            if version == SCHEMA_VERSION and self._already_v2(db):
                return False

            if os.path.exists(self.path):
                backup = f"{self.path}.bak-v{version}"
                try:
                    shutil.copy2(self.path, backup)
                    log.info("Copia dello schema precedente salvata in %s", backup)
                except OSError as e:
                    log.warning("Non sono riuscito a salvare la copia %s: %s", backup, e)

            for tf_id, app in db["apps"].items():
                if "available" in app:
                    app.setdefault("state", "open" if app["available"] else "full")
                    del app["available"]
                    log.info("Migrato %s dallo schema v0", tf_id)
                app.setdefault("name", tf_id)
                app.setdefault("state", None)
                app.setdefault("detail", "")
                app.setdefault("last_checked", None)
                app.setdefault("anomaly_fingerprint", None)

            db["version"] = SCHEMA_VERSION
            self._save(db)
            log.info(
                "DB migrato allo schema v%d (%d app)", SCHEMA_VERSION, len(db["apps"])
            )
            return True

    @staticmethod
    def _already_v2(db: dict) -> bool:
        campi = ("name", "state", "detail", "last_checked", "anomaly_fingerprint")
        return all(
            all(c in app for c in campi) and "available" not in app
            for app in db["apps"].values()
        )

    # ── Operazioni dell'utente ─────────────────────────────────────────────
    def add(self, tf_id: str, name: str) -> bool:
        with self._lock:
            db = self.load()
            if tf_id in db["apps"]:
                return False
            db["apps"][tf_id] = _blank_app(name)
            self._save(db)
            return True

    def remove(self, tf_id: str) -> dict | None:
        with self._lock:
            db = self.load()
            app = db["apps"].pop(tf_id, None)
            if app is not None:
                self._save(db)
            return app

    def rename(self, tf_id: str, new_name: str) -> str | None:
        """Ritorna il nome precedente, None se l'ID non esiste."""
        with self._lock:
            db = self.load()
            app = db["apps"].get(tf_id)
            if app is None:
                return None
            old = app["name"]
            app["name"] = new_name
            self._save(db)
            return old

    # ── Aggiornamento dal monitor ──────────────────────────────────────────
    def record_check(
        self,
        tf_id: str,
        *,
        state: str,
        detail: str = "",
        anomaly_fingerprint: str | None = None,
    ) -> None:
        """Registra l'esito di un controllo.

        Rilegge il DB sotto lock: fra l'inizio del giro e questo momento
        l'utente può aver aggiunto o rimosso app dal bot, e non vanno perse.
        """
        with self._lock:
            db = self.load()
            app = db["apps"].get(tf_id)
            if app is None:
                return  # rimossa mentre il controllo era in corso
            app["state"] = state
            app["detail"] = detail
            app["last_checked"] = _now()
            app["anomaly_fingerprint"] = anomaly_fingerprint
            self._save(db)
