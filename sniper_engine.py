"""
sniper_engine.py
=================
Détection en temps réel des nouveaux pools sur Pump.fun/Raydium via WebSocket,
et exécution des achats/ventes via l'API Jupiter.

Ce module s'appuie sur rug_checker.py pour le scoring et est pensé pour être
branché dans telegram_bot.py (voir pool_detection_loop() dans ce dernier).

Dépendances :
    pip install -r requirements.txt
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import AsyncGenerator, Optional

import httpx
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
import websockets

load_dotenv()

logger = logging.getLogger("sniper_engine")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
WSS_URL = os.getenv("WSS_URL", "wss://api.mainnet-beta.solana.com")
JUPITER_API_URL = os.getenv("JUPITER_API_URL", "https://quote-api.jup.ag/v6")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")

BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.05))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", 2000))
COMPUTE_UNIT_PRICE = int(os.getenv("COMPUTE_UNIT_PRICE", 100000))

SOL_MINT = "So11111111111111111111111111111111111111112"

# Program IDs publics (constants connues, pas des secrets)
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_AMM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


def get_keypair() -> Optional[Keypair]:
    if not WALLET_PRIVATE_KEY:
        logger.warning("WALLET_PRIVATE_KEY absent du .env — exécution réelle impossible.")
        return None
    try:
        return Keypair.from_base58_string(WALLET_PRIVATE_KEY)
    except Exception as e:
        logger.error(f"Impossible de charger le wallet depuis WALLET_PRIVATE_KEY : {e}")
        return None


# ---------------------------------------------------------------------------
# Détection de nouveaux pools (WebSocket logsSubscribe)
# ---------------------------------------------------------------------------

async def listen_new_pools(program_id: str = PUMP_FUN_PROGRAM_ID) -> AsyncGenerator[str, None]:
    """
    Écoute les logs du programme Pump.fun (ou Raydium) via WebSocket et yield
    l'adresse mint de chaque nouveau token détecté.

    NOTE IMPORTANTE : l'extraction du mint depuis les logs dépend du format
    exact des instructions du programme (discriminators, structure des
    comptes). Le parsing ci-dessous est un point de départ générique ;
    tu devras l'affiner en inspectant des transactions de création réelles
    sur Solscan/Solana Explorer pour repérer l'index exact du compte mint
    dans l'instruction de création de pool.
    """
    subscribe_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [program_id]},
            {"commitment": "confirmed"},
        ],
    }

    while True:  # boucle de reconnexion automatique
        try:
            async with websockets.connect(WSS_URL) as ws:
                await ws.send(json.dumps(subscribe_msg))
                await ws.recv()  # confirmation d'abonnement
                logger.info(f"Abonné aux logs du programme {program_id}")

                async for message in ws:
                    data = json.loads(message)
                    try:
                        logs = data["params"]["result"]["value"]["logs"]
                    except (KeyError, TypeError):
                        continue

                    mint = _extract_mint_from_logs(logs)
                    if mint:
                        yield mint

        except (websockets.ConnectionClosed, OSError) as e:
            logger.warning(f"Connexion WebSocket perdue ({e}), reconnexion dans 5s...")
            await asyncio.sleep(5)


def _extract_mint_from_logs(logs: list[str]) -> Optional[str]:
    """
    Cherche un marqueur de création de token/pool dans les logs de programme.

    Placeholder volontairement simple : les logs Pump.fun contiennent
    généralement une ligne du type "Program log: Instruction: Create" suivie
    des comptes impliqués. Pour une extraction fiable du mint, il est
    recommandé de :
      1. Repérer la signature de transaction dans `data["params"]["result"]["value"]["signature"]`
      2. Faire un getTransaction complet dessus
      3. Parser les comptes de l'instruction "create" via l'IDL du programme
    Ce qui suit n'est qu'un filtre grossier sur les logs pour donner le squelette.
    """
    for line in logs:
        if "Instruction: Create" in line or "initialize" in line.lower():
            # Le mint réel doit être récupéré via getTransaction (voir docstring).
            # On retourne None ici tant que cette étape n'est pas branchée,
            # pour éviter de yield un faux positif.
            logger.debug("Création détectée dans les logs — récupération du mint via getTransaction requise.")
            return None
    return None


async def get_mint_from_signature(signature: str) -> Optional[str]:
    """
    Récupère l'adresse mint créée dans une transaction donnée, en inspectant
    les comptes de l'instruction de création. À adapter selon l'IDL exact
    du programme Pump.fun (les index de comptes peuvent changer selon les
    versions du programme).
    """
    async with AsyncClient(RPC_URL) as client:
        try:
            tx = await client.get_transaction(
                signature, encoding="jsonParsed", max_supported_transaction_version=0
            )
            # TODO: parser tx.value pour extraire le compte mint de l'instruction
            # de création (généralement l'un des tout premiers comptes listés
            # dans l'instruction "create" du programme Pump.fun).
            return None
        except Exception as e:
            logger.error(f"Erreur récupération transaction {signature}: {e}")
            return None


# ---------------------------------------------------------------------------
# Exécution des swaps via Jupiter
# ---------------------------------------------------------------------------

async def get_jupiter_quote(
    input_mint: str, output_mint: str, amount: int, slippage_bps: int = SLIPPAGE_BPS
) -> Optional[dict]:
    url = f"{JUPITER_API_URL}/quote"
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippageBps": slippage_bps,
    }
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            r = await http.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            logger.error(f"Erreur quote Jupiter : {e}")
            return None


async def build_swap_transaction(quote: dict, wallet_pubkey: str) -> Optional[str]:
    """Retourne la transaction sérialisée (base64) prête à signer."""
    url = f"{JUPITER_API_URL}/swap"
    payload = {
        "quoteResponse": quote,
        "userPublicKey": wallet_pubkey,
        "wrapAndUnwrapSol": True,
        "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
            "priorityLevel": "high", "maxLamports": COMPUTE_UNIT_PRICE * 1000
        }},
    }
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
            return r.json().get("swapTransaction")
        except httpx.HTTPError as e:
            logger.error(f"Erreur construction swap Jupiter : {e}")
            return None


async def execute_swap(
    input_mint: str, output_mint: str, amount: int, keypair: Keypair
) -> Optional[str]:
    """Exécute un swap complet (quote -> build -> sign -> send). Retourne la signature."""
    quote = await get_jupiter_quote(input_mint, output_mint, amount)
    if not quote:
        logger.error("Impossible d'obtenir une quote Jupiter — swap annulé.")
        return None

    swap_tx_b64 = await build_swap_transaction(quote, str(keypair.pubkey()))
    if not swap_tx_b64:
        logger.error("Impossible de construire la transaction de swap — annulé.")
        return None

    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
    signed_tx = VersionedTransaction(raw_tx.message, [keypair])

    async with AsyncClient(RPC_URL) as client:
        try:
            resp = await client.send_raw_transaction(
                bytes(signed_tx), opts={"skip_preflight": True, "preflight_commitment": Confirmed}
            )
            signature = str(resp.value)
            logger.info(f"Transaction envoyée : {signature}")
            return signature
        except Exception as e:
            logger.error(f"Erreur envoi transaction : {e}")
            return None


async def execute_buy(mint: str, amount_sol: float = BUY_AMOUNT_SOL) -> Optional[str]:
    keypair = get_keypair()
    if not keypair:
        logger.error("Achat impossible : wallet non configuré.")
        return None
    lamports = int(amount_sol * 1_000_000_000)
    logger.info(f"Achat de {amount_sol} SOL sur {mint}...")
    return await execute_swap(SOL_MINT, mint, lamports, keypair)


async def execute_sell(mint: str, amount_tokens: int) -> Optional[str]:
    keypair = get_keypair()
    if not keypair:
        logger.error("Vente impossible : wallet non configuré.")
        return None
    logger.info(f"Vente de {amount_tokens} unités de {mint}...")
    return await execute_swap(mint, SOL_MINT, amount_tokens, keypair)


async def get_jupiter_honeypot_check(mint: str):
    """
    Fonction compatible avec jupiter_quote_fn attendu par rug_checker.check_honeypot().
    Simule un aller-retour SOL -> token -> SOL sans envoyer de transaction,
    juste via les quotes (pas de frais, pas de risque).
    """
    test_amount = 10_000_000  # 0.01 SOL en lamports
    buy_quote = await get_jupiter_quote(SOL_MINT, mint, test_amount)
    if not buy_quote:
        return False, False, 100.0

    tokens_out = int(buy_quote.get("outAmount", 0))
    if tokens_out == 0:
        return False, False, 100.0

    sell_quote = await get_jupiter_quote(mint, SOL_MINT, tokens_out)
    if not sell_quote:
        return True, False, 100.0

    sol_back = int(sell_quote.get("outAmount", 0))
    loss_pct = max(0.0, (1 - sol_back / test_amount) * 100)
    sell_ok = sol_back > 0
    return True, sell_ok, round(loss_pct, 2)


# ---------------------------------------------------------------------------
# Exemple d'utilisation autonome (test)
# ---------------------------------------------------------------------------

async def _demo():
    logging.basicConfig(level=logging.INFO)
    logger.info("Démarrage de la détection (Ctrl+C pour arrêter)...")
    async for mint in listen_new_pools():
        logger.info(f"Nouveau mint potentiel détecté : {mint}")


if __name__ == "__main__":
    asyncio.run(_demo())
