"""AI Hub: Telegram bot + Mini App (liquid glass) + AI'lar boshqaruvi, hammasi bitta faylda.

O'RNATISH
  pip install fastapi uvicorn httpx python-dotenv
ISHGA TUSHIRISH
  uvicorn ai_hub:app --host 0.0.0.0 --port 8000      (HTTPS domen kerak: Caddy / Cloudflare Tunnel)
.env FAYLI (shu fayl yonida)
  BOT_TOKEN=...            # @BotFather'dan
  ADMIN_IDS=111,222        # botga /id yozib o'z ID'ingizni bilib oling
  WEBAPP_URL=https://domeningiz.uz
  # har biriga vergul bilan bir nechta kalit: k1,k2,k3 (limit tugasa keyingisiga o'tadi)
  GEMINI_API_KEYS=  OPENROUTER_API_KEYS=  GROQ_API_KEYS=  CLOUDFLARE_API_KEYS=  CF_ACCOUNT_ID=
  ZAI_API_KEYS=  SAMBANOVA_API_KEYS=  MISTRAL_API_KEYS=
  # ixtiyoriy (pullik): ANTHROPIC_API_KEYS=  OPENAI_API_KEYS=  CEREBRAS_API_KEYS=
  # modelni almashtirish: GEMINI_MODEL, GROQ_MODEL, ZAI_MODEL, SAMBANOVA_MODEL, CLOUDFLARE_MODEL ...
BOT: /start -> Mini App tugmasi, /id -> Telegram ID'ingiz.
"""
import os, hmac, hashlib, json, asyncio, io, re, time, sqlite3, zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()
E = os.getenv
BOT_TOKEN, WEBAPP_URL = E("BOT_TOKEN", ""), E("WEBAPP_URL", "")
ADMINS = {int(x) for x in E("ADMIN_IDS", "").split(",") if x.strip()}
T = 120
SYS = ("Sen tajribali dasturchisan. Vazifa bo'yicha tayyor, ishlaydigan kod yoz. Har bir faylni alohida "
       "```til bloki ichida ber va blokning birinchi qatori albatta `# fayl: yo'l/nom.ext` bo'lsin. Qisqa tushuntirish qo'sh.")

db = sqlite3.connect("data.db", check_same_thread=False)
db.execute("create table if not exists users(id integer primary key, name text)")

class QuotaError(Exception):
    def __init__(self, until): self.until = until

def next_reset(prov):
    tz = ZoneInfo("America/Los_Angeles") if prov == "Gemini" else timezone.utc
    n = datetime.now(tz)
    return (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

def check(r, prov):
    if r.status_code in (429, 402):
        t = r.text.lower().replace(" ", "")
        ra = r.headers.get("retry-after")
        m = re.search(r'retrydelay"?:"?(\d+)', t)
        if ra and ra.isdigit(): until = time.time() + int(ra)
        elif "perday" in t or "daily" in t: until = next_reset(prov)
        elif m: until = time.time() + int(m.group(1))
        else: until = time.time() + 60
        raise QuotaError(until)
    r.raise_for_status()

class Pool:
    def __init__(s, name, env, fn):
        s.name, s.fn, s.cool = name, fn, {}
        s.keys = [k.strip() for k in E(env, "").split(",") if k.strip()]
    def ready(s): return [k for k in s.keys if s.cool.get(k, 0) <= time.time()]
    def reset_in(s): return max(0, int(min(s.cool.get(k, 0) for k in s.keys) - time.time())) if s.keys else 0
    async def call(s, task, system=SYS):
        for k in s.ready():
            try: return await s.fn(k, task, system, s.name)
            except QuotaError as q: s.cool[k] = q.until
        raise QuotaError(min(s.cool.values()))

def oai(url, env, default):
    async def f(key, task, system, name):
        async with httpx.AsyncClient(timeout=T) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {key}"}, json={"model": E(env, default),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": task}]})
            check(r, name)
            return r.json()["choices"][0]["message"]["content"]
    return f

async def gemini(key, task, system, name):
    async with httpx.AsyncClient(timeout=T) as c:
        r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{E('GEMINI_MODEL','gemini-2.5-flash')}:generateContent",
            headers={"x-goog-api-key": key}, json={"systemInstruction": {"parts": [{"text": system}]},
                                                    "contents": [{"parts": [{"text": task}]}]})
        check(r, name)
        return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])

