# pdfrm-tgbot

A Telegram bot worker for Ubuntu that:
- polls a local `telegram-bot-api` server,
- receives PDF files,
- optionally renames output based on the next text message,
- removes watermark-like layers using the `tools/pdfw.py` pipeline,
- sends processed PDFs back to the user.

## Prerequisites (Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-venv poppler-utils
```

Run local Telegram Bot API server separately (example):

```bash
telegram-bot-api --local --http-port=8081 --api-id=<API_ID> --api-hash=<API_HASH>
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run once (for cron/systemd timer)

```bash
python bot.py --token <BOT_TOKEN> --api-base http://127.0.0.1:8081
```

## Scheduled run with GitHub Actions (every 10 minutes)

Workflow file: `.github/workflows/run-bot.yml`

It runs on `ubuntu-latest` every 10 minutes, starts local `telegram-bot-api` (jakbin binary) on port `8081`, and then executes:

```bash
python bot.py --token <BOT_TOKEN> --api-base <TELEGRAM_API_BASE>
```

### GitHub Secrets to configure

In GitHub repository: **Settings → Secrets and variables → Actions → New repository secret**

Create these secrets:
- `BOT_TOKEN` (required)
- `API_ID` (required; used to start `jakbin/telegram-bot-api-binary` in workflow)
- `API_HASH` (required; used to start `jakbin/telegram-bot-api-binary` in workflow)
- `TELEGRAM_API_BASE` (optional, defaults to `http://localhost:8081`)

## Text-based renaming behavior

For each received PDF, the bot waits for the next plain text message from the same chat:
- If text arrives, output file is renamed to `<text>.pdf`.
- If no text arrives, it uses the original PDF name.

Bot state and downloaded/generated files are saved in `data/`.
