"""
rug_checker.py
==============
Module de scoring anti-rug pour un bot sniper Solana (Pump.fun / Raydium).

Objectif : donner un score /100 à un token AVANT d'acheter, basé sur des
critères objectifs vérifiables on-chain + via API (Dexscreener/Birdeye).

Ce module ne fait AUCUN achat lui-même : il ne fait que décider si un token
est "safe" ou non, selon un seuil configurable. Il doit être branché en amont
de ta logique d'exécution d'ordre (voir sniper_engine.py).

Dépendances :
    pip install solders solana httpx
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from dotenv import load_dotenv
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

# ---------------------------------------------------------------------------
# Configuration (chargée depuis .env — voir .env.example)
# ---------------------------------------------------------------------------

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/{mint}"

# Pondération de chaque critère (total = 100)
WEIGHTS = {
    "mint_authority_renounced": 20,
    "freeze_authority_renounced": 15,
    "lp_locked_or_burned": 25,
    "holder_distribution": 20,
    "not_honeypot": 15,
    "liquidity_min": 5,
}

# Seuils (surchargeables via .env)
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", 3000))
MAX_TOP_HOLDER_PCT = float(os.getenv("MAX_TOP_HOLDER_PCT", 15.0))
MIN_SCORE_TO_BUY = int(os.getenv("MIN_SCORE_TO_BUY", 75))

if RPC_URL == "https://api.mainnet-beta.solana.com":
    print(
        "[AVERTISSEMENT] Tu utilises le RPC public Solana — "
        "trop lent/rate-limité pour du sniping. "
        "Configure RPC_URL dans ton .env avec Helius/QuickNode/Triton."
    )


@dataclass
class RugCheckResult:
    mint: str
    score: int = 0
    max_score: int = 100
    details: dict = field(default_factory=dict)
    passed: bool = False
    reasons_failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Token {self.mint} — score {self.score}/{self.max_score}"]
        for k, v in self.details.items():
            lines.append(f"  - {k}: {v}")
        if self.reasons_failed:
            lines.append("  ECHECS: " + ", ".join(self.reasons_failed))
        lines.append(f"  => {'ACHAT AUTORISE' if self.passed else 'REJETE'}")
        return "\n".join(lines)


class RugChecker:
    def __init__(self, rpc_url: str = RPC_URL, min_score: int = MIN_SCORE_TO_BUY):
        self.rpc_url = rpc_url
        self.min_score = min_score
        self._client: Optional[AsyncClient] = None
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = AsyncClient(self.rpc_url)
        self._http = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.close()
        if self._http:
            await self._http.aclose()

    # -----------------------------------------------------------------
    # Critère 1 & 2 : mint / freeze authority
    # -----------------------------------------------------------------
    async def check_authorities(self, mint: str) -> tuple[bool, bool]:
        """Retourne (mint_renoncee, freeze_renoncee)."""
        pubkey = Pubkey.from_string(mint)
        resp = await self._client.get_account_info_json_parsed(pubkey)
        try:
            parsed = resp.value.data.parsed["info"]
            mint_renounced = parsed.get("mintAuthority") is None
            freeze_renounced = parsed.get("freezeAuthority") is None
            return mint_renounced, freeze_renounced
        except (AttributeError, KeyError, TypeError):
            # Impossible de parser -> on considère par prudence que c'est un échec
            return False, False

    # -----------------------------------------------------------------
    # Critère 3 : liquidité verrouillée / burnée + montant
    # -----------------------------------------------------------------
    async def check_liquidity(self, mint: str) -> dict:
        """Interroge Dexscreener pour la liquidité et son statut."""
        url = DEXSCREENER_API.format(mint=mint)
        try:
            r = await self._http.get(url)
            data = r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return {"liquidity_usd": 0, "lp_locked": False}
            pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
            liquidity_usd = pair.get("liquidity", {}).get("usd", 0)
            # Dexscreener n'indique pas toujours le lock directement ;
            # pour une vraie vérif de lock, interroger l'API de Streamflow/Unicrypt
            # ou vérifier si les LP tokens sont dans une adresse burn connue.
            lp_locked = liquidity_usd > 0  # placeholder, à affiner avec check LP token holder
            return {"liquidity_usd": liquidity_usd, "lp_locked": lp_locked}
        except (httpx.HTTPError, KeyError, ValueError):
            return {"liquidity_usd": 0, "lp_locked": False}

    # -----------------------------------------------------------------
    # Critère 4 : distribution des holders
    # -----------------------------------------------------------------
    async def check_holder_distribution(self, mint: str) -> dict:
        pubkey = Pubkey.from_string(mint)
        try:
            largest = await self._client.get_token_largest_accounts(pubkey)
            accounts = largest.value
            if not accounts:
                return {"top_holder_pct": 100.0, "ok": False}
            supply_resp = await self._client.get_token_supply(pubkey)
            total_supply = int(supply_resp.value.amount)
            if total_supply == 0:
                return {"top_holder_pct": 100.0, "ok": False}
            top_amount = int(accounts[0].amount.amount)
            top_pct = (top_amount / total_supply) * 100
            return {"top_holder_pct": round(top_pct, 2), "ok": top_pct <= MAX_TOP_HOLDER_PCT}
        except Exception:
            return {"top_holder_pct": 100.0, "ok": False}

    # -----------------------------------------------------------------
    # Critère 5 : simulation honeypot (achat + revente à blanc)
    # -----------------------------------------------------------------
    async def check_honeypot(self, mint: str, jupiter_quote_fn) -> dict:
        """
        jupiter_quote_fn : fonction injectée qui appelle l'API Jupiter
        pour simuler un swap SOL->token puis token->SOL sans l'exécuter.
        Doit retourner (buy_ok: bool, sell_ok: bool, sell_tax_pct: float).
        """
        try:
            buy_ok, sell_ok, sell_tax_pct = await jupiter_quote_fn(mint)
            is_honeypot = (not sell_ok) or sell_tax_pct > 50
            return {"sell_ok": sell_ok, "sell_tax_pct": sell_tax_pct, "is_honeypot": is_honeypot}
        except Exception:
            # Si on ne peut pas vérifier, on est prudent : on ne bloque pas
            # mais on ne donne pas les points -> traité au niveau du score
            return {"sell_ok": None, "sell_tax_pct": None, "is_honeypot": None}

    # -----------------------------------------------------------------
    # Scoring global
    # -----------------------------------------------------------------
    async def evaluate(self, mint: str, jupiter_quote_fn=None) -> RugCheckResult:
        result = RugCheckResult(mint=mint)

        mint_renounced, freeze_renounced = await self.check_authorities(mint)
        liquidity_info = await self.check_liquidity(mint)
        holder_info = await self.check_holder_distribution(mint)

        result.details["mint_authority_renounced"] = mint_renounced
        result.details["freeze_authority_renounced"] = freeze_renounced
        result.details["liquidity_usd"] = liquidity_info["liquidity_usd"]
        result.details["lp_locked"] = liquidity_info["lp_locked"]
        result.details["top_holder_pct"] = holder_info["top_holder_pct"]

        score = 0
        if mint_renounced:
            score += WEIGHTS["mint_authority_renounced"]
        else:
            result.reasons_failed.append("mint authority non renoncée")

        if freeze_renounced:
            score += WEIGHTS["freeze_authority_renounced"]
        else:
            result.reasons_failed.append("freeze authority non renoncée")

        if liquidity_info["lp_locked"]:
            score += WEIGHTS["lp_locked_or_burned"]
        else:
            result.reasons_failed.append("liquidité non verrouillée/burnée")

        if liquidity_info["liquidity_usd"] >= MIN_LIQUIDITY_USD:
            score += WEIGHTS["liquidity_min"]
        else:
            result.reasons_failed.append(
                f"liquidité insuffisante ({liquidity_info['liquidity_usd']}$ < {MIN_LIQUIDITY_USD}$)"
            )

        if holder_info["ok"]:
            score += WEIGHTS["holder_distribution"]
        else:
            result.reasons_failed.append(
                f"concentration holders trop élevée ({holder_info['top_holder_pct']}%)"
            )

        if jupiter_quote_fn is not None:
            honeypot_info = await self.check_honeypot(mint, jupiter_quote_fn)
            result.details["honeypot_check"] = honeypot_info
            if honeypot_info["is_honeypot"] is False:
                score += WEIGHTS["not_honeypot"]
            elif honeypot_info["is_honeypot"] is True:
                result.reasons_failed.append("honeypot détecté (revente impossible ou taxe > 50%)")
            # si None (impossible à vérifier), on ne donne ni ne retire de points
        else:
            result.details["honeypot_check"] = "non testé (jupiter_quote_fn non fourni)"

        result.score = score
        result.passed = score >= self.min_score
        return result


# ---------------------------------------------------------------------------
# Exemple d'utilisation
# ---------------------------------------------------------------------------

async def _demo():
    example_mint = "So11111111111111111111111111111111111111112"  # exemple: wSOL
    async with RugChecker(min_score=MIN_SCORE_TO_BUY) as checker:
        result = await checker.evaluate(example_mint)
        print(result.summary())


if __name__ == "__main__":
    asyncio.run(_demo())
