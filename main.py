# main.py  –  LedWall backend
# - solo la giornata “oggi” nei /api/slots pubblici
# - ?days_ahead=N consentito solo all’admin
# - slot fuori fascia 09-18 pre-bloccati (booked = capacity)
# - auto-release, limite 5 slot per IP, e-mail di stato

import os, datetime, secrets, smtplib, ipaddress
from email.message import EmailMessage
from fastapi import FastAPI, HTTPException, Depends, status, Request, Path, Query
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert
from models import Base, TimeSlot, Video
from storage import new_file_key, presign_put
from zoneinfo import ZoneInfo     # <— aggiungi questa riga
TZ_IT = ZoneInfo("Europe/Rome")   #  fuso orario Italia

# ------------------------------------------------------------------#
# CONFIG
# ------------------------------------------------------------------#
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"options": "-c timezone=Europe/Rome"},
)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

app                      = FastAPI()
security                 = HTTPBasic()
ADMIN_USER, ADMIN_PASS   = "admin", "Fossalta58@"

# e-mail
SMTP_HOST, SMTP_PORT     = "smtp.office365.com", 587
SMTP_USER = SMTP_FROM    = "noreply@canevaworld.it"
SMTP_PASS                = "Jyb7#NeALsYcWbnf"
SMTP_SUBJ                = "LedWall – stato video"

# policy
MAX_SLOTS_PER_IP         = 5      # slot “ancora vivi” (pending + approved)
UPLOAD_GRACE_MIN         = 5      # minuti concessi per completare l’upload
BUSINESS_START, BUSINESS_END = 9, 18   # 09:00 → 18:00

# fascia oraria “libera” in ora locale (Europe/Rome)
OPEN_HOUR_LOCAL  = 9      # 09:00
CLOSE_HOUR_LOCAL = 18     # 18:00

# offset attuale fra Roma e UTC (+1 d’inverno, +2 d’estate).
# Se ti va bene ignorare l’ora legale e tenere sempre +1 usa 1,
# altrimenti alza a 2 finché siamo in CEST.
TZ_OFFSET = 2             # cambia manualmente quando passa l’ora legale

# ------------------------------------------------------------------#
# UTIL
# ------------------------------------------------------------------#
def send_mail(to_addr: str, body: str):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = SMTP_SUBJ, SMTP_FROM, to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        print("MAIL OK →", to_addr)
    except Exception as e:
        print("MAIL ERR:", e)


def verify_admin(c: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest
    if not (ok(c.username, ADMIN_USER) and ok(c.password, ADMIN_PASS)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Bad credentials",
                            {"WWW-Authenticate": "Basic"})
    return True

# ------------------------------------------------------------------#
# SLOT helpers
# ------------------------------------------------------------------#
def round5(dt: datetime.datetime) -> datetime.datetime:
    tr = datetime.timedelta
    return dt - tr(minutes=dt.minute % 5,
                   seconds=dt.second,
                   microseconds=dt.microsecond) + tr(minutes=5)

def ensure_slots(db, start_dt, end_dt):
    """
    Crea tutti gli slot 5' fra start_dt e end_dt.

    • Se l’ora (in locale) è fra 09:00 e 17:55  → booked = 0  (libero)
    • Altrimenti                               → booked = capacity (bloccato)
    """
    ts     = round5(start_dt)
    rows   = []
    CAP    = TimeSlot.capacity.property.columns[0].default.arg   # 5 di default
    # calcoliamo la finestra valida in UTC
    open_utc  = (OPEN_HOUR_LOCAL  - TZ_OFFSET) % 24   # 07 se TZ_OFFSET = 2
    close_utc = (CLOSE_HOUR_LOCAL - TZ_OFFSET) % 24   # 16 se TZ_OFFSET = 2

    while ts <= round5(end_dt):
        hr = ts.hour
        is_open = open_utc <= hr < close_utc
        rows.append({
            "start_utc": ts,
            "booked": 0 if is_open else CAP
        })
        ts += datetime.timedelta(minutes=5)

    if rows:
        db.execute(
            pg_insert(TimeSlot)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["start_utc"])
        )
        db.commit()
        
