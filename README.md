# TestFlight Watcher

Bot Telegram che controlla a intervalli regolari una lista di beta TestFlight e
ti avvisa appena si libera un posto.

## Come funziona

La pagina `testflight.apple.com/join/<ID>` risponde sempre `200`: lo stato del
beta non sta nel codice HTTP ma nel blocco `<div class="beta-status">`. Il
monitor legge quel testo e lo classifica:

| Stato | Significato |
|---|---|
| `open` | Il beta accetta iscrizioni → parte la notifica |
| `full` | "This beta is full." / "isn't accepting any new testers right now." |
| `invalid` | HTTP 404: link rimosso da Apple |
| `error` | Errore di rete o HTTP inatteso: lo stato precedente resta invariato |

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
| `CHECK_INTERVAL` | `60` | Secondi tra un controllo e l'altro |
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

L'`ID` è la parte finale del link di invito:
`https://testflight.apple.com/join/`**`aBcD1234`**

Le stesse operazioni si fanno dai pulsanti inline, rinomina inclusa.

## Stato salvato

`data/data.json`, montato come volume:

```json
{
  "apps": {
    "aBcD1234": { "name": "WhatsApp Beta", "state": "full" }
  }
}
```

Scrittura atomica (file temporaneo + `os.replace`): un'interruzione a metà non
lascia un JSON troncato. Se il file risulta illeggibile viene spostato in
`data.json.corrupt` invece di essere cancellato.

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