async def claude(key, task, system, name):
    async with httpx.AsyncClient(timeout=T) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": E("CLAUDE_MODEL", "claude-sonnet-4-5"), "max_tokens": 4000, "system": system,
                  "messages": [{"role": "user", "content": task}]})
        check(r, name)
        return "".join(b.get("text", "") for b in r.json()["content"])

pools = [Pool(n, e, f) for n, e, f in [
    ("Gemini", "GEMINI_API_KEYS", gemini), ("Claude", "ANTHROPIC_API_KEYS", claude),
    ("Codex", "OPENAI_API_KEYS", oai("https://api.openai.com/v1/chat/completions", "OPENAI_MODEL", "gpt-4.1")),
    ("Groq", "GROQ_API_KEYS", oai("https://api.groq.com/openai/v1/chat/completions", "GROQ_MODEL", "llama-3.3-70b-versatile")),
    ("OpenRouter", "OPENROUTER_API_KEYS", oai("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324:free")),
    ("Mistral", "MISTRAL_API_KEYS", oai("https://api.mistral.ai/v1/chat/completions", "MISTRAL_MODEL", "mistral-small-latest")),
    ("Z.ai", "ZAI_API_KEYS", oai("https://api.z.ai/api/paas/v4/chat/completions", "ZAI_MODEL", "glm-4.5-flash")),
    ("SambaNova", "SAMBANOVA_API_KEYS", oai("https://api.sambanova.ai/v1/chat/completions", "SAMBANOVA_MODEL", "Meta-Llama-3.3-70B-Instruct")),
    ("Cloudflare", "CLOUDFLARE_API_KEYS", oai(f"https://api.cloudflare.com/client/v4/accounts/{E('CF_ACCOUNT_ID','')}/ai/v1/chat/completions", "CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")),
    ("Cerebras", "CEREBRAS_API_KEYS", oai("https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_MODEL", "gpt-oss-120b")),
]]
pools = [p for p in pools if p.keys]

def human(sec):
    h, m = divmod(sec // 60, 60)
    return f"{h} soat {m} daqiqa" if h else f"{m} daqiqa {sec % 60} soniya" if m else f"{sec} soniya"

def auth(init):
    if not BOT_TOKEN: return 0, "dev", True
    d = dict(parse_qsl(init, keep_blank_values=True)); h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        raise HTTPException(401, "Telegram imzosi noto'g'ri")
    u = json.loads(d["user"]); uid = u["id"]
    adm = uid in ADMINS
    if not adm and not db.execute("select 1 from users where id=?", (uid,)).fetchone():
        raise HTTPException(403, "Sizga ruxsat berilmagan. Admin bilan bog'laning.")
    return uid, u.get("first_name", ""), adm

app = FastAPI()

@app.on_event("startup")
async def menu():
    if BOT_TOKEN and WEBAPP_URL:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton", json={
                "menu_button": {"type": "web_app", "text": "AI Hub", "web_app": {"url": WEBAPP_URL}}})


async def bot_loop():
    if not (BOT_TOKEN and WEBAPP_URL): return
    api, off = f"https://api.telegram.org/bot{BOT_TOKEN}", 0
    async with httpx.AsyncClient(timeout=40) as c:
        while True:
            try:
                for u in (await c.get(api + "/getUpdates", params={"offset": off, "timeout": 25})).json().get("result", []):
                    off = u["update_id"] + 1
                    m = u.get("message") or {}
                    txt, cid = m.get("text", ""), m.get("chat", {}).get("id")
                    if txt.startswith("/start"):
                        await c.post(api + "/sendMessage", json={"chat_id": cid,
                            "text": "AI Hub: vazifani yozing, bir nechta AI bir vaqtda javob beradi.",
                            "reply_markup": {"inline_keyboard": [[{"text": "AI Hub’ni ochish", "web_app": {"url": WEBAPP_URL}}]]}})
                    elif txt.startswith("/id"):
                        await c.post(api + "/sendMessage", json={"chat_id": cid, "text": f"Sizning ID: {m['from']['id']}"})
            except Exception:
                await asyncio.sleep(3)

