# main.py – LedWall backend / e-mail, auto-release, IP-limit

import os, datetime, secrets, smtplib, ipaddress
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import Base, TimeSlot, Video
from storage import new_file_key, presign_put

# ------------------------------------------------------------------#
# CONFIG
# ------------------------------------------------------------------#
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

app        = FastAPI()
security   = HTTPBasic()
ADMIN_USER = "admin"; ADMIN_PASS = "Fossalta58@"

SMTP_HOST = "smtp.office365.com"; SMTP_PORT = 587
SMTP_USER = SMTP_FROM = "noreply@canevaworld.it"
SMTP_PASS = "Jyb7#NeALsYcWbnf"
SMTP_SUBJ = "LedWall – stato video"

MAX_SLOTS_PER_IP   = 5
UPLOAD_GRACE_MIN   = 5          # minuti per completare l’upload

# ------------------------------------------------------------------#
# UTIL
# ------------------------------------------------------------------#
def send_mail(to_addr: str, body: str):
    msg = EmailMessage(); msg["Subject"]=SMTP_SUBJ; msg["From"]=SMTP_FROM
    msg["To"]=to_addr; msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        print("EMAIL OK ->", to_addr)
    except Exception as e:
        print("EMAIL ERROR:", e)

def verify_admin(creds: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(creds.username, ADMIN_USER) and
            secrets.compare_digest(creds.password , ADMIN_PASS)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Invalid credentials",
                            {"WWW-Authenticate": "Basic"})
    return True

# ------------------------------------------------------------------#
# SLOT HELPERS
# ------------------------------------------------------------------#
def round5(dt):
    tr = datetime.timedelta
    return dt - tr(minutes=dt.minute%5, seconds=dt.second,
                   microseconds=dt.microsecond) + tr(minutes=5)

def ensure_slots(db, start_dt, end_dt):
    ts, rows = round5(start_dt), []
    while ts <= round5(end_dt):
        rows.append({"start_utc": ts}); ts += datetime.timedelta(minutes=5)
    if rows:
        db.execute(pg_insert(TimeSlot)
                   .values(rows)
                   .on_conflict_do_nothing(index_elements=["start_utc"]))
        db.commit()

def auto_release_expired(db):
    """libera slot prenotati ma non caricati entro UPLOAD_GRACE_MIN"""
    limit = datetime.datetime.utcnow() - datetime.timedelta(minutes=UPLOAD_GRACE_MIN)
    stale = (db.query(Video)
               .filter(Video.status == "pending",
                       Video.uploaded == False,
                       Video.created_at <= limit)      # created_at è già nel modello
               .all())
    for v in stale:
        slot = db.query(TimeSlot).filter_by(id=v.slot_id).first()
        if slot and slot.booked > 0: slot.booked -= 1
        db.delete(v)
    if stale: db.commit()

# ------------------------------------------------------------------#
# PUBLIC
# ------------------------------------------------------------------#
@app.get("/")
def root(): return {"status":"ok","message":"LedWall portal online ✔"}

@app.get("/api/slots")
def free_slots(hours_ahead: int = 24):
    now = datetime.datetime.utcnow()
    upper = now + datetime.timedelta(hours=hours_ahead)
    with Session() as db:
        ensure_slots(db, now, upper)
        auto_release_expired(db)
        q = (select(TimeSlot)
             .where(TimeSlot.booked < TimeSlot.capacity,
                    TimeSlot.start_utc >= now,
                    TimeSlot.start_utc <= upper)
             .order_by(TimeSlot.start_utc))
        slots = db.execute(q).scalars().all()
    return [{"id":s.id,"start_utc":s.start_utc.isoformat()+"Z",
             "free":s.capacity-s.booked} for s in slots]

class InitRequest(BaseModel):
    slot_id: int
    email:   EmailStr
    original_name: str

