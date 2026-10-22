import asyncio
import hashlib
import hmac
import json
import os
import random
import secrets
import time
import sqlite3
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "coins.db")

START_COINS = 500
ANTE = 20
RESCUE_REWARD = 300
RESCUE_COOLDOWN = 24 * 60 * 60
AUTH_MAX_AGE = 24 * 60 * 60
MAX_WS_MESSAGE = 16 * 1024
RATE_WINDOW = 5
RATE_MAX_MESSAGES = 60

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("""CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY,
    coins INTEGER NOT NULL DEFAULT 500,
    rescue_at INTEGER NOT NULL DEFAULT 0
)""")
db.commit()
db_lock = asyncio.Lock()

async def get_coins(uid):
    async with db_lock:
        row=db.execute("SELECT coins FROM users WHERE id=?",(uid,)).fetchone()
        if row: return int(row["coins"])
        db.execute("INSERT INTO users(id,coins,rescue_at) VALUES(?,?,0)",(uid,START_COINS))
        db.commit()
        return START_COINS

async def change_coins(uid, amount):
    async with db_lock:
        row=db.execute("SELECT coins FROM users WHERE id=?",(uid,)).fetchone()
        if not row:
            current=START_COINS
            db.execute("INSERT INTO users(id,coins,rescue_at) VALUES(?,?,0)",(uid,current))
        else:
            current=int(row["coins"])
        new=current+int(amount)
        if new<0: return False,current
        db.execute("UPDATE users SET coins=? WHERE id=?",(new,uid))
        db.commit()
        return True,new

async def rescue(uid):
    now=int(time.time())
    async with db_lock:
        row=db.execute("SELECT coins,rescue_at FROM users WHERE id=?",(uid,)).fetchone()
        if not row:
            db.execute("INSERT INTO users(id,coins,rescue_at) VALUES(?,?,0)",(uid,START_COINS))
            db.commit()
            return False,START_COINS,"У тебя ещё есть стартовые монеты."
        current=int(row["coins"]); last=int(row["rescue_at"])
        if current>=ANTE:
            return False,current,"Спасение доступно только когда монет меньше 20."
        if now-last<RESCUE_COOLDOWN:
            left=RESCUE_COOLDOWN-(now-last)
            return False,current,f"Спасение уже использовано. Осталось примерно {max(1,left//3600)} ч."
        new=current+RESCUE_REWARD
        db.execute("UPDATE users SET coins=?,rescue_at=? WHERE id=?",(new,now,uid))
        db.commit()
        return True,new,f"+{RESCUE_REWARD} монет."

def auth(init_data):
    if not TOKEN or not init_data: return None,None
    try:
        params=dict(parse_qsl(init_data,keep_blank_values=True))
        received=params.pop("hash","")
        if not received: return None,None
        auth_date=int(params.get("auth_date","0"))
        if auth_date<=0 or time.time()-auth_date>AUTH_MAX_AGE: return None,None
        check="\n".join(f"{k}={v}" for k,v in sorted(params.items()))
        secret=hmac.new(b"WebAppData",TOKEN.encode(),hashlib.sha256).digest()
        calculated=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated,received): return None,None
        user=json.loads(params["user"])
        return str(user["id"]),str(user.get("first_name") or "Игрок")[:64]
    except Exception:
        return None,None