def auto_release_expired(db):
    limit = datetime.datetime.utcnow() - datetime.timedelta(minutes=UPLOAD_GRACE_MIN)
    stale = (db.query(Video)
               .filter(Video.status == "pending",
                       Video.uploaded.is_(False),
                       Video.created_at <= limit)
               .all())
    for v in stale:
        slot = db.query(TimeSlot).filter_by(id=v.slot_id).first()
        if slot and slot.booked > 0:
            slot.booked -= 1
        db.delete(v)
    if stale:
        db.commit()

# ------------------------------------------------------------------#
# PUBLIC ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/")
def root(): return {"status": "ok", "message": "LedWall portal online ✔"}

@app.get("/api/slots")
def free_slots(request: Request,
               days_ahead: int | None = Query(None, ge=0, le=30)):          # max 1 mese
    """
    - pubblico → ignora days_ahead, mostra SOLO gli slot del giorno corrente.
    - admin    → se autenticato può passare days_ahead=N per vedere/creare i
                 prossimi N giorni (0 = oggi).
    """
    is_admin = False
    if "authorization" in request.headers:
        try:
            creds = security(request)
            verify_admin(creds)
            is_admin = True
        except Exception:
            pass

    today_utc = datetime.datetime.utcnow().replace(hour=0, minute=0,
                                                   second=0, microsecond=0)
    if is_admin and days_ahead is not None:
        start, end = today_utc, today_utc + datetime.timedelta(days=days_ahead + 1)
    else:
        start, end = today_utc, today_utc + datetime.timedelta(days=1)

    with Session() as db:
        ensure_slots(db, start, end)
        auto_release_expired(db)
        q = (select(TimeSlot)
             .where(TimeSlot.booked < TimeSlot.capacity,
                    TimeSlot.start_utc >= start,
                    TimeSlot.start_utc <  end)
             .order_by(TimeSlot.start_utc))
        slots = db.execute(q).scalars().all()

out = []
for s in slots:
    local_dt = s.start_utc.astimezone(TZ_IT)             # UTC → ITA
    out.append({
        "id":    s.id,
        "start": local_dt.isoformat(timespec="minutes"), # es: 2025-06-27T09:00+02:00
        "free":  s.capacity - s.booked,
    })
return out

# ------------------------------------------------------------------#
#  UPLOAD FLOW
# ------------------------------------------------------------------#
class InitRequest(BaseModel):
    slot_id: int
    email:   EmailStr
    original_name: str

@app.post("/api/upload_init")
def upload_init(body: InitRequest, request: Request):
    raw_ip = request.client.host or "0.0.0.0"
    try:
        client_ip = str(ipaddress.ip_address(raw_ip))
    except ValueError:
        client_ip = "0.0.0.0"

    with Session() as db:
        # limite IP
        count = (db.query(func.count(Video.id))
                   .filter(Video.client_ip == client_ip,
                           Video.status.in_(("pending", "approved")))
                   .scalar())
        if count >= MAX_SLOTS_PER_IP:
            raise HTTPException(429, "Troppe prenotazioni da questo IP")

        slot = db.query(TimeSlot).with_for_update().filter_by(id=body.slot_id).first()
        if not slot: raise HTTPException(404, "Slot inesistente")
        if slot.booked >= slot.capacity: raise HTTPException(409, "Slot pieno")

        fkey = new_file_key(body.original_name)
        video = Video(email=body.email, slot_id=slot.id, filename=fkey,
                      status="pending", uploaded=False, client_ip=client_ip)
        slot.booked += 1
        db.add(video); db.commit(); db.refresh(video)

    return {"video_id": video.id,
            "upload_url": presign_put(fkey),
            "file_key": fkey}

class CompleteBody(BaseModel):
    video_id: int

