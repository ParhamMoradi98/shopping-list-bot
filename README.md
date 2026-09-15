# PanicCart

Telegram bot for a shared household shopping list. Items are tagged with a store and an emergency score from 0–10, so you can ask “where should I go?” and get the panic items with stores attached.

## Local run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set TELEGRAM_BOT_TOKEN
python shopping_bot.py
```

`.env` is loaded automatically. Talk to the bot in Telegram after it prints `PanicCart running`.

## How it works

**Add an item** — send a name (or tap ➕ Add). Then pick a store:

1. Costco
2. PC stores
3. Shoppers
4. Persian food markets

If you pick Persian, you get:

0. All stores
1. Paeez
2. Heeva
3. Arzon
4. Khorak
5. Irooni market
6. Koorosh

Then tag urgency **0–10**. The item lands on that store’s list. Edit the store list any time with `/stores` (add, rename, hide, nest new groups).

**Where to go** — `/go` (or 🚨 Where to go). Starts with emergency **10**, grouped by grocery store, plus a shortest-run suggestion. Cutoff buttons: 10s / 8+ / 5+ / all.

**At a store** — `/at` (or 🛒 At a store). Pick the store, see what’s needed with urgency, tap ✅ as you grab things.

Quick paste (no buttons):

```
milk | costco | 8
sabzi | paeez | 10 | 2 bunches
yogurt | persian | 9
```

## Extra

- Persistent buttons on the keyboard: Add, At a store, Where to go, The list, Stores, Home
- Household sharing (`/invite` / `/join`) — same cart, personal digest times
- Claim “I’ll grab it” so you don’t double-buy
- Staples: mark an item 🔁, then Restock from Stats after a shop
- `/roll` weighted toward panic-10s
- Morning digest of 8+ items
- `/find`, `/stats`, `/export`
- `/at paeez` jumps straight to that aisle list

## Deploy on Railway (from GitHub)

1. Push this repo to GitHub.
2. In [Railway](https://railway.app): **New Project → Deploy from GitHub** → pick the repo.
3. Add variables:
   - `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `SHOP_TZ` — optional, default `America/Toronto`
4. Attach a **Volume** mounted at `/data` so SQLite survives redeploys.
   The bot writes to `/data/shopping.db` when `/data` exists (or set `SHOP_DB`).
5. Start command is already in `railway.toml`: `python shopping_bot.py`.
6. No public HTTP URL — the bot uses Telegram long polling.

## Env vars

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather |
| `SHOP_DB` | no | SQLite path (default: `/data/shopping.db` on Railway, else `./shopping.db`) |
| `SHOP_TZ` | no | Default timezone for new members |