@app.post("/api/upload_init")
def upload_init(body: InitRequest, request: Request):
    client_ip = request.client.host or "0.0.0.0"
    # limite 5 slot per IP
    with Session() as db:
        count = db.query(func.count(Video.id)).filter(
                    Video.client_ip==client_ip,
                    Video.status.in_(("pending","approved"))
                ).scalar()
        if count >= MAX_SLOTS_PER_IP:
            raise HTTPException(429, "troppi slot prenotati da questo IP")

        slot = db.query(TimeSlot).with_for_update().filter_by(id=body.slot_id).first()
        if not slot: raise HTTPException(404,"slot not found")
        if slot.booked >= slot.capacity: raise HTTPException(409,"slot full")

        fkey = new_file_key(body.original_name)
        video = Video(email=body.email, slot_id=slot.id,
                      filename=fkey, status="pending",
                      uploaded=False, client_ip=client_ip)
        slot.booked += 1
        db.add(video); db.commit(); db.refresh(video)

    # restituisce URL PUT ma **niente mail ancora**
    return {"video_id":video.id,"upload_url":presign_put(fkey),"file_key":fkey}

# -------- endpoint chiamato dal client dopo il PUT riuscito -------------
class CompleteBody(BaseModel):
    video_id: int

@app.post("/api/upload_complete")
def upload_complete(body: CompleteBody):
    with Session() as db:
        video = db.query(Video).filter_by(id=body.video_id).first()
        if not video: raise HTTPException(404,"video not found")
        if video.uploaded: return {"msg":"already flagged"}   # idempotente
        video.uploaded = True
        db.commit()
        start_dt = db.query(TimeSlot).filter_by(id=video.slot_id).first().start_utc
        send_mail(video.email,
          f"Abbiamo ricevuto il tuo video.\n"
          f"Sarà revisionato al più presto e ti informeremo dell'esito.\n"
          f"Slot richiesto: {start_dt:%d/%m %H:%M} (UTC).")
    return {"msg":"ok"}

# ------------------------------------------------------------------#
# ADMIN
# ------------------------------------------------------------------#
@app.get("/api/admin/videos", dependencies=[Depends(verify_admin)])
def list_videos(status:str="pending", limit:int=100):
    if status not in {"pending","approved","rejected"}:
        raise HTTPException(400,"status errato")
    with Session() as db:
        q=(db.query(Video,TimeSlot)
             .join(TimeSlot,Video.slot_id==TimeSlot.id)
             .filter(Video.status==status)
             .order_by(TimeSlot.start_utc).limit(limit))
        return [{"video_id":v.id,"file_key":v.filename,"status":v.status,
                 "email":v.email,"slot_start_utc":s.start_utc.isoformat()+"Z"}
                 for v,s in q]

class ValidateBody(BaseModel):
    video_id:int; action:str        # approve|reject

@app.post("/api/admin/validate", dependencies=[Depends(verify_admin)])
def validate_video(body:ValidateBody):
    if body.action not in {"approve","reject"}:
        raise HTTPException(400,"action errata")
    with Session() as db:
        v=db.query(Video).filter_by(id=body.video_id).first()
        if not v: raise HTTPException(404,"video non trovato")
        if v.status!="pending": raise HTTPException(409,"già processato")
        if not v.uploaded: raise HTTPException(409,"file non caricato")

        s=db.query(TimeSlot).filter_by(id=v.slot_id).first()
        if not s: raise HTTPException(500,"slot mancante")

        if body.action=="approve":
            v.status="approved"
            mail_txt=(f"Il tuo video è stato APPROVATO e sarà trasmesso "
                      f"alle {s.start_utc:%H:%M} UTC del {s.start_utc:%d/%m}.")
        else:
            v.status="rejected"
            if s.booked>0: s.booked-=1
            mail_txt=("Siamo spiacenti, il tuo video è stato RIFIUTATO in "
                      "quanto non conforme alle policy.")
        db.commit()

        # prendi i valori PRIMA di chiudere la sessione
        vid        = v.id
        email_addr = v.email
        slot_dt    = s.start_utc
        status_now = v.status
        mail_txt   = (f"Il tuo video è stato APPROVATO e sarà trasmesso "
                      f"alle {slot_dt:%H:%M} UTC del {slot_dt:%d/%m}.") \
                     if status_now == "approved" else \
                     ("Siamo spiacenti, il tuo video è stato RIFIUTATO "
                      "in quanto non conforme alle policy.")
    #  ← siamo fuori dal 'with', usiamo SOLO le variabili scalari
    send_mail(email_addr, mail_txt)
    return {"video_id": vid, "status": status_now}

