# Refactor di testflight-watcher: rilevamento a doppio segnale e struttura a moduli

Data: 2026-08-17
Stato: approvato in brainstorming, in attesa di piano di implementazione

## 1. Il problema

Il bot non ha mai notificato un posto libero. La causa non è il parsing HTML,
che funziona, ma la politica di classificazione in `monitor.py:188-195`:

```python
low = status_text.lower()
if any(marker in low for marker in CLOSED_MARKERS):
    return ST_FULL, status_text
log.warning(...)
return ST_FULL, status_text   # <- tutto ciò che non è riconosciuto è "pieno"
```

Il codice riconosce solo le frasi di chiusura e considera chiuso tutto il resto.
Ma la pagina di un beta aperto non è vuota: contiene un invito a installare
TestFlight. Quel testo non è in `CLOSED_MARKERS`, finisce nel ramo "sconosciuto"
e diventa `full`.

**Per costruzione, nessun beta aperto può essere rilevato.**

Verificato su pagine reali scaricate il 2026-08-17:

| ID | Testo in `<div class="beta-status">` | Classificazione attuale |
|---|---|---|
| `vP9R49Ro` (aperto) | "To join the app.list beta, open the link on your iPhone, iPad, or Mac after you install TestFlight." | `full` — **errata** |
| `3xxFME3Y` (aperto) | "To join the TesterBuddy… after you install TestFlight." | `full` — **errata** |
| `FULL0001` (pieno) | "This beta is full." | `full` — corretta |
| `BjPkQhIY` (chiuso) | "This beta isn't accepting any new testers right now." | `full` — corretta |
| `2vcAjCNM` (rimosso) | HTTP 404 | `invalid` — corretta |

## 2. Vincoli accertati

Verificati empiricamente, non assunti.

**Non esiste alcun meccanismo push.** Apple non offre webhook, feed o canali
di notifica per le pagine di join TestFlight. Il polling è l'unica strada.

**Non esiste alcun segnale non testuale nei metadati.** Il `<div>` ha la stessa
classe (`beta-status`) in entrambi gli stati; i `<meta>`, i `<script>` e le
classi CSS sono identici. Nessun JSON incorporato.

**Le richieste condizionali non sono supportate.** La risposta di
`testflight.apple.com` non contiene né `ETag` né `Last-Modified`, quindi non è
possibile ottenere un `304 Not Modified`. Ogni controllo scarica ~40 KB.

**La cache di Apple è reale ma aggirabile.** Richieste ripetute allo stesso URL
restituiscono la stessa `X-Apple-Jingle-Correlation-Key` con `max-age`
decrescente (600 → 478 → …): è una copia che invecchia. Gli header
`Cache-Control: no-cache` e `Pragma: no-cache` vengono ignorati.

Un parametro variabile in query string (`?_=<nanosecondi>`) ottiene invece una
risposta generata al momento: correlation-key diversa a ogni richiesta e
`max-age` pieno a 600. Verificato il 2026-08-17, e verificato che non alteri la
pagina servita (un beta aperto resta aperto, uno pieno resta pieno).

Senza cache-buster `CHECK_INTERVAL` non significherebbe nulla: la freschezza
reale oscillerebbe fra l'intervallo e l'intervallo più 600s a seconda di dove
si cade nella finestra di cache. Con il cache-buster l'intervallo è esatto.

## 3. Il motore di rilevamento

### 3.1 Due segnali indipendenti

Su un beta chiuso Apple genera l'impalcatura dei pulsanti ma ne **svuota le
etichette**. Questo dà un secondo segnale, indipendente dal testo e dalla lingua.

| Campione | Pulsanti `<a class="button">` | Testo `beta-status` |
|---|---|---|
| `vP9R49Ro` aperto | `['View in App Store', 'View in TestFlight']` | invito a installare |
| `3xxFME3Y` aperto | non vuoti | invito a installare |
| `FULL0001` pieno | `['', '']` | "This beta is full." |
| `BjPkQhIY` chiuso | `['', '']` | "isn't accepting any new testers" |

I due segnali concordano su 6 campioni su 6 (2 aperti, 3 chiusi, 1 rimosso).

- **Segnale strutturale**: esiste almeno un `<a class="button">` con testo?
  Sì → aperto. È il giudice primario: non dipende dalle parole scelte da Apple.
- **Segnale testuale**: il `beta-status` contiene l'invito a installare
  (→ aperto) o una frase di chiusura nota (→ chiuso)?

### 3.2 Stati

| Stato | Significato | Notifica |
|---|---|---|
| `open` | Il beta accetta iscrizioni | sì, al passaggio a `open` |
| `full` | Pieno, non accetta, scaduto o non disponibile | no |
| `invalid` | HTTP 404: link rimosso da Apple | sì, una volta |
| `unknown` | I segnali non permettono di decidere | mai (solo avviso anomalia) |
| `error` | Errore di rete o HTTP inatteso | mai |

