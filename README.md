# Sniper Bot — Pump.fun / Raydium (Telegram)

Bot Telegram qui détecte les nouveaux tokens lancés sur Pump.fun/Raydium (Solana),
les filtre avec un score anti-rug avant tout achat, et envoie les alertes/statuts
directement sur Telegram.

## Architecture

```
.
├── rug_checker.py       # Scoring anti-rug (mint/freeze authority, LP, holders, honeypot)
├── sniper_engine.py     # Détection de nouveaux pools + exécution des achats/ventes (Jupiter)
├── telegram_bot.py      # POINT D'ENTREE PRINCIPAL — orchestre détection, scoring, alertes, commandes
├── .env                 # Config (à créer, jamais commité)
├── .env.example          # Modèle de configuration
└── requirements.txt
```

`telegram_bot.py` importe directement `rug_checker.py` et `sniper_engine.py` :
chaque nouveau token détecté passe par `RugChecker.evaluate()` avant qu'une
alerte ou un achat ne soit déclenché, et les commandes `/buy`/`/sell`
exécutent de vraies transactions via `sniper_engine.py`.

## Prérequis

- Python 3.10+
- Un wallet Solana dédié au bot (jamais ton wallet principal)
- Un RPC Solana payant (Helius, QuickNode ou Triton) — le RPC public est trop
  lent/rate-limité pour du sniping en temps réel
- Un bot Telegram créé via [@BotFather](https://t.me/BotFather)

## Installation

### Linux / macOS

```bash
# 1. Cloner le repo
git clone <ton-repo>
cd <ton-repo>

# 2. Vérifier que Python 3.10+ est installé
python3 --version

# 3. Créer et activer un environnement virtuel
python3 -m venv venv
source venv/bin/activate

# 4. Installer les dépendances
pip install -r requirements.txt

# 5. Créer le fichier de config
cp .env.example .env
nano .env   # ou vim/gedit — remplis tes propres valeurs

# 6. Lancer le bot
python3 telegram_bot.py
```

Pour désactiver l'environnement virtuel plus tard : `deactivate`.

### Windows

**Option A — PowerShell (recommandé)**

```powershell
# 1. Cloner le repo
git clone <ton-repo>
cd <ton-repo>

# 2. Vérifier que Python 3.10+ est installé
python --version

# 3. Créer et activer un environnement virtuel
python -m venv venv
venv\Scripts\Activate.ps1
```

Si PowerShell bloque le script avec une erreur d'exécution de policy, lance
d'abord (une seule fois, en administrateur) :
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

```powershell
# 4. Installer les dépendances
pip install -r requirements.txt

# 5. Créer le fichier de config
copy .env.example .env
notepad .env   # remplis tes propres valeurs

# 6. Lancer le bot
python telegram_bot.py
```

**Option B — Invite de commandes (cmd)**

```cmd
git clone <ton-repo>
cd <ton-repo>
python -m venv venv
venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env
notepad .env
python telegram_bot.py
```

**Notes Windows :**
- Si `python` n'est pas reconnu, réinstalle Python depuis [python.org](https://www.python.org/downloads/)
  en cochant bien "Add Python to PATH" pendant l'installation.
- `git` doit être installé séparément si ce n'est pas déjà fait : [git-scm.com](https://git-scm.com/download/win)
- Certaines dépendances (`solders`) sont écrites en Rust et distribuées en
  binaire précompilé (wheel) — normalement aucune compilation locale n'est
  nécessaire, mais si l'installation échoue, vérifie que tu as bien Python
  3.10, 3.11 ou 3.12 (les wheels ne couvrent pas toujours la toute dernière
  version de Python immédiatement après sa sortie).

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

## Limite connue à corriger avant utilisation réelle

L'extraction de l'adresse mint depuis les logs WebSocket dans
`sniper_engine.py::_extract_mint_from_logs()` est un placeholder : elle
détecte qu'une création de token a eu lieu, mais ne récupère pas encore le
mint exact. Il faut compléter `get_mint_from_signature()` en parsant la
transaction complète (`getTransaction`) pour extraire le bon compte selon
l'IDL du programme Pump.fun. Tant que ce n'est pas fait, le bot ne détectera
aucun token en conditions réelles.
