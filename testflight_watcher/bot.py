"""Interfaccia Telegram: comandi, pulsanti, rinomina.

Non conosce il monitor: entrambi passano dallo Store. L'unica cosa che
condividono è il file JSON, protetto dal lock dello Store.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .client import join_url
from .detector import ST_FULL, ST_INVALID, ST_OPEN, ST_UNKNOWN, classify
from .notifier import esc

log = logging.getLogger(__name__)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Lista App", callback_data="list")],
        [
            InlineKeyboardButton("➕ Aggiungi", callback_data="prompt_add"),
            InlineKeyboardButton("➖ Rimuovi", callback_data="prompt_remove"),
        ],
        [InlineKeyboardButton("✏️ Rinomina", callback_data="prompt_rename")],
    ])


def status_label(app: dict) -> str:
    return {
        ST_OPEN: "🟢 Disponibile",
        ST_FULL: "🔴 Piena",
        ST_INVALID: "⚠️ Link non valido",
        ST_UNKNOWN: "❓ Rilevamento incerto",
    }.get(app.get("state"), "⏳ Mai controllata")


def render_list(apps: dict) -> str:
    righe = ["📋 <b>App Monitorate:</b>", ""]
    for tf_id, app in apps.items():
        righe.append(
            f"• <b>{esc(app['name'])}</b> (<code>{esc(tf_id)}</code>) — {status_label(app)}"
        )
    return "\n".join(righe)


def build_application(cfg, store, client) -> Application:
    """Costruisce il bot con le dipendenze già collegate."""

    def autorizzato(update: Update) -> bool:
        """Il bot è personale: solo la chat configurata può usarlo."""
        chat = update.effective_chat
        return chat is not None and str(chat.id) == str(cfg.telegram_chat_id)

    # ── Comandi ────────────────────────────────────────────────────────────
    async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not autorizzato(update):
            return
        await update.message.reply_text(
            "🤖 <b>TestFlight Watcher</b>\n\n"
            "Usa i pulsanti o i comandi diretti:\n"
            "/add <code>ID</code> [Nome] — Aggiungi un'app\n"
            "/remove <code>ID</code> — Rimuovi un'app\n"
            "/list — Elenca le app monitorate\n"
            "/rename <code>ID</code> <code>Nuovo Nome</code> — Rinomina\n"
            "/check <code>ID</code> — Controlla subito e mostra cosa legge da Apple",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not autorizzato(update):
            return
        if not ctx.args:
            await update.message.reply_text("❌ Uso: /add <ID_TestFlight> [Nome App]")
            return

        tf_id = ctx.args[0]
        nome = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else tf_id

        if not store.add(tf_id, nome):
            await update.message.reply_text(
                f"⚠️ L'ID <code>{esc(tf_id)}</code> è già monitorato!", parse_mode="HTML"
            )
            return

        await update.message.reply_text(
            f"✅ Aggiunto: <b>{esc(nome)}</b> (<code>{esc(tf_id)}</code>)",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    async def remove_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not autorizzato(update):
            return
        if not ctx.args:
            await update.message.reply_text("❌ Uso: /remove <ID_TestFlight>")
            return

        tf_id = ctx.args[0]
        app = store.remove(tf_id)
        if app is None:
            await update.message.reply_text(
                f"⚠️ L'ID <code>{esc(tf_id)}</code> non è nella lista!",
                parse_mode="HTML",
            )
            return

        await update.message.reply_text(
            f"🗑 Rimosso: <b>{esc(app['name'])}</b> (<code>{esc(tf_id)}</code>)",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not autorizzato(update):
            return
        apps = store.apps()
        if not apps:
            await update.message.reply_text(
                "📭 Nessuna app monitorata. Usa /add per aggiungerne una!",
                reply_markup=main_keyboard(),
            )
            return
        await update.message.reply_text(
            render_list(apps), parse_mode="HTML", reply_markup=main_keyboard()
        )

    async def rename_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not autorizzato(update):
            return
        if len(ctx.args) < 2:
            await update.message.reply_text(
                "❌ Uso: /rename <ID_TestFlight> <Nuovo Nome>"
            )
            return

        tf_id, nuovo = ctx.args[0], " ".join(ctx.args[1:])
        vecchio = store.rename(tf_id, nuovo)
        if vecchio is None:
            await update.message.reply_text(
                f"⚠️ L'ID <code>{esc(tf_id)}</code> non è nella lista!", parse_mode="HTML"
            )
            return

        await update.message.reply_text(
            f"✏️ Rinominato: <b>{esc(vecchio)}</b> → <b>{esc(nuovo)}</b>\n"
            f"(<code>{esc(tf_id)}</code>)",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Controllo immediato con diagnostica.

        Esiste perché il rilevamento si è già rotto due volte senza che se ne
        accorgesse nessuno: serve poter vedere cosa legge il bot senza aprire
        i log del container.
        """
        if not autorizzato(update):
            return
        if not ctx.args:
            await update.message.reply_text("❌ Uso: /check <ID_TestFlight>")
            return

        tf_id = ctx.args[0]
        await update.message.reply_text(
            f"🔎 Controllo <code>{esc(tf_id)}</code>…", parse_mode="HTML"
        )

        try:
            # La rete è sincrona: fuori dal loop asyncio per non bloccarlo.
            fetched = await asyncio.to_thread(client.fetch, tf_id)
        except Exception as e:
            await update.message.reply_text(
                f"❌ Richiesta fallita: <code>{esc(e)}</code>", parse_mode="HTML"
            )
            return

        d = classify(fetched.status_code, fetched.body)
        await update.message.reply_text(
            f"🔎 <b>{esc(tf_id)}</b>\n"
            f"Stato: <b>{esc(status_label({'state': d.state}))}</b>\n"
            f"HTTP: <code>{fetched.status_code}</code>\n"
            f"Segnale pagina: <code>{esc(d.structural or '—')}</code>\n"
            f"Segnale testo: <code>{esc(d.textual or '—')}</code>\n"
            f"Testo letto: <code>{esc(d.detail or '(vuoto)')}</code>"
            + (f"\n⚠️ {esc(d.anomaly)}" if d.anomaly else "")
            + f"\n🔗 <a href='{join_url(tf_id)}'>Apri TestFlight</a>",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=main_keyboard(),
        )

    # ── Pulsanti ───────────────────────────────────────────────────────────
    async def button_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not autorizzato(update):
            return
        query = update.callback_query
        await query.answer()
        data = query.data
        apps = store.apps()

        if data == "list":
            if not apps:
                await query.edit_message_text(
                    "📭 Nessuna app monitorata. Clicca ➕ Aggiungi!",
                    reply_markup=main_keyboard(),
                )
                return
            await query.edit_message_text(
                render_list(apps), parse_mode="HTML", reply_markup=main_keyboard()
            )

        elif data == "prompt_add":
            await query.edit_message_text(
                "➕ <b>Aggiungi App</b>\nScrivi il comando così:\n\n"
                "<code>/add ID_TestFlight Nome App</code>\n\n"
                "L'ID è la parte finale del link di invito.",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

        elif data in ("prompt_remove", "prompt_rename"):
            rimozione = data == "prompt_remove"
            if not apps:
                await query.edit_message_text(
                    "📭 Non ci sono app in lista!", reply_markup=main_keyboard()
                )
                return
            prefisso = "remove_" if rimozione else "select_rename_"
            icona = "🗑" if rimozione else "✏️"
            tastiera = [
                [InlineKeyboardButton(f"{icona} {a['name']}", callback_data=prefisso + tf_id)]
                for tf_id, a in apps.items()
            ]
            tastiera.append([InlineKeyboardButton("⬅️ Indietro", callback_data="back")])
            await query.edit_message_text(
                f"{icona} <b>Scegli l'app:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(tastiera),
            )

        elif data.startswith("remove_"):
            tf_id = data[len("remove_"):]
            app = store.remove(tf_id)
            if app is None:
                await query.edit_message_text(
                    "⚠️ App già rimossa.", reply_markup=main_keyboard()
                )
                return
            await query.edit_message_text(
                f"🗑 Rimosso: <b>{esc(app['name'])}</b> (<code>{esc(tf_id)}</code>)",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

        elif data.startswith("select_rename_"):
            tf_id = data[len("select_rename_"):]
            ctx.user_data["renaming_id"] = tf_id
            ctx.user_data["action"] = "waiting_rename"
            await query.edit_message_text(
                f"✏️ Stai rinominando <code>{esc(tf_id)}</code>.\n"
                "Scrivi ora il nuovo nome (senza /):",
                parse_mode="HTML",
            )

        elif data == "back":
            await query.edit_message_text(
                "🤖 <b>Menu Principale</b>",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

    # ── Testo libero (rinomina da pulsante) ────────────────────────────────
    async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not autorizzato(update):
            return
        if ctx.user_data.get("action") != "waiting_rename":
            await update.message.reply_text(
                "Non ho capito. Usa i pulsanti o i comandi (/start)",
                reply_markup=main_keyboard(),
            )
            return

        tf_id = ctx.user_data.get("renaming_id")
        nuovo = update.message.text
        ctx.user_data["action"] = None
        ctx.user_data["renaming_id"] = None

        vecchio = store.rename(tf_id, nuovo)
        if vecchio is None:
            await update.message.reply_text(
                "⚠️ Quell'app non esiste più.", reply_markup=main_keyboard()
            )
            return

        await update.message.reply_text(
            f"✏️ Rinominato: <b>{esc(vecchio)}</b> → <b>{esc(nuovo)}</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    app = Application.builder().token(cfg.telegram_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("rename", rename_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app
