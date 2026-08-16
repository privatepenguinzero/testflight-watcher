import os
import re
import html
import time
import json
import logging
import tempfile
from threading import Thread, RLock

from curl_cffi import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Configurazione ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "60"))
DB_FILE          = os.getenv("DB_FILE", "/app/data/data.json")
# curl_cffi impersona il TLS fingerprint di un browser reale: senza questo
# Apple può bloccare le richieste automatiche.
IMPERSONATE      = os.getenv("IMPERSONATE", "safari17_0")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TESTFLIGHT_URL = "https://testflight.apple.com/join/{}"

# Stati possibili di un beta
ST_OPEN    = "open"
ST_FULL    = "full"
ST_INVALID = "invalid"
ST_ERROR   = "error"


# ── Database (file JSON, condiviso tra bot e thread di monitoraggio) ────────
# Il bot (thread asyncio) e il monitor (thread separato) scrivono sullo stesso
# file: senza lock il monitor sovrascriveva le modifiche fatte dall'utente
# durante il giro di controlli.
_db_lock = RLock()


def load_db() -> dict:
    with _db_lock:
        if not os.path.exists(DB_FILE):
            db = {"apps": {}}
            save_db(db)
            return db
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                db = json.load(f)
        except (OSError, ValueError) as e:
            # File corrotto: lo mettiamo da parte invece di cancellarlo.
            log.error(f"DB illeggibile ({e}); ne creo uno nuovo")
            try:
                os.replace(DB_FILE, DB_FILE + ".corrupt")
            except OSError:
                pass
            db = {"apps": {}}
            save_db(db)
            return db

        if not isinstance(db, dict) or not isinstance(db.get("apps"), dict):
            log.error("DB con struttura inattesa; ne creo uno nuovo")
            db = {"apps": {}}
            save_db(db)
        return db


def save_db(data: dict) -> None:
    # Scrittura atomica: senza questo un crash a metà dump lascia un JSON
    # troncato, che al riavvio veniva interpretato come "DB corrotto".
    with _db_lock:
        directory = os.path.dirname(DB_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".data-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, DB_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def update_app_state(tf_id: str, **fields) -> None:
    """Aggiorna un'app rileggendo il DB, così non si sovrascrivono le
    aggiunte/rimozioni fatte dall'utente mentre il check era in corso."""
    with _db_lock:
        db = load_db()
        if tf_id in db["apps"]:
            db["apps"][tf_id].update(fields)
            save_db(db)


def esc(text) -> str:
    """I nomi delle app arrivano dall'utente: senza escape un nome con < o &
    fa fallire l'invio del messaggio (parse_mode HTML)."""
    return html.escape(str(text), quote=False)


# ── Logica di Check ─────────────────────────────────────────────────────────
# La pagina TestFlight risponde SEMPRE 200 (non fa redirect), e lo stato del
# beta sta dentro <div class="beta-status">...</div>.
_BETA_STATUS_RE = re.compile(
    r'<div[^>]*\bclass\s*=\s*["\'][^"\']*\bbeta-status\b[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_DIV_TOKEN_RE = re.compile(r"<\s*(/?)div\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# Frasi che indicano un beta NON accessibile.
CLOSED_MARKERS = (
    "this beta is full",
    "isn't accepting any new testers",
    "is not accepting any new testers",
    "this beta has expired",
    "this beta isn't available",
    "this beta is not available",
    "this beta isn't accepting",
)


def _beta_status_text(body: str) -> str:
    """Testo dentro <div class="beta-status">.

    Il div può contenere altri <div> annidati (es. l'icona dell'app): serve
    contare la profondità, altrimenti ci si ferma al primo </div> e si legge
    una stringa vuota, che verrebbe scambiata per "beta aperto".
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

    text = _TAG_RE.sub(" ", body[start:end])
    return " ".join(html.unescape(text).split())


def check_status(tf_id: str) -> tuple[str, str]:
    """Ritorna (stato, dettaglio)."""
    url = TESTFLIGHT_URL.format(tf_id)
    r = requests.get(url, headers=HEADERS, timeout=20, impersonate=IMPERSONATE)

    if r.status_code == 404:
        return ST_INVALID, "link non valido o rimosso"
    if r.status_code != 200:
        return ST_ERROR, f"HTTP {r.status_code}"

    status_text = _beta_status_text(r.text)

    # Nessun messaggio di stato => il beta accetta iscrizioni.
    if not status_text:
        return ST_OPEN, ""

    low = status_text.lower()
    if any(marker in low for marker in CLOSED_MARKERS):
        return ST_FULL, status_text

    # Messaggio sconosciuto: lo trattiamo come chiuso per non generare falsi
    # allarmi, ma lo logghiamo così si può aggiungere a CLOSED_MARKERS.
    log.warning(f"[{tf_id}] stato TestFlight non riconosciuto: {status_text!r}")
    return ST_FULL, status_text


def send_notification(message: str) -> None:
    """Invio sincrono, usato dal thread di monitoraggio."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code != 200:
        log.error(f"Invio notifica fallito: HTTP {r.status_code} {r.text[:200]}")


# ── Autorizzazione ──────────────────────────────────────────────────────────
def is_authorized(update: Update) -> bool:
    """Il bot è personale: solo la chat configurata può usarlo."""
    chat = update.effective_chat
    return chat is not None and str(chat.id) == str(TELEGRAM_CHAT_ID)


# ── UI ──────────────────────────────────────────────────────────────────────
def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Lista App", callback_data="list")],
        [
            InlineKeyboardButton("➕ Aggiungi", callback_data="prompt_add"),
            InlineKeyboardButton("➖ Rimuovi", callback_data="prompt_remove"),
        ],
        [InlineKeyboardButton("✏️ Rinomina", callback_data="prompt_rename")],
    ])


