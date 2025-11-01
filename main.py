#!/usr/bin/env python3
"""
HostBot v3 — Premium Multi-User Python Host (Render / Heroku compatible)

This version is beginner-friendly and uses environment variables for secrets.
It is safe to upload to GitHub. Do NOT commit your real token into a public repo.
If you are a beginner, follow README.md steps — easiest approach is to set BOT_TOKEN in Render env vars.

Features:
- Multi-user isolated folders per user
- Upload .py or .zip; extracts first .py
- Background running with subprocess.Popen (unbuffered)
- Per-app logs in /tmp/hostbot/logs (Render-friendly)
- Auto-detect ModuleNotFoundError in logs and attempt pip install (with safe retry)
- Commands: /start, /help, /upload (send file), /myapps, /panel, /logs <id>, /stop <id>, /stats, /admin (owner only)
"""

import os
import sys
import json
import sqlite3
import asyncio
import zipfile
import subprocess
import signal
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

try:
    import psutil
    import aiofiles
    from aiogram import Bot, Dispatcher, types
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from aiogram.filters import Command
except Exception as e:
    print("Missing packages. Install requirements.txt then rerun.")
    raise

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CONFIG_PATH = Path("config.json")
if not BOT_TOKEN and CONFIG_PATH.exists():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        BOT_TOKEN = cfg.get("BOT_TOKEN")
    except Exception:
        BOT_TOKEN = None

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not found. Set environment variable BOT_TOKEN or create config.json with BOT_TOKEN.")
    sys.exit(1)

# Admin IDs (comma separated env var or config)
ADMIN_IDS = os.getenv("ADMIN_IDS", "")
if not ADMIN_IDS and CONFIG_PATH.exists():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        ADMIN_IDS = cfg.get("ADMIN_IDS", "")
    except Exception:
        ADMIN_IDS = ""
ADMIN_IDS = [int(x.strip()) for x in str(ADMIN_IDS).split(",") if str(x).strip().isdigit()]

# Host paths (use /tmp for Render/Heroku safety)
BASE_DIR = Path(os.getenv("HOSTBOT_BASE", "/tmp/hostbot"))
USERS_DIR = BASE_DIR / "users"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "hostbot.db"

# Configurable limits
MAX_APPS_PER_USER = int(os.getenv("MAX_APPS_PER_USER", "6"))
MODULE_INSTALL_RETRIES = int(os.getenv("MODULE_INSTALL_RETRIES", "1"))
PIP_INSTALL_TIMEOUT = int(os.getenv("PIP_INSTALL_TIMEOUT", "120"))  # seconds
LOG_SCAN_INTERVAL = int(os.getenv("LOG_SCAN_INTERVAL", "6"))  # seconds
APP_LOG_TAIL_CHARS = int(os.getenv("APP_LOG_TAIL_CHARS", "3500"))

# Ensure directories exist
for p in (BASE_DIR, USERS_DIR, LOGS_DIR):
    p.mkdir(parents=True, exist_ok=True)

# ---------------- Database helpers ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        name TEXT,
        folder TEXT,
        entrypoint TEXT,
        pid INTEGER DEFAULT 0,
        status TEXT DEFAULT 'stopped',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_start TIMESTAMP,
        install_attempts INTEGER DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()

