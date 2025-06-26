import os, datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import Base, TimeSlot, Video
from storage import new_file_key, presign_put

# --------------------------- DB -----------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

# ------------------------- FastAPI --------------------------------
app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "LedWall portal online ✔"}

# --------------------- helper slot logic --------------------------
def round_to_next_5(dt: datetime.datetime) -> datetime.datetime:
    discard = datetime.timedelta(
        minutes=dt.minute % 5,
        seconds=dt.second,
        microseconds=dt.microsecond,
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

# ------------------------- endpoints -------------------------------
@app.get("/api/slots")
def get_free_slots(hours_ahead: int = 24):
    now   = datetime.datetime.utcnow()
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
        {"id": s.id,
         "start_utc": s.start_utc.isoformat() + "Z",
         "free": s.capacity - s.booked}
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

    return {
        "video_id": video.id,
        "upload_url": url,
        "file_key": file_key,
    }
