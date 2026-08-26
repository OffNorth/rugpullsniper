# Sniper Bot — Pump.fun / Raydium (Telegram)

Bot Telegram qui détecte les nouveaux tokens lancés sur Pump.fun/Raydium (Solana),
les filtre avec un score anti-rug avant tout achat, et envoie les alertes/statuts
directement sur Telegram.

## Architecture

```
.
├── rug_checker.py       # Scoring anti-rug (mint/freeze authority, LP, holders, honeypot)
├── sniper_engine.py     # Détection de nouveaux pools + exécution des achats (à venir)
├── telegram_bot.py      # POINT D'ENTREE PRINCIPAL — orchestre détection, scoring, alertes, commandes
├── .env                 # Config (à créer, jamais commité)
├── .env.example          # Modèle de configuration
└── requirements.txt
```

`telegram_bot.py` importe directement `rug_checker.py` : chaque nouveau token
détecté passe par `RugChecker.evaluate()` avant qu'une alerte ou un achat
ne soit déclenché. `sniper_engine.py` viendra s'y brancher pour la détection
de pools en temps réel et l'exécution des transactions (actuellement un
placeholder dans `pool_detection_loop()`).

## Prérequis

- Python 3.10+
- Un wallet Solana dédié au bot (jamais ton wallet principal)
- Un RPC Solana payant (Helius, QuickNode ou Triton) — le RPC public est trop
  lent/rate-limité pour du sniping en temps réel
- Un bot Telegram créé via [@BotFather](https://t.me/BotFather)

## Installation

```bash
git clone <ton-repo>
cd <ton-repo>
python3 -m venv venv
source venv/bin/activate  # Windows : venv\Scripts\activate
pip install -r requirements.txt
```

### requirements.txt

```
solders
solana
httpx
python-dotenv
python-telegram-bot
websockets
```

## Configuration (.env)

Copie le modèle puis remplis tes propres valeurs :

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `RPC_URL` | Endpoint RPC HTTPS (Helius/QuickNode/Triton) |
| `WSS_URL` | Endpoint WebSocket du même fournisseur, pour la détection temps réel |
| `BIRDEYE_API_KEY` | Clé API Birdeye (optionnel, pour données complémentaires) |
| `JUPITER_API_URL` | API Jupiter pour les quotes/swaps (défaut fourni) |
| `WALLET_PRIVATE_KEY` | Clé privée base58 du wallet dédié au bot |
| `TELEGRAM_BOT_TOKEN` | Token du bot, donné par @BotFather |
| `TELEGRAM_CHAT_ID` | ID du chat/canal où envoyer les alertes |
| `MIN_LIQUIDITY_USD` | Liquidité minimale acceptée (défaut : 3000) |
| `MAX_TOP_HOLDER_PCT` | % max détenu par un seul wallet (défaut : 15) |
| `MIN_SCORE_TO_BUY` | Score anti-rug minimal pour déclencher un achat (défaut : 75) |
| `AUTO_BUY` | `false` par défaut = alertes seules ; `true` = achat automatique dès qu'un token passe le seuil (risqué) |
| `BUY_AMOUNT_SOL` | Montant en SOL par achat |
| `SLIPPAGE_BPS` | Slippage toléré en points de base (2000 = 20%) |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | Seuils de sortie automatique |

**⚠️ Sécurité :** ajoute `.env` à ton `.gitignore` avant tout commit. La clé
privée du wallet ne doit jamais apparaître dans l'historique Git.

## Créer le bot Telegram

1. Ouvre une conversation avec [@BotFather](https://t.me/BotFather) sur Telegram
2. Envoie `/newbot`, choisis un nom et un username (doit finir par `bot`)
3. Copie le token fourni dans `TELEGRAM_BOT_TOKEN`
4. Pour récupérer ton `TELEGRAM_CHAT_ID` : ajoute le bot à ton chat/canal, envoie
   un message, puis va sur `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`
   et repère le champ `"chat":{"id": ...}`

## Lancer le bot

```bash
python3 telegram_bot.py
```

Le bot va :
1. Écouter les nouveaux pools via WebSocket RPC
2. Passer chaque nouveau token dans `rug_checker.py` pour obtenir un score /100
3. Si le score dépasse `MIN_SCORE_TO_BUY`, envoyer une alerte Telegram avec le
   détail du scoring
4. Selon la config, exécuter l'achat automatiquement ou attendre une
   confirmation manuelle via une commande Telegram (`/buy <mint>`)

## Commandes Telegram prévues

| Commande | Effet |
|---|---|
| `/status` | Statut du bot (connecté, RPC, wallet, solde) |
| `/watchlist` | Liste des tokens en cours de surveillance |
| `/buy <mint>` | Forcer un achat manuel sur un token |
| `/sell <mint>` | Forcer une vente manuelle |
| `/config` | Afficher les seuils actuels (lecture seule) |

## Avertissement

Ce bot exécute des transactions financières réelles sur un marché à très haut
risque (memecoins nouvellement lancés). Le scoring anti-rug réduit le risque
mais ne l'élimine pas :
- Un token peut passer tous les filtres et quand même perdre 90%+ de sa valeur
  par simple manque d'acheteurs (pas besoin d'un rug pull pour perdre l'argent)
- N'investis jamais plus que ce que tu es prêt à perdre entièrement
- Teste d'abord avec de très petits montants (`BUY_AMOUNT_SOL` bas) avant de
  scaler
