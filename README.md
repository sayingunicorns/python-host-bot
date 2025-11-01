# Python Host Bot v3 (Premium Multi-User)

This package contains a ready-to-deploy Telegram **Python Host Bot** (multi-user).
It is safe for beginners: no bot token is hardcoded. Use Render (recommended) or Railway.

## Files
- `main.py` — bot code
- `requirements.txt` — dependencies
- `render.yaml` — optional Render config (one-click)
- `config.json` — optional local token (template)

## Quick Deploy (Render.com) — Beginner friendly
1. Create a GitHub repo (public or private).
2. Upload these files to the repository.
3. On Render, create a **New → Web Service**, connect the repo.
4. Build command: `pip install -r requirements.txt`
5. Start command: `python main.py`
6. Add environment variables in Render:
   - `BOT_TOKEN` = your Telegram bot token (keep this private)
   - `ADMIN_IDS` = your Telegram user id (comma separated, optional)
7. Deploy. Open logs to see the bot starting.

## Local testing
Create `config.json`:
```
{
  "BOT_TOKEN": "12345:ABC...",
  "ADMIN_IDS": "123456789"
}
```
Then run:
```
python -m pip install -r requirements.txt
python main.py
```

## Notes
- On Heroku the filesystem is ephemeral; use Postgres and S3 for long-term persistence.
- Running arbitrary user code is risky — for a public bot consider adding Docker sandboxing per app.