@app.post("/api/upload_complete")
def upload_complete(body: CompleteBody):
    with Session() as db:
        v = db.query(Video).filter_by(id=body.video_id).first()
        if not v: raise HTTPException(404,"video not found")
        if v.uploaded: return {"msg":"already flagged"}
        v.uploaded = True; db.commit()
        slot_dt = db.query(TimeSlot).filter_by(id=v.slot_id).first().start_utc
    send_mail(v.email,
              f"Abbiamo ricevuto il tuo video.\n"
              f"Sarà revisionato a breve.\n"
              f"Slot selezionato: {slot_dt:%d/%m %H:%M} UTC.")
    return {"msg":"ok"}

# ------------------------------------------------------------------#
# ADMIN ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/api/admin/videos", dependencies=[Depends(verify_admin)])
def list_videos(status:str="pending", limit:int=100):
    if status not in {"pending","approved","rejected"}:
        raise HTTPException(400,"status errato")
    with Session() as db:
        q=(db.query(Video,TimeSlot)
             .join(TimeSlot,Video.slot_id==TimeSlot.id)
             .filter(Video.status==status)
             .order_by(TimeSlot.start_utc)
             .limit(limit))
        return [{"video_id":v.id,"file_key":v.filename,"status":v.status,
                 "email":v.email,"slot_start_utc":s.start_utc.isoformat()+"Z"}
                for v,s in q]

class ValidateBody(BaseModel):
    video_id:int; action:str   # approve|reject

@app.post("/api/admin/validate", dependencies=[Depends(verify_admin)])
def validate_video(body:ValidateBody):
    if body.action not in {"approve","reject"}:
        raise HTTPException(400,"action errata")

    with Session() as db:
        v = db.query(Video).filter_by(id=body.video_id).first()
        if not v: raise HTTPException(404,"Video mancante")
        if v.status!="pending": raise HTTPException(409,"già processato")
        if not v.uploaded: raise HTTPException(409,"file non caricato")

        s = db.query(TimeSlot).filter_by(id=v.slot_id).first()
        if not s: raise HTTPException(500,"slot mancante")

        if body.action=="approve":
            v.status = "approved"
            slot_it = s.start_utc.astimezone(TZ_IT)
mail_txt = (
    f"Il tuo video è stato APPROVATO e sarà trasmesso "
    f"alle {slot_it:%H:%M} del {slot_it:%d/%m}."
)
        else:
            v.status = "rejected"
            if s.booked>0: s.booked -= 1
            mail_txt = ("Siamo spiacenti: il tuo video è stato RIFIUTATO "
                        "perché non conforme alle policy.")
        db.commit()
        vid, mail_to, status_now = v.id, v.email, v.status

    send_mail(mail_to, mail_txt)
    return {"video_id": vid, "status": status_now}

# --- poco sotto gli altri endpoint admin -----------------------------
class SlotAction(BaseModel):
    slot_id: int
    action:  str  # "block" = forza booked = capacity | "free" = booked = 0

@app.post("/api/admin/slot", dependencies=[Depends(verify_admin)])
def slot_admin(body: SlotAction):
    if body.action not in {"block", "free"}:
        raise HTTPException(400, "action doit être 'block' or 'free'")
    with Session() as db:
        s = db.query(TimeSlot).filter_by(id=body.slot_id).first()
        if not s:
            raise HTTPException(404, "slot non trovato")

        if body.action == "block":
            s.booked = s.capacity          # occupato al 100 %
        else:                              # "free"
            s.booked = 0

        db.commit()
    return {"slot_id": body.slot_id, "status": body.action}

# -------- sblocca manualmente uno slot “chiuso” (booked=capacity) ---
@app.post("/api/admin/slots/{slot_id}/free", dependencies=[Depends(verify_admin)])
def free_slot(slot_id: int = Path(..., ge=1)):
    with Session() as db:
        s = db.query(TimeSlot).filter_by(id=slot_id).first()
        if not s: raise HTTPException(404,"slot not found")
        s.booked = 0; db.commit()
    return {"slot_id": slot_id, "booked": 0}
