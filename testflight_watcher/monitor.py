"""Il ciclo di sorveglianza.

Orchestra e basta: scarica (client), classifica (detector), registra (store),
avvisa (notifier). Le decisioni su *quando* svegliare l'utente vivono qui, la
decisione su *cosa* stia succedendo vive nel detector.
"""

from __future__ import annotations

import logging
import random
import time

from .client import join_url
from .detector import ST_ERROR, ST_INVALID, ST_OPEN, ST_UNKNOWN, classify
from .notifier import esc

log = logging.getLogger(__name__)

# Un'app che continua a fallire viene saltata per un numero crescente di giri,
# invece di essere ritentata a ogni ciclo. Il loop non rallenta: le altre app
# vengono controllate normalmente.
MAX_BACKOFF_CYCLES = 32

# Un rifiuto esplicito (403/429) non è un guasto passeggero: ritentare al giro
# dopo è il modo migliore per farsi bloccare più a lungo. Si riparte da una
# pausa già sostanziosa.
BLOCKED_INITIAL_CYCLES = 8

# Con più app in lista le richieste partirebbero tutte insieme a ogni ciclo.
# Distanziarle riduce il picco istantaneo e assomiglia di più a un uso umano.
DEFAULT_STAGGER = (2.0, 8.0)


class Monitor:
    def __init__(self, store, client, notifier, interval: int, stagger=DEFAULT_STAGGER):
        self._store = store
        self._client = client
        self._notifier = notifier
        self._interval = interval
        self._stagger = stagger
        # Stato del backoff: volutamente in memoria, non nel file JSON. Al
        # riavvio si riparte puliti, che è il comportamento desiderato.
        self._failures: dict[str, int] = {}
        self._skip: dict[str, int] = {}
        # Ultima correlation-key vista per app, e per quali app abbiamo già
        # segnalato che le risposte arrivano dalla cache. In memoria: al
        # riavvio si riparte puliti.
        self._last_key: dict[str, str] = {}
        self._cache_warned: set[str] = set()
        self._blocks: dict[str, int] = {}
        self._block_warned: set[str] = set()

    # ── Un singolo controllo ───────────────────────────────────────────────
    def check_app(self, tf_id: str, app: dict) -> None:
        name = app.get("name", tf_id)
        prev = app.get("state")

        try:
            fetched = self._client.fetch(tf_id)
        except Exception as e:
            self._register_failure(tf_id, name, e)
            return

        if fetched.blocked:
            self._register_block(tf_id, name, fetched)
            return

        self._check_freshness(tf_id, name, fetched)

        detection = classify(fetched.status_code, fetched.body)

        if detection.state == ST_ERROR:
            self._register_failure(tf_id, name, detection.detail)
            return

        # Successo: il backoff si azzera.
        self._failures.pop(tf_id, None)
        self._skip.pop(tf_id, None)
        self._blocks.pop(tf_id, None)
        self._block_warned.discard(tf_id)

        self._alert_if_anomalous(tf_id, name, app, detection)
        self._notify_if_changed(tf_id, name, prev, detection)

        self._store.record_check(
            tf_id,
            state=detection.state,
            detail=detection.detail,
            anomaly_fingerprint=detection.fingerprint,
        )

    def _register_failure(self, tf_id: str, name: str, reason) -> None:
        """Errore di rete o HTTP inatteso.

        Lo stato memorizzato NON viene toccato: un guasto momentaneo non deve
        cancellare l'ultimo stato noto, né far scattare una notifica quando la
        rete torna.
        """
        count = self._failures.get(tf_id, 0) + 1
        self._failures[tf_id] = count
        self._skip[tf_id] = min(2 ** (count - 1), MAX_BACKOFF_CYCLES)
        log.warning(
            "Controllo fallito per %s (%s): %s — riprovo fra %d giri",
            name, tf_id, reason, self._skip[tf_id],
        )

    def _register_block(self, tf_id: str, name: str, fetched) -> None:
        """Apple ci sta rifiutando: ci si ferma a lungo e si avvisa.

        Diverso da un guasto di rete. Insistere al giro dopo è il modo
        migliore per farsi bloccare più a lungo, quindi si riparte da una
        pausa già ampia e la si raddoppia a ogni rifiuto successivo.

        Lo stato del beta non viene toccato: non sappiamo com'è messo, e
        l'ultimo valore noto è più utile di una supposizione.
        """
        count = self._blocks.get(tf_id, 0) + 1
        self._blocks[tf_id] = count

        if fetched.retry_after:
            # Se il server dice quanto aspettare, si fa esattamente quello.
            cicli = max(1, -(-fetched.retry_after // self._interval))
            motivo = f"Retry-After: {fetched.retry_after}s"
        else:
            cicli = min(BLOCKED_INITIAL_CYCLES * 2 ** (count - 1), MAX_BACKOFF_CYCLES)
            motivo = "nessun Retry-After, uso il backoff"

        self._skip[tf_id] = cicli
        attesa_min = round(cicli * self._interval / 60)
        log.warning(
            "[%s] rifiutato da Apple (HTTP %s, %s): pausa di %d giri (~%d min)",
            tf_id, fetched.status_code, motivo, cicli, attesa_min,
        )

        if tf_id in self._block_warned:
            return
        self._block_warned.add(tf_id)
        self._notifier.send(
            "🚫 <b>Richieste rifiutate da Apple</b>\n"
            f"📱 App: <b>{esc(name)}</b> (<code>{esc(tf_id)}</code>)\n"
            f"HTTP <code>{fetched.status_code}</code> — mi fermo per ~{attesa_min} minuti.\n"
            "Se succede spesso, alza <code>CHECK_INTERVAL</code>."
        )

    def _check_freshness(self, tf_id: str, name: str, fetched) -> None:
        """Verifica che Apple stia generando la risposta, non ripescandola.

        Il cache-buster in query string ottiene oggi una risposta fresca a ogni
        richiesta. Se Apple iniziasse a includere la query string nella chiave
        di cache smetterebbe di funzionare *in silenzio*, e torneremmo a
        leggere copie vecchie fino a dieci minuti senza accorgercene: lo stesso
        schema di guasto che ha reso questo bot inutile in passato.

        La correlation-key identifica la risposta generata: se si ripete fra
        due controlli, stiamo rileggendo la stessa copia.

        Non tocca mai lo stato del beta: è una diagnosi sul trasporto.
        """
        key = fetched.correlation_key
        if not key:
            return

        precedente = self._last_key.get(tf_id)
        self._last_key[tf_id] = key

        if precedente is None or key != precedente:
            self._cache_warned.discard(tf_id)
            return

        if tf_id in self._cache_warned:
            return  # già segnalato, non ripetiamo a ogni giro
        self._cache_warned.add(tf_id)

        log.warning(
            "[%s] risposta servita dalla cache: correlation-key ripetuta (%s)", tf_id, key
        )
        self._notifier.send(
            "🕰 <b>Risposte dalla cache</b>\n"
            f"📱 App: <b>{esc(name)}</b> (<code>{esc(tf_id)}</code>)\n"
            "Apple sta restituendo la stessa copia invece di rigenerarla: il "
            "cache-buster non fa più effetto.\n"
            "⚠️ I controlli possono essere vecchi fino a 10 minuti."
        )

    # ── Decisioni di notifica ──────────────────────────────────────────────
    def _alert_if_anomalous(self, tf_id: str, name: str, app: dict, detection) -> None:
        """Avvisa quando i segnali non sono affidabili, una volta sola.

        È la rete di sicurezza del design: se Apple cambia la pagina, l'utente
        lo scopre da un messaggio invece che dal silenzio del bot.
        """
        if not detection.anomaly:
            return
        if detection.fingerprint == app.get("anomaly_fingerprint"):
            return  # stessa anomalia già segnalata

        log.warning(
            "[%s] anomalia: %s (testo: %r)", tf_id, detection.anomaly, detection.detail
        )
        self._notifier.send(
            "🛠 <b>Rilevamento incerto</b>\n"
            f"📱 App: <b>{esc(name)}</b> (<code>{esc(tf_id)}</code>)\n"
            f"⚠️ {esc(detection.anomaly)}\n"
            f"📄 Testo letto: <code>{esc(detection.detail or '(vuoto)')}</code>\n"
            f"➡️ Stato assunto: <b>{esc(detection.state)}</b>"
        )

    def _notify_if_changed(self, tf_id: str, name: str, prev, detection) -> None:
        if detection.state == prev:
            return

        if detection.state == ST_OPEN:
            log.info("✅ Disponibile: %s (%s)", name, tf_id)
            self._notifier.send(
                "✅ <b>Posto disponibile!</b>\n"
                f"📱 App: <b>{esc(name)}</b>\n"
                f"🔗 <a href='{join_url(tf_id)}'>Apri TestFlight</a>"
            )
        elif detection.state == ST_INVALID:
            log.warning("⚠️ Link non valido: %s (%s)", name, tf_id)
            self._notifier.send(
                f"⚠️ <b>Link non valido</b>\n📱 App: <b>{esc(name)}</b> "
                f"(<code>{esc(tf_id)}</code>)\nIl beta è stato rimosso da Apple."
            )
        elif detection.state == ST_UNKNOWN:
            # Nessuna notifica di stato: l'avviso di anomalia l'ha già coperta.
            log.info("[%s] %s: %s → unknown", tf_id, name, prev)
        else:
            log.info("[%s] %s: %s → %s", tf_id, name, prev, detection.state)

    # ── Il giro ────────────────────────────────────────────────────────────
    def check_once(self) -> None:
        primo = True
        for tf_id, app in list(self._store.apps().items()):
            restanti = self._skip.get(tf_id, 0)
            if restanti > 0:
                self._skip[tf_id] = restanti - 1
                continue

            # Pausa fra un'app e l'altra, ma non prima della prima: con più
            # app in lista le richieste partirebbero altrimenti tutte insieme.
            if not primo:
                self._pause_between_apps()
            primo = False

            self.check_app(tf_id, app)

    def _pause_between_apps(self) -> None:
        minimo, massimo = self._stagger
        if massimo > 0:
            time.sleep(random.uniform(minimo, massimo))

    def run_forever(self) -> None:
        log.info("▶ Avvio sorveglianza (ogni %ds circa)", self._interval)
        while True:
            try:
                self.check_once()
            except Exception as e:
                # Il thread non deve mai morire: senza questo un errore
                # imprevisto spegneva il monitoraggio lasciando vivo il bot,
                # che continuava a rispondere come se tutto funzionasse.
                log.exception("Errore nel ciclo di sorveglianza: %s", e)
            time.sleep(self._next_delay())

    def _next_delay(self) -> float:
        """Intervallo con jitter ±10%, per non bussare a cadenza esatta."""
        return self._interval * random.uniform(0.9, 1.1)
