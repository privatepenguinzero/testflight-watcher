"""Verifica che il ciclo di sorveglianza sia ancora vivo.

    python -m testflight_watcher.healthcheck   # 0 = sano, 1 = fermo

Serve a chiudere l'ultimo modo che questo bot ha di mentire in silenzio: se il
thread di monitoraggio si bloccasse, il bot Telegram continuerebbe a rispondere
a /list mostrando gli ultimi stati salvati e sembrerebbe perfettamente vivo,
mentre da ore non controlla più niente.

Guarda `last_cycle`, non il `last_checked` delle singole app: un'app in backoff
o rifiutata da Apple ha un `last_checked` vecchio pur essendo tutto in ordine.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from .config import DEFAULT_CHECK_INTERVAL
from .store import Store

# Quanti cicli di ritardo si tollerano prima di dichiarare il monitor fermo.
# Tre lascia spazio a un ciclo lento o a una rete incerta senza far rimbalzare
# il container al primo intoppo.
CYCLES_TOLERATED = 3
MIN_TOLERANCE_SECONDS = 120


def _interval() -> int:
    try:
        return max(1, int(os.getenv("CHECK_INTERVAL", "") or DEFAULT_CHECK_INTERVAL))
    except ValueError:
        return DEFAULT_CHECK_INTERVAL


def tolerance_seconds(interval: int) -> int:
    return max(interval * CYCLES_TOLERATED, MIN_TOLERANCE_SECONDS)


def staleness(last_cycle: str | None, now: datetime | None = None) -> float | None:
    """Secondi trascorsi dall'ultimo giro completato, None se mai avvenuto."""
    if not last_cycle:
        return None
    try:
        visto = datetime.strptime(last_cycle, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - visto).total_seconds()


def check(store: Store, interval: int, now: datetime | None = None) -> tuple[bool, str]:
    limite = tolerance_seconds(interval)
    eta = staleness(store.last_cycle(), now)

    if eta is None:
        # All'avvio il primo giro non è ancora avvenuto: se ne occupa
        # `start_period` nel compose, qui non dichiariamo un guasto.
        return True, "nessun ciclo registrato (avvio in corso)"
    if eta > limite:
        return False, f"ultimo ciclo {eta:.0f}s fa, limite {limite}s: monitor fermo"
    return True, f"ultimo ciclo {eta:.0f}s fa (limite {limite}s)"


def main() -> int:
    store = Store(os.getenv("DB_FILE", "/app/data/data.json"))
    sano, motivo = check(store, _interval())
    print(motivo)
    return 0 if sano else 1


if __name__ == "__main__":
    sys.exit(main())
