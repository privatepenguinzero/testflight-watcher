# TestFlight Watcher

Bot Telegram che controlla a intervalli regolari una lista di beta TestFlight e
ti avvisa appena si libera un posto.

## Come funziona

La pagina `testflight.apple.com/join/<ID>` risponde sempre `200`: lo stato del
beta non sta nel codice HTTP. Il monitor incrocia **due segnali indipendenti**:

- **strutturale** — su un beta chiuso Apple genera i pulsanti ma ne svuota le
  etichette, quindi "esiste un `<a class="button">` con testo" equivale a
  "beta aperto". Non dipende dalla lingua né dalle parole scelte da Apple.
- **testuale** — il contenuto di `<div class="beta-status">`.

| Stato | Significato |
|---|---|
| `open` | Il beta accetta iscrizioni → parte la notifica |
| `full` | Pieno, non accetta, scaduto o non disponibile |
| `invalid` | HTTP 404: link rimosso da Apple |
| `unknown` | I segnali non concordano: nessuna notifica, ma arriva un avviso |
| `error` | Errore di rete o HTTP inatteso: lo stato precedente resta invariato |

Se i due segnali concordano, quello è lo stato. Se **discordano**, lo stato è
`unknown` e ricevi un messaggio col testo grezzo letto da Apple. È la rete di
sicurezza del sistema: perché il bot torni a sbagliare in silenzio, Apple deve
rompere entrambi i segnali insieme e in modo coerente.

La notifica scatta **solo sul cambio di stato**, quindi niente spam finché il
beta resta aperto. Le richieste usano `curl_cffi` con fingerprint TLS di Safari
per non farsi bloccare da Apple.

## Configurazione

Crea un file `.env` nella cartella del progetto:

```env
TELEGRAM_TOKEN=123456789:AAAA-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
TELEGRAM_CHAT_ID=987654321
CHECK_INTERVAL=60
IMPERSONATE=safari17_0
```

| Variabile | Default | Descrizione |
|---|---|---|
| `TELEGRAM_TOKEN` | — | Token del bot, da [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | — | Chat delle notifiche, da [@userinfobot](https://t.me/userinfobot). È anche l'unica chat autorizzata a comandare il bot |
| `CHECK_INTERVAL` | `300` | Secondi tra un controllo e l'altro (minimo 30) |
| `IMPERSONATE` | `safari17_0` | Fingerprint TLS. Anche `safari17_2_ios`, `safari18_0`, `chrome124` |
| `DB_FILE` | `/app/data/data.json` | Percorso dello stato |

> `.env` è escluso dal repo tramite `.gitignore`: contiene il token del bot.

## Avvio

```bash
docker compose up -d --build
docker compose logs -f
```

Oppure con l'immagine già pubblicata su GHCR, sostituendo `build: .` con:

```yaml
image: ghcr.io/privatepenguinzero/testflight-watcher:latest
```

## Comandi del bot

| Comando | Descrizione |
|---|---|
| `/start` | Menu principale con i pulsanti |
| `/add <ID> [Nome]` | Aggiunge un beta da monitorare |
| `/remove <ID>` | Rimuove un beta |
| `/list` | Elenca i beta e il loro stato |
| `/rename <ID> <Nome>` | Rinomina un beta |
| `/check <ID>` | Controlla subito e mostra cosa legge da Apple (diagnostica) |

L'`ID` è la parte finale del link di invito:
`https://testflight.apple.com/join/`**`aBcD1234`**

Le stesse operazioni si fanno dai pulsanti inline, rinomina inclusa.

## Stato salvato

`data/data.json`, montato come volume:

```json
{
  "version": 2,
  "apps": {
    "aBcD1234": {
      "name": "WhatsApp Beta",
      "state": "full",
      "detail": "This beta is full.",
      "last_checked": "2026-08-17T14:03:11Z",
      "anomaly_fingerprint": null
    }
  }
}
```

Scrittura atomica (file temporaneo + `os.replace`): un'interruzione a metà non
lascia un JSON troncato. Se il file risulta illeggibile viene spostato in
`data.json.corrupt` invece di essere cancellato.

**Aggiornare il container non perde i tuoi ID.** Il file sta sull'host, montato
come volume, e l'immagine non lo contiene. I formati più vecchi vengono
convertiti al primo avvio, conservando nomi e stati; prima della conversione
viene salvata una copia in `data.json.bak-v<versione>`.

## Sviluppo

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

I test del rilevamento girano su pagine TestFlight reali salvate in
`tests/fixtures/` — beta aperti, pieni e rimossi — quindi non serve rete e non
serve trovare un beta aperto al momento giusto. La CI li esegue prima di
costruire l'immagine: un rilevamento rotto non arriva su GHCR.

## Note sui permessi

`docker-compose.yml` usa `user: "0:0"` perché serve a scrivere sul volume
`./data`. Con **podman rootless** non è root vero: l'UID 0 del container viene
mappato sull'utente dell'host, e i file restano di proprietà del tuo utente.
Con **Docker rootful** invece è root effettivo: in quel caso togli quella riga
e usa l'utente non-root (UID 1000) già definito nel `Dockerfile`.

## Release

Ogni release pubblicata su GitHub fa partire la action `release.yml`, che
builda l'immagine per `linux/amd64` e `linux/arm64` e la pubblica su GHCR con
il tag della versione più `latest`.