@app.on_event("startup")
async def start_bot():
    asyncio.create_task(bot_loop())

class Req(BaseModel):
    initData: str = ""
    task: str = ""
    text: str = ""
    answers: dict = {}
    action: str = ""
    id: int = 0
    name: str = ""

@app.get("/")
def index(): return HTMLResponse(HTML)

@app.post("/api/me")
def me(r: Req):
    _, _, adm = auth(r.initData)
    return {"admin": adm, "providers": [p.name for p in pools]}

async def ask(p, task):
    try: return {"ok": True, "text": await p.call(task)}
    except QuotaError as q:
        return {"ok": False, "text": f"Kreditlar tugadi. Tiklanishiga taxminan {human(max(0, int(q.until - time.time())))} qoldi."}
    except Exception as e: return {"ok": False, "text": f"Xatolik: {e}"}

ALIASES = {"gemini": "Gemini", "antigravity": "Gemini", "claude": "Claude", "codex": "Codex", "openai": "Codex",
           "gpt": "Codex", "groq": "Groq", "openrouter": "OpenRouter", "mistral": "Mistral", "cerebras": "Cerebras",
           "zai": "Z.ai", "z.ai": "Z.ai", "glm": "Z.ai", "sambanova": "SambaNova", "cloudflare": "Cloudflare", "grok": "Grok"}
NAME_RE = re.compile(r"(?<![\w.])(" + "|".join(sorted(map(re.escape, ALIASES), key=len, reverse=True)) + r")(?![\w.])\s*[:\-]?\s*", re.I)

def split_tasks(text):
    """None -> hamma AI'ga bir xil vazifa; dict -> {AI: vazifa}"""
    t = text.strip()
    m0 = re.match(r"hamma\s*[:\-]\s*", t, re.I)
    if m0: return None, t[m0.end():]
    ms = NAME_RE.findall(t) and list(NAME_RE.finditer(t))
    if not ms: return None, t
    if ms[0].start() != 0:  # gap o'rtasida: faqat yangi qator yoki ; . ! ? dan keyingi nomlar
        ms = [m for m in ms if re.search(r"(^|[\n;.!?])\s*$", t[:m.start()])]
        if not ms: return None, t
    prefix, out = t[:ms[0].start()].strip(), {}
    for i, m in enumerate(ms):
        seg = t[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(t)].strip()
        task = (prefix + "\n" + seg).strip()
        if task: out[ALIASES[m.group(1).lower()]] = (out.get(ALIASES[m.group(1).lower()], "") + "\n" + task).strip()
    return out or None, t

@app.post("/api/run")
async def run(r: Req):
    auth(r.initData)
    if not pools: raise HTTPException(500, "Hech bir API kalit sozlanmagan")
    per, plain = split_tasks(r.task)
    if per is None:
        out = await asyncio.gather(*[ask(p, plain) for p in pools])
        return {p.name: o for p, o in zip(pools, out)}
    byname, res, jobs = {p.name: p for p in pools}, {}, []
    for n, t in per.items():
        if n in byname: jobs.append((n, ask(byname[n], t)))
        else:
            res[n] = {"ok": False, "text": f"{n} ulanmagan." + (" xAI bepul API bermaydi." if n == "Grok" else " .env da kalit yo'q.")}
    for (n, _), o in zip(jobs, await asyncio.gather(*[j for _, j in jobs])): res[n] = o
    return res

@app.post("/api/compare")
async def compare(r: Req):
    auth(r.initData)
    body = "\n\n".join(f"=== {n} ===\n{t}" for n, t in r.answers.items())
    q = (f"Vazifa: {r.task}\n\nQuyidagi AI javoblarini solishtir: har birining kuchli va zaif tomonlarini qisqa yoz, "
         f"eng yaxshisini tanla, so'ng ularning eng yaxshi jihatlarini birlashtirgan yakuniy kodni ber.\n\n{body}")
    for p in sorted(pools, key=lambda p: len(p.ready()) == 0):
        o = await ask(p, q)
        if o["ok"]: return {"text": f"(Taqqoslovchi: {p.name})\n\n" + o["text"]}
    return {"text": o["text"]}

