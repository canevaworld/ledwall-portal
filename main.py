# main.py – LedWall backend + admin + EMAIL notify

import os, datetime, secrets, smtplib
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
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
# APP & AUTH
# ------------------------------------------------------------------#
app = FastAPI()
security = HTTPBasic()
ADMIN_USER = "admin"
ADMIN_PASS = "Fossalta58@"

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    ok_u = secrets.compare_digest(credentials.username, ADMIN_USER)
    ok_p = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (ok_u and ok_p):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

# ------------------------------------------------------------------#
# EMAIL (SMTP) SETTINGS
# ------------------------------------------------------------------#
SMTP_HOST = "smtp.canevaworld.it"
SMTP_PORT = 587
SMTP_USER = "noreply@canevaworld.it"
SMTP_PASS = "Jyb7#NeALsYcWbnf"
SMTP_FROM = "noreply@canevaworld.it"
SMTP_SUBJ = "Stato video LedWall"

def send_mail(to_addr: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = SMTP_SUBJ
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        # non blocca l’API ma registra l’errore nei log
        print("EMAIL ERROR:", e)

# ------------------------------------------------------------------#
# SLOT HELPERS
# ------------------------------------------------------------------#
def round_to_next_5(dt: datetime.datetime) -> datetime.datetime:
    discard = datetime.timedelta(
        minutes=dt.minute % 5, seconds=dt.second, microseconds=dt.microsecond
    )
    return dt - discard + datetime.timedelta(minutes=5)

def ensure_slots(db, start_dt, end_dt):
    ts = round_to_next_5(start_dt)
    rows = []
    while ts <= round_to_next_5(end_dt):
        rows.append({"start_utc": ts})
        ts += datetime.timedelta(minutes=5)
    if rows:
        stmt = (
            pg_insert(TimeSlot)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["start_utc"])
        )
        db.execute(stmt)
        db.commit()

# ------------------------------------------------------------------#
# PUBLIC ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/")
def read_root():
    return {"status": "ok", "message": "LedWall portal online ✔"}

@app.get("/api/slots")
def get_free_slots(hours_ahead: int = 24):
    now = datetime.datetime.utcnow()
    upper = now + datetime.timedelta(hours=hours_ahead)
    with Session() as db:
        ensure_slots(db, now, upper)
        stmt = (
            select(TimeSlot)
            .where(
                TimeSlot.booked < TimeSlot.capacity,
                TimeSlot.start_utc >= now,
                TimeSlot.start_utc <= upper,
            )
            .order_by(TimeSlot.start_utc)
        )
        slots = db.execute(stmt).scalars().all()
    return [
        {
            "id": s.id,
            "start_utc": s.start_utc.isoformat() + "Z",
            "free": s.capacity - s.booked,
        }
        for s in slots
    ]

class InitRequest(BaseModel):
    slot_id: int
    phone: str          # usato come parte dell’indirizzo e-mail (o email vera)
    original_name: str

@app.post("/api/upload_init")
def upload_init(body: InitRequest):
    with Session() as db:
        slot = db.query(TimeSlot).with_for_update().filter_by(id=body.slot_id).first()
        if not slot:
            raise HTTPException(404, "slot not found")
        if slot.booked >= slot.capacity:
            raise HTTPException(409, "slot full")

        file_key = new_file_key(body.original_name)
        video = Video(phone=body.phone,
                      slot_id=slot.id,
                      filename=file_key,
                      status="pending")
        slot.booked += 1
        db.add(video); db.commit(); db.refresh(video)

        url = presign_put(file_key)
    return {"video_id": video.id, "upload_url": url, "file_key": file_key}

# ------------------------------------------------------------------#
# ADMIN ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/api/admin/videos", dependencies=[Depends(verify_admin)])
def list_videos(status: str = "pending", limit: int = 100):
    if status not in {"pending", "approved", "rejected"}:
        raise HTTPException(400, "status must be pending | approved | rejected")
    with Session() as db:
        q = (
            db.query(Video, TimeSlot)
            .join(TimeSlot, Video.slot_id == TimeSlot.id)
            .filter(Video.status == status)
            .order_by(TimeSlot.start_utc)
            .limit(limit)
        )
        return [
            {
                "video_id": v.id,
                "file_key": v.filename,
                "status": v.status,
                "slot_start_utc": s.start_utc.isoformat() + "Z",
                "phone": v.phone,
            }
            for v, s in q
        ]

class ValidateBody(BaseModel):
    video_id: int
    action: str  # "approve" | "reject"

@app.post("/api/admin/validate", dependencies=[Depends(verify_admin)])
def validate_video(body: ValidateBody):
    if body.action not in {"approve", "reject"}:
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    with Session() as db:
        video = db.query(Video).filter_by(id=body.video_id).first()
        if not video:
            raise HTTPException(404, "video not found")
        if video.status != "pending":
            raise HTTPException(409, "already processed")

        slot = db.query(TimeSlot).filter_by(id=video.slot_id).first()
        if not slot:
            raise HTTPException(500, "slot not found")

        if body.action == "approve":
            video.status = "approved"
        else:
            video.status = "rejected"
            if slot.booked > 0:
                slot.booked -= 1

        db.commit()
        vid, vstat, phone, slot_dt = video.id, video.status, video.phone, slot.start_utc

    # -------- EMAIL NOTIFY --------
    dest_email = f"{phone}@canevaworld.it"  # oppure email reale dell’utente
    send_mail(dest_email,
              f"Il tuo video per le {slot_dt:%d/%m %H:%M} è stato {vstat.upper()}!")

    return {"video_id": vid, "status": vstat}
