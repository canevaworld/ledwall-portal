# main.py  – LedWall backend + admin + Telegram notify

import os, datetime, secrets
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

from telegram import Bot

from models import Base, TimeSlot, Video, TgChat
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
# APP, SECURITY, TELEGRAM
# ------------------------------------------------------------------#
app = FastAPI()
security = HTTPBasic()

ADMIN_USER = "admin"
ADMIN_PASS = "Fossalta58@"

TG_BOT_KEY = os.getenv("TG_BOT_KEY")        # <— variabile env su Render
bot = Bot(TG_BOT_KEY) if TG_BOT_KEY else None


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Confronta user/pass con le costanti hard-code."""
    ok_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    ok_pass = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


def notify_user(phone: str, text: str):
    """Invia un messaggio Telegram se abbiamo il chat_id per quel telefono."""
    if not bot:
        return
    with Session() as db:
        rec = db.query(TgChat).filter_by(phone=phone).first()
        if rec:
            try:
                bot.send_message(chat_id=rec.chat_id, text=text)
            except Exception as e:
                print("Telegram send error:", e)


# ------------------------------------------------------------------#
# HELPER FUNZIONI SLOT
# ------------------------------------------------------------------#
def round_to_next_5(dt: datetime.datetime) -> datetime.datetime:
    discard = datetime.timedelta(
        minutes=dt.minute % 5, seconds=dt.second, microseconds=dt.microsecond
    )
    return dt - discard + datetime.timedelta(minutes=5)


def ensure_slots(db, start_dt: datetime.datetime, end_dt: datetime.datetime):
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
    phone: str
    original_name: str


@app.post("/api/upload_init")
def upload_init(body: InitRequest):
    with Session() as db:
        slot = (
            db.query(TimeSlot)
            .with_for_update()
            .filter_by(id=body.slot_id)
            .first()
        )
        if not slot:
            raise HTTPException(404, "slot not found")
        if slot.booked >= slot.capacity:
            raise HTTPException(409, "slot full")

        file_key = new_file_key(body.original_name)
        video = Video(
            phone=body.phone,
            slot_id=slot.id,
            filename=file_key,
            status="pending",
        )
        slot.booked += 1
        db.add(video)
        db.commit()
        db.refresh(video)

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

        # valori prima di chiudere la sessione
        vid = video.id
        vstat = video.status
        phone = video.phone
        slot_start = slot.start_utc

    # --- Telegram notify fuori dalla sessione ---
    if vstat == "approved":
        notify_user(phone, f"✅ Il tuo video per le {slot_start:%d/%m %H:%M} è stato APPROVATO!")
    else:
        notify_user(phone, f"❌ Il tuo video per le {slot_start:%d/%m %H:%M} è stato RIFIUTATO.")

    return {"video_id": vid, "status": vstat}