@app.post("/api/zip")
async def zip_send(r: Req):
    uid, _, _ = auth(r.initData)
    files, n = {}, 0
    for blk in re.findall(r"```[^\n]*\n(.*?)```", r.text, re.S):
        n += 1
        m = re.search(r"fayl:\s*([\w./\-]+)", blk.split("\n", 1)[0])
        body = blk.split("\n", 1)[1] if m and "\n" in blk else blk
        name = (m.group(1) if m else f"file_{n}.txt").lstrip("/").replace("..", "")
        files[name] = body
    if not files: raise HTTPException(400, "Zip uchun kod bloki topilmadi")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items(): z.writestr(k, v)
    async with httpx.AsyncClient() as c:
        await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument", data={"chat_id": uid},
                     files={"document": ("loyiha.zip", buf.getvalue())})
    return {"files": len(files)}

@app.post("/api/admin")
def admin(r: Req):
    _, _, adm = auth(r.initData)
    if not adm: raise HTTPException(403, "Faqat admin")
    if r.action == "add": db.execute("insert or replace into users values(?,?)", (r.id, r.name)); db.commit()
    if r.action == "remove": db.execute("delete from users where id=?", (r.id,)); db.commit()
    return {"users": [{"id": i, "name": n} for i, n in db.execute("select id,name from users")],
            "keys": [{"name": p.name, "total": len(p.keys), "ready": len(p.ready()),
                      "reset": human(p.reset_in()) if not p.ready() else ""} for p in pools]}