class DurakGame:
    def __init__(self,pids):
        deck=[[s,r] for s in range(4) for r in range(9)]
        random.shuffle(deck)
        self.pids=list(pids); self.deck=deck
        self.trump=deck[0][0]
        self.hand={p:[deck.pop() for _ in range(6)] for p in self.pids}
        self.table=[]; self.cap=0; self.end=None; self.paid=False; self.msg=""
        a,b=self.pids
        def lt(pid):
            vals=[c[1] for c in self.hand[pid] if c[0]==self.trump]
            return min(vals,default=99)
        la,lb=lt(a),lt(b)
        self.attacker=a if la<lb else b if lb<la else random.choice(self.pids)
        self.sort()

    @property
    def defender(self):
        return next(p for p in self.pids if p!=self.attacker)

    def sort(self):
        for h in self.hand.values():
            h.sort(key=lambda c:((c[0]==self.trump)*20+c[1],c[0]))

    def beats(self,d,a):
        return d[1]>a[1] if d[0]==a[0] else d[0]==self.trump

    def open_pair(self):
        return next((p for p in self.table if p[1] is None),None)

    def ranks(self):
        out=set()
        for a,d in self.table:
            if a: out.add(a[1])
            if d: out.add(d[1])
        return out

    def can_play(self,uid,c):
        if self.end: return False
        if uid==self.attacker:
            return not self.table or (len(self.table)<self.cap and c[1] in self.ranks())
        o=self.open_pair()
        return bool(o and self.beats(c,o[0]))

    def play(self,uid,i):
        h=self.hand.get(uid)
        if h is None or not isinstance(i,int) or not 0<=i<len(h): return False
        c=h[i]
        if not self.can_play(uid,c): return False
        h.pop(i)
        if uid==self.attacker:
            if not self.table: self.cap=min(6,len(self.hand[self.defender]))
            self.table.append([c,None]); self.msg="Твоя атака."
        else:
            o=self.open_pair()
            if not o: h.insert(i,c); return False
            o[1]=c; self.msg="Карта бита."
        self.sort(); return True

    def done(self,uid):
        if uid!=self.attacker or not self.table or self.open_pair() or self.end: return False
        self.finish(False); return True

    def take(self,uid):
        if uid!=self.defender or not self.open_pair() or self.end: return False
        self.finish(True); return True

    def finish(self,taken):
        if self.end: return
        if taken:
            self.hand[self.defender].extend(c for p in self.table for c in p if c)
            self.msg="Ты взял карты."
        else:
            self.msg="Бито."
        self.table=[]
        for p in (self.attacker,self.defender):
            while len(self.hand[p])<6 and self.deck: self.hand[p].append(self.deck.pop())
        if not taken: self.attacker=self.defender
        if not self.deck:
            a,b=self.pids; ea,eb=not self.hand[a],not self.hand[b]
            if ea and eb:self.end=("draw",None)
            elif ea:self.end=("win",a)
            elif eb:self.end=("win",b)
        self.sort()

rooms={}

def room_view(room,uid,balance):
    g=room["game"]; opp=[p for p in room["players"] if p!=uid]
    d={"t":"state","code":room["code"],"coins":balance,
       "name":room["names"].get(uid,"Игрок"),
       "opp":room["names"].get(opp[0]) if opp else None,"g":None}
    if g:
        d["g"]={"hand":g.hand[uid],
                 "can":[g.can_play(uid,c) for c in g.hand[uid]],
                 "opp":len(g.hand[opp[0]]) if opp else 0,
                 "table":g.table,"ts":g.trump,"deck":len(g.deck),
                 "att":uid==g.attacker,"msg":g.msg,
                 "end":None if not g.end else ("draw" if g.end[0]=="draw" else "win" if g.end[1]==uid else "lose")}
    return d

async def push(room):
    for uid,ws in list(room["sockets"].items()):
        try: await ws.send_json(room_view(room,uid,await get_coins(uid)))
        except Exception: pass

async def settle(room):
    g=room["game"]
    if not g or not g.end or g.paid:return
    g.paid=True
    if g.end[0]=="draw":
        for p in room["players"]: await change_coins(p,ANTE)
    else:
        await change_coins(g.end[1],2*ANTE)

async def remove_player(room,uid,disconnect=False):
    if uid not in room["players"]: return
    g=room["game"]
    others=[p for p in room["players"] if p!=uid]
    if g and not g.end and len(others)==1:
        g.end=("win",others[0]); await settle(room)
    room["players"]=others
    room["names"].pop(uid,None); room["sockets"].pop(uid,None)
    if not room["players"]: rooms.pop(room["code"],None)
    else: await push(room)

def rate_limited(s):
    now=time.monotonic()
    if now-s["start"]>=RATE_WINDOW:s["start"]=now;s["count"]=0
    s["count"]+=1
    return s["count"]>RATE_MAX_MESSAGES