def status_label(app_data: dict) -> str:
    state = app_data.get("state")
    if state == ST_OPEN:
        return "🟢 Disponibile"
    if state == ST_INVALID:
        return "⚠️ Link non valido"
    if state == ST_ERROR:
        return "❓ Errore di controllo"
    if state == ST_FULL:
        return "🔴 Piena"
    return "⏳ Mai controllata"


def render_list(db: dict) -> str:
    text = "📋 <b>App Monitorate:</b>\n\n"
    for tf_id, app_data in db["apps"].items():
        text += f"• <b>{esc(app_data['name'])}</b> (<code>{esc(tf_id)}</code>) - {status_label(app_data)}\n"
    return text


# ── Comandi Telegram ────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "🤖 <b>TestFlight Monitor Bot</b>\n\n"
        "Usa i pulsanti qui sotto o i comandi diretti:\n"
        "/add <code>ID</code> [Nome] - Aggiungi un'app\n"
        "/remove <code>ID</code> - Rimuovi un'app\n"
        "/list - Elenca le app monitorate\n"
        "/rename <code>ID</code> <code>Nuovo Nome</code> - Rinomina un'app",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Uso: /add <ID_TestFlight> [Nome App]")
        return

    tf_id = context.args[0]
    name = " ".join(context.args[1:]) if len(context.args) > 1 else tf_id

    with _db_lock:
        db = load_db()
        if tf_id in db["apps"]:
            await update.message.reply_text(
                f"⚠️ L'ID <code>{esc(tf_id)}</code> è già monitorato!", parse_mode="HTML"
            )
            return
        db["apps"][tf_id] = {"name": name, "state": None}
        save_db(db)

    await update.message.reply_text(
        f"✅ Aggiunto: <b>{esc(name)}</b> (<code>{esc(tf_id)}</code>)",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Uso: /remove <ID_TestFlight>")
        return

    tf_id = context.args[0]
    with _db_lock:
        db = load_db()
        if tf_id not in db["apps"]:
            await update.message.reply_text(
                f"⚠️ L'ID <code>{esc(tf_id)}</code> non è nella lista!", parse_mode="HTML"
            )
            return
        name = db["apps"].pop(tf_id)["name"]
        save_db(db)

    await update.message.reply_text(
        f"🗑 Rimosso: <b>{esc(name)}</b> (<code>{esc(tf_id)}</code>)",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    db = load_db()
    if not db["apps"]:
        await update.message.reply_text(
            "📭 Nessuna app monitorata. Usa /add per aggiungerne una!",
            reply_markup=get_main_keyboard(),
        )
        return
    await update.message.reply_text(
        render_list(db), parse_mode="HTML", reply_markup=get_main_keyboard()
    )


async def rename_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Uso: /rename <ID_TestFlight> <Nuovo Nome>")
        return

    tf_id = context.args[0]
    new_name = " ".join(context.args[1:])

    with _db_lock:
        db = load_db()
        if tf_id not in db["apps"]:
            await update.message.reply_text(
                f"⚠️ L'ID <code>{esc(tf_id)}</code> non è nella lista!", parse_mode="HTML"
            )
            return
        old_name = db["apps"][tf_id]["name"]
        db["apps"][tf_id]["name"] = new_name
        save_db(db)

    await update.message.reply_text(
        f"✏️ Rinominato: <b>{esc(old_name)}</b> → <b>{esc(new_name)}</b>\n"
        f"(<code>{esc(tf_id)}</code>)",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


# ── Click sui pulsanti ──────────────────────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    query = update.callback_query
    await query.answer()

    data = query.data
    db = load_db()

    if data == "list":
        if not db["apps"]:
            await query.edit_message_text(
                "📭 Nessuna app monitorata. Clicca ➕ Aggiungi!",
                reply_markup=get_main_keyboard(),
            )
            return
        await query.edit_message_text(
            render_list(db), parse_mode="HTML", reply_markup=get_main_keyboard()
        )

    elif data == "prompt_add":
        await query.edit_message_text(
            "➕ <b>Aggiungi App</b>\nScrivi il comando in questo formato:\n\n"
            "<code>/add ID_TestFlight Nome App</code>\n\n"
            "Esempio: <code>/add abc123def WhatsApp Beta</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )

    elif data == "prompt_remove":
        if not db["apps"]:
            await query.edit_message_text(
                "📭 Non ci sono app da rimuovere!", reply_markup=get_main_keyboard()
            )
            return
        keyboard = [
            [InlineKeyboardButton(f"🗑 {a['name']}", callback_data=f"remove_{tf_id}")]
            for tf_id, a in db["apps"].items()
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="back")])
        await query.edit_message_text(
            "➖ <b>Rimuovi App</b>\nScegli quale rimuovere:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "prompt_rename":
        if not db["apps"]:
            await query.edit_message_text(
                "📭 Non ci sono app da rinominare!", reply_markup=get_main_keyboard()
            )
            return
        keyboard = [
            [InlineKeyboardButton(f"✏️ {a['name']}", callback_data=f"select_rename_{tf_id}")]
            for tf_id, a in db["apps"].items()
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="back")])
        await query.edit_message_text(
            "✏️ <b>Rinomina App</b>\nScegli quale rinominare:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("remove_"):
        tf_id = data[len("remove_"):]
        with _db_lock:
            db = load_db()
            entry = db["apps"].pop(tf_id, None)
            if entry is not None:
                save_db(db)
        if entry is None:
            await query.edit_message_text(
                "⚠️ App già rimossa.", reply_markup=get_main_keyboard()
            )
            return
        await query.edit_message_text(
            f"🗑 Rimosso: <b>{esc(entry['name'])}</b> (<code>{esc(tf_id)}</code>)",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )

    elif data.startswith("select_rename_"):
        tf_id = data[len("select_rename_"):]
        context.user_data["renaming_id"] = tf_id
        context.user_data["action"] = "waiting_rename"
        await query.edit_message_text(
            f"✏️ <b>Rinomina App</b>\nStai rinominando <code>{esc(tf_id)}</code>.\n"
            "Scrivi ora il nuovo nome (senza comandi /):",
            parse_mode="HTML",
        )

    elif data == "back":
        await query.edit_message_text(
            "🤖 <b>Menu Principale</b>", parse_mode="HTML", reply_markup=get_main_keyboard()
        )


# ── Testo libero (rename da pulsante) ───────────────────────────────────────
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if context.user_data.get("action") != "waiting_rename":
        await update.message.reply_text(
            "Non ho capito. Usa i pulsanti o i comandi (/start)",
            reply_markup=get_main_keyboard(),
        )
        return

    new_name = update.message.text
    tf_id = context.user_data.get("renaming_id")
    context.user_data["action"] = None
    context.user_data["renaming_id"] = None

    with _db_lock:
        db = load_db()
        if tf_id not in db["apps"]:
            await update.message.reply_text(
                "⚠️ Quell'app non esiste più.", reply_markup=get_main_keyboard()
            )
            return
        old_name = db["apps"][tf_id]["name"]
        db["apps"][tf_id]["name"] = new_name
        save_db(db)

    await update.message.reply_text(
        f"✏️ Rinominato: <b>{esc(old_name)}</b> → <b>{esc(new_name)}</b>\n"
        f"(<code>{esc(tf_id)}</code>)",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


# ── Thread di monitoraggio ──────────────────────────────────────────────────
def check_once() -> None:
    for tf_id, data in list(load_db()["apps"].items()):
        name = data.get("name", tf_id)
        prev = data.get("state")
        url = TESTFLIGHT_URL.format(tf_id)
        try:
            state, detail = check_status(tf_id)
        except Exception as e:
            log.error(f"Errore check {tf_id}: {e}")
            # Un errore di rete non deve cancellare l'ultimo stato noto né
            # far scattare una notifica al ripristino.
            continue

        if state != prev:
            if state == ST_OPEN:
                log.info(f"✅ Disponibile: {name} ({tf_id})")
                send_notification(
                    "✅ <b>Posto disponibile!</b>\n"
                    f"📱 App: <b>{esc(name)}</b>\n"
                    f"🔗 <a href='{url}'>Apri TestFlight</a>"
                )
            elif state == ST_FULL and prev == ST_OPEN:
                log.info(f"🔴 Tornato pieno: {name} ({tf_id})")
            elif state == ST_INVALID:
                log.warning(f"⚠️ Link non valido: {name} ({tf_id})")
                send_notification(
                    f"⚠️ <b>Link non valido</b>\n📱 App: <b>{esc(name)}</b> "
                    f"(<code>{esc(tf_id)}</code>)\nIl beta è stato rimosso da Apple."
                )
            else:
                log.info(f"[{tf_id}] {name}: {prev} → {state} ({detail})")

        update_app_state(tf_id, state=state)


def monitor_loop() -> None:
    log.info(f"▶ Avvio thread di monitoraggio (ogni {CHECK_INTERVAL}s)")
    while True:
        try:
            check_once()
        except Exception as e:
            # Il thread non deve mai morire: senza questo un errore
            # imprevisto spegneva il monitoraggio lasciando vivo solo il bot.
            log.exception(f"Errore nel ciclo di monitoraggio: {e}")
        time.sleep(CHECK_INTERVAL)


# ── Avvio ───────────────────────────────────────────────────────────────────
def migrate_db() -> None:
    """Converte il vecchio campo booleano `available` nel nuovo `state`."""
    with _db_lock:
        db = load_db()
        changed = False
        for app_data in db["apps"].values():
            if "available" in app_data:
                if "state" not in app_data:
                    app_data["state"] = ST_OPEN if app_data["available"] else ST_FULL
                del app_data["available"]
                changed = True
            app_data.setdefault("state", None)
        if changed:
            save_db(db)
            log.info("DB migrato al nuovo formato (available → state)")


def main() -> None:
    migrate_db()

    Thread(target=monitor_loop, daemon=True).start()

    log.info("▶ Avvio Bot Telegram...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("rename", rename_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