HTML = '<!doctype html>\n<html lang="uz"><head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n<title>AI Hub</title>\n<script src="https://telegram.org/js/telegram-web-app.js"></script>\n<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;600;800&display=swap" rel="stylesheet">\n<style>\n:root{--ink:#0c2d52;--mut:#4f6f92;--glass:rgba(255,255,255,.45);--edge:rgba(255,255,255,.9)}\n*{box-sizing:border-box;margin:0}\nhtml,body{height:100%}\nbody{font:16px/1.5 \'Bricolage Grotesque\',system-ui,sans-serif;color:var(--ink);background:#dcefff;overflow:hidden}\n.bg{position:fixed;inset:0;z-index:-1;background:linear-gradient(180deg,#e8f5ff,#cde7fb)}\n.bg i{position:absolute;border-radius:50%;filter:blur(60px);opacity:.75}\n.bg i:nth-child(1){width:60vw;height:60vw;background:#9ccbf5;top:-15vw;left:-15vw}\n.bg i:nth-child(2){width:55vw;height:55vw;background:#c4e3ff;bottom:5vh;right:-20vw}\n.bg i:nth-child(3){width:45vw;height:45vw;background:#b2dafa;bottom:-15vw;left:5vw}\n.glass{background:var(--glass);backdrop-filter:blur(22px) saturate(180%);-webkit-backdrop-filter:blur(22px) saturate(180%);\n border:1px solid var(--edge);border-radius:26px;box-shadow:inset 0 1px 0 rgba(255,255,255,.55),inset 0 -1px 0 rgba(255,255,255,.08),0 12px 40px rgba(40,100,160,.18)}\n#app{height:100%;display:flex;flex-direction:column;gap:12px;padding:calc(env(safe-area-inset-top) + 14px) 14px calc(env(safe-area-inset-bottom) + 14px)}\nh1{font-weight:800;font-size:28px;letter-spacing:-.02em;padding:0 6px}\n.task{padding:12px;display:flex;flex-direction:column;gap:10px}\ntextarea{width:100%;min-height:92px;resize:none;border:0;outline:0;background:transparent;color:var(--ink);font:inherit}\ntextarea::placeholder{color:var(--mut)}\nbutton.go{align-self:flex-end;border:1px solid var(--edge);color:var(--ink);font:600 16px inherit;font-family:inherit;padding:10px 22px;border-radius:999px;\n background:linear-gradient(180deg,rgba(255,255,255,.35),rgba(255,255,255,.1));box-shadow:inset 0 1px 0 rgba(255,255,255,.6);cursor:pointer}\nbutton.go:disabled{opacity:.5}\n#out{flex:1;min-height:0;overflow:auto;padding:18px;display:none}\n#out.on{display:block}\n#out p{margin:0 0 12px;white-space:pre-wrap}\npre{position:relative;background:rgba(255,255,255,.6);border:1px solid rgba(80,140,200,.25);border-radius:16px;padding:14px;margin:0 0 14px;overflow:auto;font:13px/1.5 ui-monospace,monospace}\npre button{position:sticky;float:right;top:0;border:1px solid var(--edge);background:rgba(255,255,255,.75);color:var(--ink);border-radius:10px;padding:3px 10px;font-size:12px}\n.err{color:#c0392b}\n.load{display:flex;gap:6px;justify-content:center;padding:40px}\n.load b{width:10px;height:10px;border-radius:50%;background:#4a90d9;animation:p 1s infinite}\n.load b:nth-child(2){animation-delay:.15s}.load b:nth-child(3){animation-delay:.3s}\n@keyframes p{50%{transform:translateY(-8px);opacity:.4}}\n#tabs{display:none;gap:4px;padding:6px;overflow-x:auto;position:relative}\n#tabs.on{display:flex}\n#tabs button{flex:1 0 auto;border:0;background:transparent;color:var(--mut);font:600 15px inherit;font-family:inherit;padding:11px 16px;border-radius:999px;position:relative;z-index:1;transition:color .2s}\n#tabs button.act{color:var(--ink)}\n#tabs button.act::before{content:"";position:absolute;inset:0;z-index:-1;border-radius:999px;background:linear-gradient(180deg,rgba(255,255,255,.4),rgba(255,255,255,.12));\n border:1px solid var(--edge);box-shadow:inset 0 1px 0 rgba(255,255,255,.7)}\n#tabs button.bad::after{content:"";width:6px;height:6px;border-radius:50%;background:#ff7b7b;display:inline-block;margin-left:6px}\n@media(prefers-reduced-motion:reduce){*{animation:none!important}}\n\n.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}\n.acts button,.adm button{border:1px solid var(--edge);background:rgba(255,255,255,.65);color:var(--ink);font:600 14px inherit;font-family:inherit;padding:8px 16px;border-radius:999px}\n.top{display:flex;justify-content:space-between;align-items:center}\n#gear{display:none}\n#adm{position:fixed;inset:0;z-index:5;display:none;padding:calc(env(safe-area-inset-top) + 14px) 14px 14px}\n#adm.on{display:block}\n.adm{height:100%;overflow:auto;padding:18px}\n.adm h2{font-size:20px;margin:14px 0 8px}\n.row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid rgba(60,120,190,.2)}\n.adm input{background:rgba(255,255,255,.65);border:1px solid var(--edge);border-radius:12px;color:var(--ink);padding:9px 12px;font:inherit;width:100%;margin-bottom:8px}\n</style></head><body>\n<div class="bg"><i></i><i></i><i></i></div>\n<div id="app">\n <div class="top"><h1>AI Hub</h1><button class="go" id="gear" style="padding:8px 16px">Admin</button></div>\n <div class="glass task">\n  <textarea id="t" placeholder="Vazifa yozing. Bitta AI uchun: gemini bot kodi yoz. Alohida: gemini kod yoz groq tushuntirma yoz. Nom yozilmasa hammasi ishlaydi"></textarea>\n  <button class="go" id="go">Yuborish</button>\n </div>\n <div class="glass" id="out"></div>\n <div class="glass" id="tabs"></div>\n<div id="adm"><div class="glass adm" id="admc"></div></div>\n</div>\n<script>\nconst tg=window.Telegram?.WebApp; tg?.ready(); tg?.expand();\nconst $=id=>document.getElementById(id);\nconst esc=s=>String(s).replace(/[&<>]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\'}[c]));\nlet res={},cur=null,task0=\'\';\nasync function api(p,b={}){const r=await fetch(p,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({...b,initData:tg?.initData||\'\'})});const j=await r.json();if(!r.ok)throw new Error(j.detail||r.status);return j}\nfunction render(txt){return txt.split(/```/).map((p,i)=>{\n if(i%2){const nl=p.indexOf(\'\\n\');return `<pre><button onclick="cp(this)">Nusxa</button><code>${esc(p.slice(nl+1))}</code></pre>`}\n return p.trim()?`<p>${esc(p.trim())}</p>`:\'\'}).join(\'\')}\nfunction cp(b){navigator.clipboard.writeText(b.nextElementSibling.textContent);b.textContent=\'Nusxalandi\';setTimeout(()=>b.textContent=\'Nusxa\',1200)}\nfunction tabs(){$(\'tabs\').innerHTML=Object.keys(res).map(n=>`<button data-n="${n}" class="${res[n].ok?\'\':\'bad\'}">${n}</button>`).join(\'\');\n [...$(\'tabs\').children].forEach(b=>b.onclick=()=>show(b.dataset.n));$(\'tabs\').classList.add(\'on\')}\nfunction show(n){cur=n;const r=res[n];\n $(\'out\').innerHTML=(r.ok?render(r.text):`<p class="err">${esc(r.text)}</p>`)+(r.ok?`<div class="acts"><button onclick="zip()">Zip yuborish</button>${n!==\'Taqqoslash\'?\'<button onclick="cmp()">Solishtirish</button>\':\'\'}</div>`:\'\');\n $(\'out\').scrollTop=0;[...$(\'tabs\').children].forEach(b=>b.classList.toggle(\'act\',b.dataset.n===n))}\nasync function zip(){try{const j=await api(\'/api/zip\',{text:res[cur].text});tg?.showAlert(`${j.files} ta fayl zip qilib botga yuborildi`)}catch(e){tg?.showAlert(e.message)}}\nasync function cmp(){$(\'out\').innerHTML=\'<div class="load"><b></b><b></b><b></b></div>\';\n const a={};for(const n in res)if(res[n].ok&&n!==\'Taqqoslash\')a[n]=res[n].text;\n try{const j=await api(\'/api/compare\',{task:task0,answers:a});res.Taqqoslash={ok:true,text:j.text}}catch(e){res.Taqqoslash={ok:false,text:e.message}}\n tabs();show(\'Taqqoslash\')}\n$(\'go\').onclick=async()=>{const t=$(\'t\').value.trim();if(!t)return;task0=t;\n $(\'go\').disabled=true;$(\'tabs\').classList.remove(\'on\');$(\'out\').classList.add(\'on\');$(\'out\').innerHTML=\'<div class="load"><b></b><b></b><b></b></div>\';\n try{res=await api(\'/api/run\',{task:t});tabs();show(Object.keys(res)[0])}catch(e){$(\'out\').innerHTML=`<p class="err">${esc(e.message)}</p>`}\n $(\'go\').disabled=false};\nasync function adm(a={action:\'list\'}){const j=await api(\'/api/admin\',a);\n $(\'admc\').innerHTML=`<div class="top"><h2>Admin panel</h2><button onclick="$(\'adm\').classList.remove(\'on\')">Yopish</button></div>\n <h2>Foydalanuvchilar</h2>${j.users.map(u=>`<div class="row"><span>${esc(u.name)} · ${u.id}</span><button onclick="adm({action:\'remove\',id:${u.id}})">O\'chirish</button></div>`).join(\'\')||\'<p>Hali yo\\\'q</p>\'}\n <h2>Qo\'shish</h2><input id="uid" inputmode="numeric" placeholder="Telegram ID"><input id="un" placeholder="Ism"><button onclick="adm({action:\'add\',id:+uid.value,name:un.value})">Qo\'shish</button>\n <h2>API kalitlar holati</h2>${j.keys.map(k=>`<div class="row"><b>${k.name}</b><span>${k.ready}/${k.total} tayyor${k.reset?` · tiklanish: ${k.reset}`:\'\'}</span></div>`).join(\'\')}`;\n $(\'adm\').classList.add(\'on\')}\n$(\'gear\').onclick=()=>adm();\napi(\'/api/me\').then(j=>{if(j.admin)$(\'gear\').style.display=\'block\'}).catch(e=>{$(\'out\').classList.add(\'on\');$(\'out\').innerHTML=`<p class="err">${esc(e.message)}</p>`;$(\'go\').disabled=true});\n</script></body></html>\n'
