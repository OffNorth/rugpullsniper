"""
telegram_bot.py
================
Point d'entrée principal du bot sniper. Orchestre :
  - la détection de nouveaux pools (via sniper_engine.py)
  - le scoring anti-rug (via rug_checker.py)
  - l'envoi d'alertes Telegram et la gestion des commandes

Lancement :
    python3 telegram_bot.py

Dépendances :
    pip install python-telegram-bot python-dotenv
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from rug_checker import RugChecker, MIN_SCORE_TO_BUY
from sniper_engine import listen_new_pools, execute_buy, execute_sell, get_jupiter_honeypot_check

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AUTO_BUY = os.getenv("AUTO_BUY", "false").lower() == "true"  # sécurité : off par défaut

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sniper_bot")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être définis dans .env "
        "(voir .env.example et le README pour la procédure via @BotFather)."
    )

# État partagé en mémoire (à remplacer par une vraie DB si le bot grossit)
WATCHLIST: dict[str, dict] = {}   # mint -> {"score": int, "details": dict}
BOT_STATE = {"running": False, "checked_count": 0, "alerts_sent": 0}


# ---------------------------------------------------------------------------
# Commandes Telegram
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = "AUTO-BUY activé" if AUTO_BUY else "Alertes seules (achat manuel)"
    text = (
        f"*Statut du bot*\n"
        f"En ligne : {'oui' if BOT_STATE['running'] else 'non'}\n"
        f"Mode : {mode}\n"
        f"Tokens vérifiés : {BOT_STATE['checked_count']}\n"
        f"Alertes envoyées : {BOT_STATE['alerts_sent']}\n"
        f"Seuil anti-rug : {MIN_SCORE_TO_BUY}/100"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not WATCHLIST:
        await update.message.reply_text("Watchlist vide pour le moment.")
        return
    lines = ["*Watchlist actuelle*"]
    for mint, info in list(WATCHLIST.items())[-10:]:  # les 10 derniers
        lines.append(f"`{mint[:8]}...` — score {info['score']}/100")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        f"*Configuration actuelle*\n"
        f"MIN_SCORE_TO_BUY : {MIN_SCORE_TO_BUY}\n"
        f"AUTO_BUY : {AUTO_BUY}\n"
        f"BUY_AMOUNT_SOL : {os.getenv('BUY_AMOUNT_SOL', 'non défini')}\n"
        f"SLIPPAGE_BPS : {os.getenv('SLIPPAGE_BPS', 'non défini')}\n\n"
        f"_Pour modifier ces valeurs, édite le fichier .env et redémarre le bot._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage : /buy <adresse_du_mint>")
        return
    mint = context.args[0]
    if mint not in WATCHLIST:
        await update.message.reply_text(
            "Ce token n'a pas encore été vérifié par le rug checker. "
            "Achat manuel non recommandé sans scoring — abandon par sécurité."
        )
        return
    # Exécution réelle de l'achat
    await update.message.reply_text(f"Achat en cours pour `{mint[:8]}...`...", parse_mode=ParseMode.MARKDOWN)
    signature = await execute_buy(mint)
    if signature:
        await update.message.reply_text(
            f"✅ Achat exécuté : `{signature[:16]}...`\n"
            f"https://solscan.io/tx/{signature}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("❌ Echec de l'achat — voir les logs du bot.")


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : /sell <adresse_du_mint> <montant_en_unites_token>\n"
            "(vérifie le solde exact via un explorer si besoin — la vente totale "
            "automatique du solde n'est pas encore implémentée)"
        )
        return
    mint = context.args[0]
    try:
        amount_tokens = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Le montant doit être un nombre entier (unités brutes du token).")
        return

    await update.message.reply_text(f"Vente en cours pour `{mint[:8]}...`...", parse_mode=ParseMode.MARKDOWN)
    signature = await execute_sell(mint, amount_tokens)
    if signature:
        await update.message.reply_text(
            f"✅ Vente exécutée : `{signature[:16]}...`\n"
            f"https://solscan.io/tx/{signature}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("❌ Echec de la vente — voir les logs du bot.")


# ---------------------------------------------------------------------------
# Boucle de détection + scoring (tâche de fond)
# ---------------------------------------------------------------------------

async def send_alert(app: Application, mint: str, result) -> None:
    emoji = "✅" if result.passed else "⚠️"
    text = (
        f"{emoji} *Nouveau token détecté*\n"
        f"Mint : `{mint}`\n"
        f"Score anti-rug : *{result.score}/100*\n"
    )
    if result.reasons_failed:
        text += "\nPoints d'attention :\n" + "\n".join(f"• {r}" for r in result.reasons_failed)
    if result.passed:
        text += f"\n\nUtilise `/buy {mint}` pour acheter manuellement."

    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
    BOT_STATE["alerts_sent"] += 1


async def on_new_pool_detected(app: Application, mint: str) -> None:
    """
    À appeler depuis sniper_engine.py à chaque nouveau pool détecté sur
    Pump.fun/Raydium. Fait le scoring puis décide alerte/achat.
    """
    async with RugChecker(min_score=MIN_SCORE_TO_BUY) as checker:
        result = await checker.evaluate(mint, jupiter_quote_fn=get_jupiter_honeypot_check)

    WATCHLIST[mint] = {"score": result.score, "details": result.details}
    BOT_STATE["checked_count"] += 1

    logger.info(result.summary())

    # On alerte systématiquement (même les rejets, pour transparence/debug),
    # mais on ne déclenche l'achat auto que si le score passe ET que AUTO_BUY=true
    await send_alert(app, mint, result)

    if result.passed and AUTO_BUY:
        logger.info(f"AUTO_BUY activé — achat automatique déclenché pour {mint}")
        signature = await execute_buy(mint)
        if signature:
            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"🤖 Achat automatique exécuté pour `{mint[:8]}...`\nhttps://solscan.io/tx/{signature}",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"🤖❌ Achat automatique échoué pour `{mint[:8]}...`",
                parse_mode=ParseMode.MARKDOWN,
            )


async def pool_detection_loop(app: Application) -> None:
    """
    Boucle de détection réelle, branchée sur sniper_engine.listen_new_pools().

    NOTE : listen_new_pools() ne yield actuellement un mint que si
    _extract_mint_from_logs() parvient à l'extraire (voir sniper_engine.py) —
    cette extraction nécessite encore un raffinement (parsing getTransaction)
    avant de fonctionner pleinement en conditions réelles.
    """
    BOT_STATE["running"] = True
    logger.info("Boucle de détection démarrée — écoute des nouveaux pools Pump.fun.")
    try:
        async for mint in listen_new_pools():
            try:
                await on_new_pool_detected(app, mint)
            except Exception as e:
                logger.error(f"Erreur lors du traitement du mint {mint}: {e}")
    except Exception as e:
        logger.error(f"Boucle de détection interrompue : {e}")
        BOT_STATE["running"] = False


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    asyncio.create_task(pool_detection_loop(app))
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="🟢 Bot démarré. Utilise /status pour vérifier l'état.",
    )


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))

    logger.info("Démarrage du bot Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
