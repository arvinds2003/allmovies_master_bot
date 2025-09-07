
# AllMovies UltraPro Bot — Render-ready

**Features**
- FastAPI server + secure Telegram webhook
- python-telegram-bot v21 (async)
- MongoDB logging (motor)
- In-memory caching + rate limiting
- Health endpoints `/health`
- Procfile for Render (Gunicorn + Uvicorn workers)

## Deploy on Render
1. Create new **Web Service**.
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn -k uvicorn.workers.UvicornWorker web:app --workers=2 --timeout=120`
4. Add env vars from `.env` (copy values).
5. Set `WEBHOOK_URL` to your Render URL (e.g. `https://<service>.onrender.com`) and redeploy.

Webhook endpoint created automatically on startup.
