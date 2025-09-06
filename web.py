import os, asyncio, logging
from typing import Optional, Any, Dict
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
import httpx
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters
from motor.motor_asyncio import AsyncIOMotorClient
from collections import defaultdict, deque

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or "0")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "wh_dev")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in env")

app = FastAPI(title="AllMovies UltraPro Bot", version="1.0.0")
application: Optional[Application] = None
mongo_client: Optional[AsyncIOMotorClient] = None
db = None

class CacheItem(BaseModel):
    value: Any
    expiry: datetime

cache: Dict[str, CacheItem] = {}
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))
RL_WINDOW = 30
RL_LIMIT = 15
user_events: Dict[int, deque] = defaultdict(deque)

async def tmdb_search(title: str):
    if not TMDB_API_KEY:
        return None
    key = f"tmdb:{title.lower()}"
    now = datetime.utcnow()
    ci = cache.get(key)
    if ci and ci.expiry > now:
        return ci.value
    url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "include_adult": "false"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    cache[key] = CacheItem(value=data, expiry=now + timedelta(seconds=CACHE_TTL_SECONDS))
    return data

async def omdb_lookup(title: str):
    if not OMDB_API_KEY:
        return None
    key = f"omdb:{title.lower()}"
    now = datetime.utcnow()
    ci = cache.get(key)
    if ci and ci.expiry > now:
        return ci.value
    url = "http://www.omdbapi.com/"
    params = {"apikey": OMDB_API_KEY, "t": title}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    cache[key] = CacheItem(value=data, expiry=now + timedelta(seconds=CACHE_TTL_SECONDS))
    return data

def rate_limited(uid: int) -> bool:
    dq = user_events[uid]
    now = datetime.utcnow().timestamp()
    while dq and now - dq[0] > RL_WINDOW:
        dq.popleft()
    if len(dq) >= RL_LIMIT:
        return True
    dq.append(now)
    return False

async def start_cmd(update, context):
    await context.bot.send_message(update.effective_chat.id, "🎬 Welcome to *AllMovies UltraPro*!\nSend a movie name.", parse_mode="Markdown")

async def help_cmd(update, context):
    await context.bot.send_message(update.effective_chat.id, "Send a movie name. Example: `Jailer`", parse_mode="Markdown")

async def ping_cmd(update, context):
    await context.bot.send_message(update.effective_chat.id, "pong ✅")

async def text_handler(update: Update, context):
    uid = update.effective_user.id if update.effective_user else 0
    if rate_limited(uid):
        return await context.bot.send_message(update.effective_chat.id, "⏳ Too many requests. Slow down.")
    q = (update.message.text or "").strip()
    # Log to DB
    try:
        if db:
            await db.searches.insert_one({"user_id": uid, "q": q, "at": datetime.utcnow()})
    except Exception as e:
        log.warning("DB log failed: %s", e)
    info = await tmdb_search(q)
    if info and info.get("results"):
        top = info["results"][0]
        title = top.get("title") or q
        year = (top.get("release_date") or "")[:4]
        rating = top.get("vote_average", "N/A")
        caption = f"""🎬 *{title}* ({year})
⭐ {rating} / 10 (TMDB)"""
        poster = top.get("poster_path")
        if poster:
            url = f"https://image.tmdb.org/t/p/w500{poster}"
            return await context.bot.send_photo(update.effective_chat.id, url, caption=caption, parse_mode="Markdown")
        return await context.bot.send_message(update.effective_chat.id, caption, parse_mode="Markdown")
    om = await omdb_lookup(q)
    if om and om.get("Response") == "True":
        poster = om.get("Poster")
        caption = f"""🎬 *{om.get('Title','?')}* ({om.get('Year','')})
⭐ {om.get('imdbRating','N/A')} / 10 (IMDB)"""
        if poster and poster != "N/A":
            return await context.bot.send_photo(update.effective_chat.id, poster, caption=caption, parse_mode="Markdown")
        return await context.bot.send_message(update.effective_chat.id, caption, parse_mode="Markdown")
    await context.bot.send_message(update.effective_chat.id, "❌ Not found. Try another title.")

def build_application() -> Application:
    appb = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    appb.add_handler(CommandHandler("start", start_cmd))
    appb.add_handler(CommandHandler("help", help_cmd))
    appb.add_handler(CommandHandler("ping", ping_cmd))
    appb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return appb

app_tele: Optional[Application] = None
client: Optional[AsyncIOMotorClient] = None

@app.on_event("startup")
async def startup():
    global app_tele, db, client, BASE_URL
    if MONGODB_URI:
        client = AsyncIOMotorClient(MONGODB_URI, uuidRepresentation="standard")
        try:
            db = client.get_default_database()
        except Exception:
            db = client["allmovies"]
    app_tele = build_application()
    await app_tele.initialize()
    await app_tele.start()
    if not BASE_URL:
        BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if BASE_URL:
        wh = f"{BASE_URL}/webhook/{BOT_TOKEN}?secret={WEBHOOK_SECRET}"
        await app_tele.bot.set_webhook(wh, allowed_updates=None)
        log.info("Webhook set to %s", wh)

@app.on_event("shutdown")
async def shutdown():
    global app_tele, client
    try:
        if app_tele:
            await app_tele.bot.delete_webhook(drop_pending_updates=False)
            await app_tele.stop()
            await app_tele.shutdown()
    finally:
        if client:
            client.close()

class UP(BaseModel):
    pass

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")
    if request.query_params.get("secret", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret")
    data = await request.json()
    update = Update.de_json(data, app_tele.bot)
    await app_tele.process_update(update)
    return JSONResponse({"ok": True})

@app.get("/polling/start")
async def polling_start():
    asyncio.create_task(app_tele.run_polling(allowed_updates=None))
    return PlainTextResponse("polling started")
  