`full` accorpa deliberatamente "pieno" e "non accetta": ai fini del bot la
distinzione non cambia nulla. Il testo grezzo resta salvato in `detail`.

`error` **non sovrascrive** lo stato memorizzato: un guasto di rete non deve
cancellare l'ultimo stato noto né generare una notifica al ripristino.

### 3.3 Tabella di decisione

Il codice HTTP viene valutato per primo e decide da solo: `404` → `invalid`,
qualsiasi codice diverso da `200` → `error`. La tabella che segue si applica
**solo al caso `200`**, l'unico in cui c'è un corpo HTML da interpretare.

Sia `strutturale ∈ {aperto, chiuso, None}` (None = nessun `<a class="button">`
trovato: la pagina ha cambiato forma) e `testuale ∈ {aperto, chiuso, None}`
(None = nessun `beta-status`, o testo che non corrisponde a nessun marker noto).

| strutturale | testuale | risultato | avviso anomalia |
|---|---|---|---|
| aperto | aperto | `open` | no |
| chiuso | chiuso | `full` | no |
| aperto | chiuso | `unknown` | sì — **discordanza** |
| chiuso | aperto | `unknown` | sì — **discordanza** |
| aperto | None | `open` | sì — segnale degradato |
| chiuso | None | `full` | sì — segnale degradato |
| None | aperto | `open` | sì — segnale degradato |
| None | chiuso | `full` | sì — segnale degradato |
| None | None | `unknown` | sì — pagina irriconoscibile |

La riga che conta è la discordanza: perché il bot torni a mentire in silenzio,
Apple deve rompere **entrambi** i segnali contemporaneamente e in modo coerente.
Se ne rompe uno solo, il bot continua a funzionare in modalità degradata e
l'utente ne viene informato entro un ciclo di controllo.

### 3.4 Avvisi di anomalia, senza spam

Ogni anomalia produce un'impronta (`sha256` del testo grezzo di `beta-status`
più la combinazione di segnali). L'impronta viene salvata nel record dell'app;
un avviso Telegram parte **solo** se l'impronta è diversa da quella già
notificata. La stessa anomalia ripetuta a ogni ciclo non genera altri messaggi.

L'avviso contiene il testo grezzo letto, così i marker si possono aggiornare
senza aprire i log del container.

Quando un controllo torna senza anomalia, `anomaly_fingerprint` viene riportato
a `null`. Così, se la stessa anomalia si ripresenta dopo un periodo di
normalità, viene notificata di nuovo: è un evento nuovo, non la coda del
precedente.

## 4. Struttura a moduli

```
testflight_watcher/
  __init__.py
  __main__.py    avvio: costruisce e collega i pezzi
  config.py      env → Config, validato all'avvio
  detector.py    (status_code, html) → Detection.  PURO
  client.py      curl_cffi → HTML grezzo. L'unico che parla con Apple
  store.py       JSON atomico + RLock + migrazione di schema
  notifier.py    invio Telegram sincrono, usato dal monitor
  monitor.py     il loop: client → detector → store → notifier
  bot.py         handler Telegram: comandi, bottoni, rinomina
tests/
  fixtures/      i campioni HTML reali (già acquisiti nel repo)
  test_detector.py
  test_store.py
  test_monitor.py
```

**La decisione portante: `detector.py` è puro.** Riceve un codice HTTP e una
stringa HTML, restituisce uno stato. Non apre socket, non tocca il disco.

È il motivo per cui il bug è sfuggito: oggi la classificazione è appiccicata
alla chiamata di rete dentro `check_status()`, quindi per provarla servono
internet e un beta aperto sotto mano — e infatti non è mai stata provata su un
beta aperto. Separandola, le fixture diventano test istantanei e deterministici.

**Direzione delle dipendenze:** `bot.py` e `monitor.py` dipendono entrambi da
`store.py`, ma non si conoscono tra loro. `detector.py` non dipende da nulla del
progetto. `config.py` non importa nessun altro modulo del package.

### 4.1 Interfacce

```python
# detector.py
@dataclass(frozen=True)
class Detection:
    state: str            # open | full | invalid | unknown | error
    detail: str           # testo grezzo di beta-status, per diagnosi e /list
    structural: str|None  # open | full | None
    textual: str|None     # open | full | None
    anomaly: str|None     # descrizione, se i segnali non concordano

def classify(status_code: int, body: str) -> Detection: ...

# client.py
@dataclass(frozen=True)
class Fetched:
    status_code: int
    body: str

def fetch(tf_id: str, cfg: Config) -> Fetched: ...   # solleva su errore di rete

# store.py
def load() -> dict
def add(tf_id, name) -> bool
def remove(tf_id) -> dict|None
def rename(tf_id, name) -> str|None
def record_check(tf_id, *, state, detail, anomaly_fingerprint) -> None
```

`record_check` rilegge il DB sotto lock prima di scrivere, così il monitor non
sovrascrive le aggiunte o rimozioni fatte dall'utente durante un giro di
controlli. Comportamento già presente, da preservare.

