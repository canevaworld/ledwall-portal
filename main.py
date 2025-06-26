import os, math, datetime
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from models import Base, TimeSlot
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)

# Crea le tabelle se non esistono
Base.metadata.create_all(engine)

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "LedWall portal online ✔"}

def round_to_next_5(dt: datetime.datetime) -> datetime.datetime:
    # Arrotonda (in avanti) al blocco successivo di 5 minuti
    discard = datetime.timedelta(minutes=dt.minute % 5,
                                 seconds=dt.second,
                                 microseconds=dt.microsecond)
    return dt - discard + datetime.timedelta(minutes=5)

@app.get("/api/slots")
def get_free_slots(hours_ahead: int = 24):
    """Restituisce gli slot liberi nelle prossime `hours_ahead` ore (UTC)."""
    now = datetime.datetime.utcnow()
    upper = now + datetime.timedelta(hours=hours_ahead)

    # crea automaticamente gli slot mancanti
    def ensure_slots(db, start_dt, end_dt):
    """
    Inserisce in bulk tutti gli slot 5′ compresi fra start_dt ed end_dt
    evitando i duplicati con ON CONFLICT DO NOTHING.
    """
    ts = round_to_next_5(start_dt)
    rows = []
    while ts <= round_to_next_5(end_dt):
        rows.append({"start_utc": ts})
        ts += datetime.timedelta(minutes=5)

    if rows:
        stmt = pg_insert(TimeSlot).values(rows).on_conflict_do_nothing(
            index_elements=["start_utc"]
        )
        db.execute(stmt)
        db.commit()

@app.get("/api/slots")
def get_free_slots(hours_ahead: int = 24):
    now   = datetime.datetime.utcnow()
    upper = now + datetime.timedelta(hours=hours_ahead)

    with Session() as db:
        ensure_slots(db, now, upper)

        # seleziona solo quelli non pieni
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

        # ora seleziona quelli con posti liberi
        stmt = select(TimeSlot).where(TimeSlot.booked < TimeSlot.capacity,
                                      TimeSlot.start_utc >= now,
                                      TimeSlot.start_utc <= upper).order_by(TimeSlot.start_utc)
        slots = db.execute(stmt).scalars().all()

    return [{"id": s.id,
             "start_utc": s.start_utc.isoformat() + "Z",
             "free": s.capacity - s.booked}
            for s in slots]
