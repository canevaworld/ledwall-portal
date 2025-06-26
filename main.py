# main.py – LedWall backend | slot booking + admin + e-mail notify

import os, datetime, secrets, smtplib
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import Base, TimeSlot, Video
from storage import new_file_key, presign_put

# ------------------------------------------------------------------#
# DB CONFIG
# ------------------------------------------------------------------#
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

# ------------------------------------------------------------------#
# APP & BASIC-AUTH
# ------------------------------------------------------------------#
app = FastAPI()
security = HTTPBasic()
ADMIN_USER = "admin"
ADMIN_PASS = "Fossalta58@"

def verify_admin(creds: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(creds.username, ADMIN_USER) and
            secrets.compare_digest(creds.password,  ADMIN_PASS)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Invalid credentials",
                            {"WWW-Authenticate": "Basic"})
    return True

# ------------------------------------------------------------------#
# SMTP SETTINGS
# ------------------------------------------------------------------#
SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587
SMTP_USER = "noreply@canevaworld.it"
SMTP_PASS = "Jyb7#NeALsYcWbnf"
SMTP_FROM = SMTP_USER
SMTP_SUBJ = "Stato video LedWall"

def send_mail(to_addr: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = SMTP_SUBJ
    msg["From"]    = SMTP_FROM
    msg["To"]      = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
    except Exception as e:
        print("EMAIL ERROR:", e)

# ------------------------------------------------------------------#
# SLOT HELPERS
# ------------------------------------------------------------------#
def round5(dt: datetime.datetime) -> datetime.datetime:
    disc = datetime.timedelta(minutes=dt.minute % 5,
                              seconds=dt.second,
                              microseconds=dt.microsecond)
    return dt - disc + datetime.timedelta(minutes=5)

def ensure_slots(db, start_dt, end_dt):
    ts = round5(start_dt)
    rows = []
    while ts <= round5(end_dt):
        rows.append({"start_utc": ts})
        ts += datetime.timedelta(minutes=5)
    if rows:
        db.execute(pg_insert(TimeSlot)
                   .values(rows)
                   .on_conflict_do_nothing(index_elements=["start_utc"]))
        db.commit()

# ------------------------------------------------------------------#
# PUBLIC ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/")
def root():
    return {"status": "ok", "message": "LedWall portal online ✔"}

@app.get("/api/slots")
def free_slots(hours_ahead: int = 24):
    now, upper = datetime.datetime.utcnow(), \
                 datetime.datetime.utcnow() + datetime.timedelta(hours=hours_ahead)
    with Session() as db:
        ensure_slots(db, now, upper)
        q = (select(TimeSlot)
             .where(TimeSlot.booked < TimeSlot.capacity,
                    TimeSlot.start_utc >= now,
                    TimeSlot.start_utc <= upper)
             .order_by(TimeSlot.start_utc))
        slots = db.execute(q).scalars().all()
    return [{"id": s.id,
             "start_utc": s.start_utc.isoformat()+"Z",
             "free": s.capacity - s.booked}
            for s in slots]

class InitRequest(BaseModel):
    slot_id: int
    email:   EmailStr
    original_name: str

@app.post("/api/upload_init")
def upload_init(body: InitRequest):
    with Session() as db:
        slot = db.query(TimeSlot).with_for_update().filter_by(id=body.slot_id).first()
        if not slot:                     raise HTTPException(404, "slot not found")
        if slot.booked >= slot.capacity: raise HTTPException(409, "slot full")

        fkey  = new_file_key(body.original_name)
        video = Video(email=body.email,
                      slot_id=slot.id,
                      filename=fkey,
                      status="pending")
        slot.booked += 1
        db.add(video); db.commit(); db.refresh(video)

    return {"video_id":  video.id,
            "upload_url": presign_put(fkey),
            "file_key":   fkey}

# ------------------------------------------------------------------#
# ADMIN ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/api/admin/videos", dependencies=[Depends(verify_admin)])
def list_videos(status: str = "pending", limit: int = 100):
    if status not in {"pending", "approved", "rejected"}:
        raise HTTPException(400, "status must be pending|approved|rejected")
    with Session() as db:
        q = (db.query(Video, TimeSlot)
               .join(TimeSlot, Video.slot_id == TimeSlot.id)
               .filter(Video.status == status)
               .order_by(TimeSlot.start_utc)
               .limit(limit))
        return [{"video_id": v.id,
                 "file_key": v.filename,
                 "status":   v.status,
                 "email":    v.email,
                 "slot_start_utc": s.start_utc.isoformat()+"Z"}
                for v, s in q]

class ValidateBody(BaseModel):
    video_id: int
    action:   str  # approve | reject

@app.post("/api/admin/validate", dependencies=[Depends(verify_admin)])
def validate_video(body: ValidateBody):
    if body.action not in {"approve", "reject"}:
        raise HTTPException(400, "action must be approve|reject")
    with Session() as db:
        video = db.query(Video).filter_by(id=body.video_id).first()
        if not video:                 raise HTTPException(404, "video not found")
        if video.status != "pending": raise HTTPException(409, "already done")

        slot = db.query(TimeSlot).filter_by(id=video.slot_id).first()
        if not slot: raise HTTPException(500, "slot not found")

        if body.action == "approve":
            video.status = "approved"
        else:
            video.status = "rejected"
            if slot.booked > 0: slot.booked -= 1

        db.commit()
        vid, stat, email_addr, slot_dt = video.id, video.status, video.email, slot.start_utc

    send_mail(email_addr,
              f"Il tuo video per le {slot_dt:%d/%m %H:%M} è stato {stat.upper()}!")

    return {"video_id": vid, "status": stat}