def register_user(user_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def add_app(user_id:int, chat_id:int, name:str, folder:str, entrypoint:str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO apps (user_id, chat_id, name, folder, entrypoint) VALUES (?, ?, ?, ?, ?)",
                (user_id, chat_id, name, folder, entrypoint))
    conn.commit()
    app_id = cur.lastrowid
    conn.close()
    return app_id

def update_app_pid(app_id:int, pid:int, status:str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE apps SET pid=?, status=?, last_start=CURRENT_TIMESTAMP WHERE id=?", (pid, status, app_id))
    conn.commit()
    conn.close()

def set_app_status(app_id:int, status:str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE apps SET status=? WHERE id=?", (status, app_id))
    conn.commit()
    conn.close()

def increment_install_attempts(app_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE apps SET install_attempts = install_attempts + 1 WHERE id=?", (app_id,))
    conn.commit()
    conn.close()

def get_app(app_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, chat_id, name, folder, entrypoint, pid, status, created_at, last_start, install_attempts FROM apps WHERE id=?", (app_id,))
    row = cur.fetchone()
    conn.close()
    return row

def list_user_apps(user_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, name, entrypoint, pid, status, created_at FROM apps WHERE user_id=? ORDER BY id DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def list_running_apps():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, name, pid FROM apps WHERE status='running'")
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_app(app_id:int):
    row = get_app(app_id)
    if not row:
        return False
    folder = Path(row[4])
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM apps WHERE id=?", (app_id,))
    conn.commit()
    conn.close()
    # remove folder and log file (best-effort)
    try:
        log_file = LOGS_DIR / f"{row[1]}_app_{app_id}.log"
        if log_file.exists():
            log_file.unlink()
    except Exception:
        pass
    try:
        if folder.exists() and folder.is_dir():
            for child in folder.rglob("*"):
                try:
                    if child.is_file():
                        child.unlink()
                except Exception:
                    pass
            try:
                folder.rmdir()
            except Exception:
                pass
    except Exception:
        pass
    return True

# ---------------- Utilities ----------------
def safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:120]

def find_first_py(dirpath: Path) -> Optional[Path]:
    for p in dirpath.rglob("*.py"):
        return p
    return None

def human_seconds(text_seconds: float) -> str:
    sec = int(text_seconds)
    parts = []
    for label, count in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if sec >= count:
            val, sec = divmod(sec, count)
            parts.append(f"{val}{label}")
    return " ".join(parts) if parts else "0s"

# ---------------- Process management ----------------
def start_process(entry:Path, cwd:Path, log_path:Path) -> Tuple[bool, str]:
    try:
        f = open(log_path, "a+", buffering=1)
        popen = subprocess.Popen(
            ["python3", "-u", str(entry)],
            cwd=str(cwd),
            stdout=f,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid
        )
        return True, str(popen.pid)
    except Exception as e:
        return False, f"start error: {e}"

def stop_process(pid:int):
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    except Exception:
        pass

# ---------------- Auto-install watcher ----------------
async def pip_install_package(package:str) -> Tuple[bool, str]:
    try:
        def run_install(pkg):
            res = subprocess.run(["python3", "-m", "pip", "install", pkg],
                                 capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
            return res.returncode, res.stdout + "\n" + res.stderr
        rc, out = await asyncio.to_thread(run_install, package)
        return (rc == 0, out)
    except subprocess.TimeoutExpired:
        return False, "pip install timed out"
    except Exception as e:
        return False, f"pip install failed: {e}"

async def scan_logs_for_missing_modules(bot: Bot):
    while True:
        await asyncio.sleep(LOG_SCAN_INTERVAL)
        rows = list_running_apps()
        for app_id, user_id, name, pid in rows:
            log_path = LOGS_DIR / f"{user_id}_app_{app_id}.log"
            if not log_path.exists():
                continue
            try:
                txt = log_path.read_text(errors="ignore")[-4000:]
            except Exception:
                continue
            if ("ModuleNotFoundError" in txt or "No module named" in txt):
                app = get_app(app_id)
                if not app:
                    continue
                install_attempts = app[10] or 0
                if install_attempts >= MODULE_INSTALL_RETRIES:
                    continue
                pkg = None
                for line in txt.splitlines()[::-1]:
                    if "ModuleNotFoundError" in line and "No module named" in line:
                        if "'" in line:
                            pkg = line.split("'")[1]
                            break
                    if "No module named" in line:
                        parts = line.split("No module named")
                        if len(parts) > 1:
                            candidate = parts[1].strip().strip(" '\"")
                            pkg = candidate.split(".")[0]
                            break
                if not pkg:
                    continue
                chat_id = app[2]
                try:
                    await bot.send_message(chat_id, f"⚠️ App *{app[3]}* (id `{app_id}`) missing package `{pkg}`. Attempting `pip install {pkg}`...", parse_mode="Markdown")
                except Exception:
                    pass
                if pid and pid>0:
                    try:
                        stop_process(pid)
                        set_app_status(app_id, "installing")
                    except Exception:
                        pass
                increment_install_attempts(app_id)
                ok, out = await pip_install_package(pkg)
                if ok:
                    set_app_status(app_id, "restarting")
                    await asyncio.sleep(1)
                    app_folder = Path(app[4])
                    entry = app_folder / app[5]
                    log_p = LOGS_DIR / f"{user_id}_app_{app_id}.log"
                    started, pidstr = start_process(entry, app_folder, log_p)
                    if started:
                        update_app_pid(app_id, int(pidstr), "running")
                        try:
                            await bot.send_message(chat_id, f"✅ Installed `{pkg}` and restarted app *{app[3]}* — pid `{pidstr}`.", parse_mode="Markdown")
                        except Exception:
                            pass
                    else:
                        set_app_status(app_id, "error")
                        try:
                            await bot.send_message(chat_id, f"❌ Installed `{pkg}` but failed to start app: {pidstr}", parse_mode="Markdown")
                        except Exception:
                            pass
                else:
                    set_app_status(app_id, "error")
                    try:
                        await bot.send_message(chat_id, f"❌ Failed to install `{pkg}` for app *{app[3]}*. Output:\n<pre>{out[:1000]}</pre>", parse_mode="HTML")
                    except Exception:
                        pass

# ---------------- Bot UI helpers ----------------
def app_kb(app_id:int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🟢 Start", callback_data=f"start:{app_id}"),
         InlineKeyboardButton("🔴 Stop", callback_data=f"stop:{app_id}")],
        [InlineKeyboardButton("📜 Logs", callback_data=f"logs:{app_id}"),
         InlineKeyboardButton("🧾 Info", callback_data=f"info:{app_id}")],
        [InlineKeyboardButton("❌ Delete", callback_data=f"delete:{app_id}")]
    ])
    return kb

def user_panel_kb(user_id:int):
    apps = list_user_apps(user_id)
    buttons = []
    for r in apps[:8]:
        aid, name, entry, pid, status, created = r
        buttons.append([InlineKeyboardButton(f"{name} ({status})", callback_data=f"panelopen:{aid}")])
    if not buttons:
        buttons = [[InlineKeyboardButton("Upload your first app", callback_data="upload_hint")]]
    buttons.append([InlineKeyboardButton("Refresh", callback_data="panel_refresh")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------------- Bot & Dispatcher ----------------
bot = Bot(BOT_TOKEN, parse_mode="Markdown")
dp = Dispatcher()

async def human_typing(chat_id:int, seconds:float=0.6):
    try:
        await bot.send_chat_action(chat_id, "typing")
        await asyncio.sleep(seconds)
    except Exception:
        pass

# ---------------- Handlers ----------------
@dp.message(Command(commands=["start"]))
async def cmd_start(msg: types.Message):
    register_user(msg.from_user.id)
    await human_typing(msg.chat.id, 0.6)
    intro = (
        "👋 *Welcome to Python Host Pro*\n\n"
        "Host your Python apps (bots, scrapers, small services) directly from Telegram.\n\n"
        "Quick guide:\n"
        "1. Send a `.py` file (as Document) or a `.zip` containing your `.py`.\n"
        "2. Bot will register and start the app automatically.\n"
        "3. Open `/panel` to manage: Start / Stop / Logs / Delete.\n\n"
        "Useful commands: `/upload` (send file), `/myapps`, `/panel`, `/stats`, `/help`.\n\n"
        "⚠️ Note: Running arbitrary code on the same host has risks. This bot isolates per-user folders and uses per-app logs."
    )
    await msg.answer(intro)

@dp.message(Command(commands=["help"]))
async def cmd_help(msg: types.Message):
    txt = (
        "*Commands*\n"
        "/upload — send a .py or .zip file as Document to upload and auto-run\n"
        "/myapps — list your apps (with control buttons)\n"
        "/panel — interactive panel for your apps\n"
        "/logs <id> — show log tail for app id\n"
        "/stop <id> — stop app\n"
        "/stats — host resource & app counts\n"
        "/help — this message\n"
    )
    await msg.answer(txt)

@dp.message(Command(commands=["stats"]))
async def cmd_stats(msg: types.Message):
    mem = psutil.virtual_memory()
    uptime = time.time() - psutil.boot_time()
    total_users = 0
    total_apps = 0
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0] or 0
    except Exception:
        total_users = 0
    try:
        cur.execute("SELECT COUNT(*) FROM apps")
        total_apps = cur.fetchone()[0] or 0
    except Exception:
        total_apps = 0
    conn.close()
    txt = (
        f"📊 *Host Stats*\n"
        f"Uptime: `{human_seconds(uptime)}`\n"
        f"Memory: `{round(mem.used/1024/1024)}MB` / `{round(mem.total/1024/1024)}MB` ({mem.percent}%)\n"
        f"CPUs: `{psutil.cpu_count()}`\n"
        f"Users: `{total_users}` | Apps: `{total_apps}`\n"
    )
    await msg.answer(txt)

@dp.message(Command(commands=["myapps"]))
async def cmd_myapps(msg: types.Message):
    uid = msg.from_user.id
    rows = list_user_apps(uid)
    if not rows:
        await msg.answer("You don't have any apps yet. Send a `.py` file or `.zip` as Document to upload.")
        return
    for rid, name, entrypoint, pid, status, created_at in rows:
        txt = f"• *{name}* (id `{rid}`)\n  entry: `{entrypoint}`\n  status: `{status}` pid: `{pid}`\n  created: `{created_at}`"
        await msg.answer(txt, reply_markup=app_kb(rid))

@dp.message(Command(commands=["panel"]))
async def cmd_panel(msg: types.Message):
    uid = msg.from_user.id
    kb = user_panel_kb(uid)
    await msg.answer("🧭 Your Panel — quick app access", reply_markup=kb)

@dp.message()
async def handle_upload(msg: types.Message):
    doc = msg.document
    if not doc:
        return
    uid = msg.from_user.id
    register_user(uid)
    existing = list_user_apps(uid)
    if len(existing) >= MAX_APPS_PER_USER:
        await msg.answer(f"❌ App limit reached ({MAX_APPS_PER_USER}). Delete an app first.")
        return
    fname = doc.file_name or "file"
    ext = Path(fname).suffix.lower()
    safe = safe_name(fname)
    timestamp = int(time.time())
    user_folder = USERS_DIR / str(uid) / f"app_{timestamp}_{safe}"
    user_folder.mkdir(parents=True, exist_ok=True)
    saved_path = user_folder / fname
    await msg.document.download(destination_file=saved_path)
    await msg.answer(f"📥 Uploaded `{fname}` — processing...")
    entrypoint = None
    if ext == ".zip":
        try:
            with zipfile.ZipFile(saved_path, "r") as z:
                z.extractall(user_folder)
            py = find_first_py(user_folder)
            if not py:
                await msg.answer("❌ No .py found inside the zip.")
                return
            entrypoint = py.relative_to(user_folder).as_posix()
        except zipfile.BadZipFile:
            await msg.answer("❌ Bad zip file.")
            return
    elif ext == ".py":
        entrypoint = fname
    else:
        await msg.answer("ℹ️ Only `.py` and `.zip` uploads are supported for automatic execution. File saved.")
        return
    app_name = safe_name(Path(fname).stem)
    app_id = add_app(uid, msg.chat.id, app_name, str(user_folder.resolve()), entrypoint)
    log_file = LOGS_DIR / f"{uid}_app_{app_id}.log"
    log_file.touch(exist_ok=True)
    await msg.answer(f"🆕 App registered as id `{app_id}`. Starting now...")
    entry = user_folder / entrypoint
    ok, pid_or_msg = start_process(entry, user_folder, log_file)
    if ok:
        update_app_pid(app_id, int(pid_or_msg), "running")
        await msg.answer(f"🚀 App *{app_name}* started (id `{app_id}`) — pid `{pid_or_msg}`", parse_mode="Markdown")
    else:
        set_app_status(app_id, "error")
        await msg.answer(f"❌ Failed to start app: {pid_or_msg}")

@dp.message(Command(commands=["logs"]))
async def cmd_logs(msg: types.Message):
    args = msg.get_args().strip()
    if not args.isdigit():
        await msg.answer("Usage: /logs <app_id>")
        return
    aid = int(args)
    row = get_app(aid)
    if not row:
        await msg.answer("App not found.")
        return
    uid = msg.from_user.id
    if row[1] != uid and uid not in ADMIN_IDS:
        await msg.answer("You don't have permission to view this app's logs.")
        return
    log_path = LOGS_DIR / f"{row[1]}_app_{aid}.log"
    if not log_path.exists():
        await msg.answer("No logs yet.")
        return
    try:
        txt = log_path.read_text(errors="ignore")
    except Exception as e:
        await msg.answer(f"Error reading log: {e}")
        return
    if len(txt) > APP_LOG_TAIL_CHARS:
        txt = txt[-APP_LOG_TAIL_CHARS:]
        txt = "⤵️ *Last ~{n} chars of log:*\n\n".format(n=APP_LOG_TAIL_CHARS) + "```\n" + txt + "\n```"
        await msg.answer(txt)
    else:
        await msg.answer("```\n" + txt + "\n```")

@dp.message(Command(commands=["stop"]))
async def cmd_stop(msg: types.Message):
    args = msg.get_args().strip()
    if not args.isdigit():
        await msg.answer("Usage: /stop <app_id>")
        return
    aid = int(args)
    app = get_app(aid)
    if not app:
        await msg.answer("App not found.")
        return
    uid = msg.from_user.id
    if app[1] != uid and uid not in ADMIN_IDS:
        await msg.answer("You don't have permission to stop this app.")
        return
    pid = app[6]
    if pid and pid>0:
        stop_process(pid)
        set_app_status(aid, "stopped")
        update_app_pid(aid, 0, "stopped")
        await msg.answer(f"🛑 Stopped app `{aid}`.")
    else:
        set_app_status(aid, "stopped")
        await msg.answer("App is not running.")

# Callback handlers (inline buttons)
@dp.callback_query(lambda c: c.data and c.data.startswith("start:"))
async def cb_start(q: types.CallbackQuery):
    _, aid = q.data.split(":",1)
    aid = int(aid)
    app = get_app(aid)
    if not app:
        await q.answer("App not found.")
        return
    uid = q.from_user.id
    if app[1] != uid and uid not in ADMIN_IDS:
        await q.answer("Not allowed.")
        return
    entry = Path(app[4]) / app[5]
    log_path = LOGS_DIR / f"{app[1]}_app_{aid}.log"
    ok, pid_or_msg = start_process(entry, Path(app[4]), log_path)
    if ok:
        update_app_pid(aid, int(pid_or_msg), "running")
        await q.message.answer(f"🚀 Started app `{app[3]}` (id `{aid}`) — pid `{pid_or_msg}`")
    else:
        set_app_status(aid, "error")
        await q.message.answer(f"❌ Start failed: {pid_or_msg}")
    await q.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("stop:"))
async def cb_stop_cb(q: types.CallbackQuery):
    _, aid = q.data.split(":",1)
    aid = int(aid)
    app = get_app(aid)
    if not app:
        await q.answer("App not found.")
        return
    uid = q.from_user.id
    if app[1] != uid and uid not in ADMIN_IDS:
        await q.answer("Not allowed.")
        return
    pid = app[6]
    if pid and pid>0:
        stop_process(pid)
        set_app_status(aid, "stopped")
        update_app_pid(aid, 0, "stopped")
        await q.message.answer(f"🛑 Stopped app `{app[3]}` (id `{aid}`).")
    else:
        set_app_status(aid, "stopped")
        await q.message.answer("App is not running.")
    await q.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("logs:"))
async def cb_logs_cb(q: types.CallbackQuery):
    _, aid = q.data.split(":",1)
    aid = int(aid)
    app = get_app(aid)
    if not app:
        await q.answer("App not found.")
        return
    uid = q.from_user.id
    if app[1] != uid and uid not in ADMIN_IDS:
        await q.answer("Not allowed.")
        return
    log_path = LOGS_DIR / f"{app[1]}_app_{aid}.log"
    if not log_path.exists():
        await q.message.answer("No logs yet.")
        await q.answer()
        return
    txt = log_path.read_text(errors="ignore")[-APP_LOG_TAIL_CHARS:]
    await q.message.answer("```\n" + txt + "\n```")
    await q.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("info:"))
async def cb_info(q: types.CallbackQuery):
    _, aid = q.data.split(":",1)
    aid = int(aid)
    app = get_app(aid)
    if not app:
        await q.answer("App not found.")
        return
    uid = q.from_user.id
    if app[1] != uid and uid not in ADMIN_IDS:
        await q.answer("Not allowed.")
        return
    pid = app[6]
    status = app[7]
    if pid and pid>0:
        try:
            p = psutil.Process(pid)
            uptime = human_seconds(time.time() - p.create_time())
            cpu = p.cpu_percent(interval=0.1)
            mem_kb = p.memory_info().rss // 1024
            txt = f"*{app[3]}* (id `{aid}`)\nstatus: `{status}`\npid: `{pid}`\nuptime: `{uptime}`\ncpu%: `{cpu}` mem(kB): `{mem_kb}`"
        except Exception:
            txt = f"*{app[3]}* (id `{aid}`)\nstatus: `{status}`\npid: `{pid}`\n(process info unavailable)"
    else:
        txt = f"*{app[3]}* (id `{aid}`)\nstatus: `{status}`\n(pid not running)"
    await q.message.answer(txt)
    await q.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("delete:"))
async def cb_delete(q: types.CallbackQuery):
    _, aid = q.data.split(":",1)
    aid = int(aid)
    app = get_app(aid)
    if not app:
        await q.answer("App not found.")
        return
    uid = q.from_user.id
    if app[1] != uid and uid not in ADMIN_IDS:
        await q.answer("Not allowed.")
        return
    pid = app[6]
    if pid and pid>0:
        stop_process(pid)
    deleted = delete_app(aid)
    if deleted:
        await q.message.answer(f"🗑️ App `{aid}` deleted.")
    else:
        await q.message.answer("❌ Could not delete app (see logs).")
    await q.answer()

@dp.callback_query(lambda c: c.data == "panel_refresh")
async def cb_panel_refresh(q: types.CallbackQuery):
    uid = q.from_user.id
    kb = user_panel_kb(uid)
    try:
        await q.message.edit_text("🧭 Your Panel — refreshed", reply_markup=kb)
    except Exception:
        await q.message.answer("🧭 Your Panel — refreshed", reply_markup=kb)
    await q.answer()

# Admin commands
@dp.message(Command(commands=["admin"]))
async def cmd_admin(msg: types.Message):
    uid = msg.from_user.id
    if uid not in ADMIN_IDS:
        await msg.answer("Unauthorized.")
        return
    rows = list_running_apps()
    total_running = len(rows)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM apps")
    total_apps = cur.fetchone()[0] or 0
    conn.close()
    text = (f"🛡️ *Admin Panel*\nUsers: `{total_users}`\nApps: `{total_apps}`\nRunning: `{total_running}`\n\n"
            "Use inline buttons to control apps.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Stop All Apps", callback_data="admin_stop_all")],
        [InlineKeyboardButton("List Running", callback_data="admin_list_running")]
    ])
    await msg.answer(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data == "admin_stop_all")
