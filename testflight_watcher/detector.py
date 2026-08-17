"""Classificazione dello stato di un beta TestFlight.

Modulo puro: riceve un codice HTTP e una stringa HTML, restituisce uno stato.
Non apre socket e non tocca il disco, così si può provare sulle pagine reali
salvate in tests/fixtures/ senza rete.

La versione precedente riconosceva solo le frasi di chiusura e trattava come
"pieno" tutto ciò che non corrispondeva. Ma la pagina di un beta aperto non è
vuota: contiene l'invito a installare TestFlight, che non era fra i marker.
Per costruzione nessun beta aperto poteva essere rilevato.

Qui si incrociano due segnali indipendenti (vedi la spec, §3):

- strutturale: su un beta chiuso Apple genera i pulsanti ma ne svuota le
  etichette, quindi "esiste un pulsante con testo" equivale a "beta aperto".
  Non dipende dalle parole scelte da Apple, solo dalla forma della pagina.
- testuale: il contenuto di <div class="beta-status">.

Se concordano, quello è lo stato. Se discordano, lo stato è `unknown`: perché
il bot torni a mentire in silenzio Apple deve rompere entrambi i segnali
insieme e in modo coerente.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

# ── Stati ───────────────────────────────────────────────────────────────────
ST_OPEN = "open"
ST_FULL = "full"
ST_INVALID = "invalid"
ST_UNKNOWN = "unknown"
ST_ERROR = "error"

# ── Pattern ─────────────────────────────────────────────────────────────────
_BETA_STATUS_RE = re.compile(
    r'<div[^>]*\bclass\s*=\s*["\'][^"\']*\bbeta-status\b[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_DIV_TOKEN_RE = re.compile(r"<\s*(/?)div\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BUTTON_RE = re.compile(
    r'<a[^>]*\bclass\s*=\s*["\'][^"\']*\bbutton\b[^"\']*["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Frasi presenti quando il beta accetta iscrizioni. Il testo completo è
# "To join the <nome> beta, open the link on your iPhone, iPad, or Mac after
# you install TestFlight." — il nome dell'app varia, queste parti no.
OPEN_MARKERS = (
    "open the link on your",
    "after you install testflight",
)

# Frasi presenti quando il beta non è accessibile.
CLOSED_MARKERS = (
    "this beta is full",
    "isn't accepting any new testers",
    "is not accepting any new testers",
    "this beta has expired",
    "this beta isn't available",
    "this beta is not available",
    "this beta isn't accepting",
)


@dataclass(frozen=True)
class Detection:
    """Esito di una classificazione."""

    state: str
    detail: str = ""
    structural: str | None = None
    textual: str | None = None
    anomaly: str | None = None

    @property
    def fingerprint(self) -> str | None:
        """Impronta dell'anomalia, per non riavvisare due volte la stessa.

        Copre anche `detail`: se Apple cambia di nuovo il testo, è un'anomalia
        diversa e merita un avviso nuovo.
        """
        if not self.anomaly:
            return None
        raw = f"{self.anomaly}|{self.structural}|{self.textual}|{self.detail}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _clean(fragment: str) -> str:
    """Testo leggibile da un frammento di HTML."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())


def beta_status_text(body: str) -> str:
    """Testo dentro <div class="beta-status">, stringa vuota se assente.

    Il div contiene altri <div> annidati (l'icona dell'app), quindi serve
    contare la profondità: fermarsi al primo </div> restituirebbe il testo
    sbagliato.
    """
    m = _BETA_STATUS_RE.search(body)
    if not m:
        return ""

    start = m.end()
    depth = 1
    end = len(body)
    for tok in _DIV_TOKEN_RE.finditer(body, start):
        depth += -1 if tok.group(1) else 1
        if depth == 0:
            end = tok.start()
            break

    return _clean(body[start:end])


def button_labels(body: str) -> list[str]:
    """Etichette degli <a class="button">, incluse quelle vuote.

    Le stringhe vuote sono informative: sono il segnale di un beta chiuso.
    """
    return [_clean(inner) for inner in _BUTTON_RE.findall(body)]


def structural_signal(body: str) -> str | None:
    """Stato dedotto dalla forma della pagina.

    None se non c'è nemmeno un pulsante: la pagina non è quella che ci
    aspettiamo e il segnale non è utilizzabile.
    """
    labels = button_labels(body)
    if not labels:
        return None
    return ST_OPEN if any(labels) else ST_FULL


def textual_signal(status_text: str) -> str | None:
    """Stato dedotto dal testo di beta-status. None se non riconosciuto."""
    if not status_text:
        return None
    low = status_text.lower()
    if any(marker in low for marker in OPEN_MARKERS):
        return ST_OPEN
    if any(marker in low for marker in CLOSED_MARKERS):
        return ST_FULL
    return None


def classify(status_code: int, body: str) -> Detection:
    """Stato di un beta a partire dalla risposta HTTP.

    Il codice HTTP decide da solo; la tabella a due segnali si applica solo
    alle risposte 200, le uniche con un corpo da interpretare.
    """
    if status_code == 404:
        return Detection(state=ST_INVALID, detail="link non valido o rimosso")
    if status_code != 200:
        return Detection(state=ST_ERROR, detail=f"HTTP {status_code}")

    detail = beta_status_text(body)
    structural = structural_signal(body)
    textual = textual_signal(detail)

    def build(state: str, anomaly: str | None) -> Detection:
        return Detection(
            state=state,
            detail=detail,
            structural=structural,
            textual=textual,
            anomaly=anomaly,
        )

    # Entrambi i segnali leggibili.
    if structural is not None and textual is not None:
        if structural == textual:
            return build(structural, None)
        return build(
            ST_UNKNOWN,
            f"segnali discordi: pagina dice {structural}, testo dice {textual}",
        )

    # Un solo segnale leggibile: si procede in modalità degradata, ma lo si
    # segnala. Meglio un bot che funziona e avvisa di essere mezzo cieco che
    # uno che si blocca del tutto.
    if structural is not None:
        return build(structural, "testo di beta-status non riconosciuto")
    if textual is not None:
        return build(textual, "nessun pulsante trovato nella pagina")

    # Nessuno dei due: la pagina non è più quella che conosciamo.
    return build(ST_UNKNOWN, "pagina irriconoscibile")