### 4.2 Schema dei dati

```json
{
  "version": 2,
  "apps": {
    "aBcD1234": {
      "name": "Nome App",
      "state": "full",
      "detail": "This beta is full.",
      "last_checked": "2026-08-17T14:03:11Z",
      "anomaly_fingerprint": null
    }
  }
}
```

**Migrazione**, da eseguire all'avvio, idempotente:

- schema v0 (`{"available": bool}`, il formato oggi presente in `data/data.json`)
  → `state: "open"|"full"`, campo `available` rimosso
- schema v1 (`{"state": str}`, nessun `version`) → aggiunta dei campi nuovi
- il file viene riscritto solo se qualcosa è effettivamente cambiato

La scrittura atomica esistente (`tempfile.mkstemp` + `fsync` + `os.replace`) e
lo spostamento in `.corrupt` invece della cancellazione vanno preservati.

## 5. Polling

- `CHECK_INTERVAL` predefinito **300s** invece di 60s, coerente con il
  `max-age=600` dichiarato da Apple. Resta configurabile.
- **Jitter** ±10% sull'intervallo, per non bussare a cadenza perfettamente
  regolare.
- **Backoff esponenziale per singola app.** Il loop continua a girare al suo
  intervallo normale; è la singola app in errore a essere *saltata* per un
  numero crescente di giri (1, 2, 4, 8… fino a un massimo equivalente a 30
  minuti). Il contatore si azzera al primo controllo riuscito. Un'app
  irraggiungibile non rallenta quindi le altre. Lo stato di backoff vive in
  memoria, non nel file JSON: al riavvio si riparte puliti.
- All'avvio, se `CHECK_INTERVAL < 600`, un log di livello INFO ricorda che Apple
  dichiara una validità di 600s e che intervalli più brevi probabilmente
  leggono una copia in cache.

## 6. Comandi del bot

Invariati: `/start`, `/add`, `/remove`, `/list`, `/rename`, più i pulsanti
inline equivalenti.

**Nuovo: `/check <ID>`** — esegue un controllo immediato e restituisce lo stato,
il testo grezzo letto da Apple e quale dei due segnali ha risposto cosa. È lo
strumento di diagnosi che oggi manca: il rilevamento si è rotto due volte e in
nessuna delle due l'utente se n'è accorto senza aprire i log.

`/list` mostra anche `unknown` con la sua icona, così un'anomalia in corso è
visibile senza chiedere.

L'autorizzazione alla sola `TELEGRAM_CHAT_ID` resta su tutti gli handler,
`/check` incluso.

## 7. Test

Il detector si prova sulle fixture reali già nel repo, senza rete:

| Fixture | Atteso |
|---|---|
| `open-applist.html` | `open`, nessuna anomalia |
| `open-testerbuddy.html` | `open`, nessuna anomalia |
| `closed-full.html` | `full`, nessuna anomalia |
| `closed-not-accepting.html` | `full`, nessuna anomalia |
| `notfound-404.html` (404) | `invalid` |

Più i casi costruiti: HTML mutilato → `unknown`; segnali in discordanza →
`unknown` con anomalia; HTTP 500 → `error`.

Per `store.py`: migrazione v0→v2 e v1→v2, scrittura atomica, file corrotto
spostato in `.corrupt`, nessuna perdita di scritture concorrenti.

Per `monitor.py`, con client e notifier finti: `full`→`open` notifica una volta;
`open`→`open` non rinotifica; `open`→`full`→`open` rinotifica; `error` preserva
lo stato senza notificare; anomalia identica ripetuta avvisa una sola volta.

Nessuna fixture deve contenere gli ID TestFlight monitorati dall'utente: il
repo è pubblico. Le fixture attuali usano solo beta pubblici, con l'ID del
campione "pieno" sostituito da `FULL0001`.

## 8. Impatto fuori dal package

- **Dockerfile**: copia il package invece del file singolo;
  `CMD ["python", "-u", "-m", "testflight_watcher"]`. Utente non-root invariato.
- **requirements-dev.txt**: nuovo, con `pytest`.
- **CI**: nuovo job che esegue i test **prima** del build dell'immagine, così un
  detector rotto non arriva su GHCR. Il workflow di release resta per il resto
  invariato.
- **docker-compose.yml**: `user: "0:0"` resta — serve con podman rootless, dove
  l'UID 0 del container è mappato sull'utente dell'host. Verificato in
  precedenza: rimuoverlo causa `PermissionError` sul volume.
- **README**: tabella degli stati aggiornata con `unknown`, nuovo comando
  `/check`, nota sull'intervallo predefinito e sulla cache di Apple.

## 9. Fuori ambito

- Interfaccia web o dashboard.
- Supporto a più chat Telegram o a più utenti.
- Iscrizione automatica al beta: richiede un dispositivo Apple autenticato.
- Database diverso dal file JSON: la scala non lo giustifica.