async def ws_handler(request):
    ws=web.WebSocketResponse(heartbeat=25,max_msg_size=MAX_WS_MESSAGE)
    await ws.prepare(request)
    uid=name=room=None
    rate={"start":time.monotonic(),"count":0}
    try:
        async for msg in ws:
            if msg.type!=aiohttp.WSMsgType.TEXT: continue
            if rate_limited(rate):
                await ws.send_json({"t":"err","m":"Слишком много запросов."}); continue
            try:d=json.loads(msg.data)
            except Exception:continue
            if not isinstance(d,dict):continue
            t=d.get("t")
            if t=="hello":
                uid,name=auth(str(d.get("init","")))
                if not uid:
                    await ws.send_json({"t":"err","m":"Не удалось войти через Telegram."}); continue
                room_code=str(d.get("room","")).strip().upper()
                if room_code:t="join";d["code"]=room_code
                else:
                    await ws.send_json({"t":"state","code":"","coins":await get_coins(uid),"name":name,"opp":None,"g":None});continue
            if not uid:continue

            if t in ("create","join"):
                if room:
                    old=room;room=None;await remove_player(old,uid)
                if t=="create":
                    code=secrets.token_hex(3).upper()[:6]
                    room={"code":code,"players":[uid],"names":{uid:name},"sockets":{uid:ws},"game":None}
                    rooms[code]=room
                else:
                    code=str(d.get("code","")).strip().upper()
                    x=rooms.get(code)
                    if not x:
                        await ws.send_json({"t":"err","m":"Стол не найден."});continue
                    if uid not in x["players"] and len(x["players"])>=2:
                        await ws.send_json({"t":"err","m":"За столом уже двое."});continue
                    if uid not in x["players"]:x["players"].append(uid)
                    x["names"][uid]=name;x["sockets"][uid]=ws;room=x
                await push(room);continue

            if t=="rescue":
                _,bal,m=await rescue(uid);await ws.send_json({"t":"rescue","coins":bal,"m":m})
                if room:await push(room)
                continue

            if t=="leave":
                if room:old=room;room=None;await remove_player(old,uid)
                await ws.send_json({"t":"state","code":"","coins":await get_coins(uid),"name":name,"opp":None,"g":None});continue

            if not room:continue
            g=room["game"]

            if t=="start":
                if len(room["players"])!=2:
                    await ws.send_json({"t":"err","m":"Для игры нужны два игрока."});continue
                if g and not g.end:continue
                if any(await get_coins(p)<ANTE for p in room["players"]):
                    await ws.send_json({"t":"err","m":"У одного из игроков не хватает монет."});continue
                ok=True
                for p in room["players"]:
                    success,_=await change_coins(p,-ANTE)
                    if not success:ok=False
                if not ok:
                    for p in room["players"]:await change_coins(p,ANTE)
                    await ws.send_json({"t":"err","m":"Не удалось списать ставку."});continue
                room["game"]=DurakGame(room["players"])

            elif g and t=="play":
                try:i=int(d.get("i",-1))
                except:i=-1
                g.play(uid,i)
            elif g and t=="done":g.done(uid)
            elif g and t=="take":g.take(uid)

            await settle(room);await push(room)
    finally:
        if room and uid in room["players"] and room["sockets"].get(uid) is ws:
            room["sockets"].pop(uid,None)
            await remove_player(room,uid,disconnect=True)
    return ws

async def index(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__),"index.html"))

async def health(request):
    return web.json_response({"ok":True,"rooms":len(rooms),"time":int(time.time())})

async def bot_task():
    if not TOKEN:return
    api=f"https://api.telegram.org/bot{TOKEN}/"
    offset=0
    timeout=aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        if BASE_URL:
            try:
                await s.post(api+"setChatMenuButton",json={"menu_button":{"type":"web_app","text":"Играть","web_app":{"url":BASE_URL}}})
            except Exception as e: print("Menu button:",e)
        while True:
            try:
                r=await (await s.post(api+"getUpdates",json={"offset":offset,"timeout":30,"allowed_updates":["message"]})).json()
                for u in r.get("result",[]):
                    offset=u["update_id"]+1
                    m=u.get("message")
                    if not m or not BASE_URL:continue
                    await s.post(api+"sendMessage",json={
                        "chat_id":m["chat"]["id"],
                        "text":"🃏 Светлячки и карты\n\nСоздай стол и пригласи друга.",
                        "reply_markup":{"inline_keyboard":[[{"text":"🎮 Играть","web_app":{"url":BASE_URL}}]]}
                    })
            except asyncio.CancelledError:raise
            except Exception as e:
                print("Telegram polling:",e);await asyncio.sleep(3)

async def on_start(app):app["bot"]=asyncio.create_task(bot_task())
async def on_stop(app):
    task=app.get("bot")
    if task:
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass

app=web.Application(client_max_size=MAX_WS_MESSAGE)
app.add_routes([web.get("/",index),web.get("/ws",ws_handler),web.get("/health",health)])
app.on_startup.append(on_start);app.on_cleanup.append(on_stop)

if __name__=="__main__":
    web.run_app(app,host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