async def admin_stop_all(q: types.CallbackQuery):
    uid = q.from_user.id
    if uid not in ADMIN_IDS:
        await q.answer("Not allowed.")
        return
    rows = list_running_apps()
    stopped = 0
    for aid, user_id, name, pid in rows:
        if pid and pid>0:
            stop_process(pid)
            set_app_status(aid, "stopped")
            update_app_pid(aid, 0, "stopped")
            stopped += 1
    await q.message.answer(f"🛑 Stopped {stopped} apps.")
    await q.answer()

@dp.callback_query(lambda c: c.data == "admin_list_running")
async def admin_list_running(q: types.CallbackQuery):
    uid = q.from_user.id
    if uid not in ADMIN_IDS:
        await q.answer("Not allowed.")
        return
    rows = list_running_apps()
    text = "🏃 Running apps:\n"
    for aid, user_id, name, pid in rows:
        text += f"• id {aid} | user {user_id} | {name} | pid {pid}\n"
    if not rows:
        text = "No running apps."
    await q.message.answer(text)
    await q.answer()

# Startup
async def on_startup():
    init_db()
    asyncio.create_task(scan_logs_for_missing_modules(bot))

if __name__ == "__main__":
    print("Starting HostBot v3...")
    try:
        import logging
        logging.basicConfig(level=logging.INFO)
        dp.startup.register(on_startup)
        dp.run_polling(bot)
    except KeyboardInterrupt:
        print("Shutting down...")
        sys.exit(0)
