"""Booking creation, listing, detail and cancellation."""
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import cache
from ..auth import get_current_user
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..schemas import BookingCreateRequest
from ..serializers import serialize_booking
from ..services import notifications, ratelimit, reference, stats
from ..services.refunds import log_refund
from ..timeutils import iso_utc, parse_input_datetime

router = APIRouter(tags=["bookings"])
_booking_lock = threading.RLock()

MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 8
QUOTA_LIMIT = 3
QUOTA_WINDOW_HOURS = 24


def _has_conflict(db: Session, room_id: int, start: datetime, end: datetime) -> bool:
    return (
        db.query(Booking)
        .filter(
            Booking.room_id == room_id,
            Booking.status == "confirmed",
            Booking.start_time < end,
            start < Booking.end_time,
        )
        .first()
        is not None
    )


def _check_quota(db: Session, user_id: int, now: datetime, start: datetime) -> None:
    window_end = now + timedelta(hours=QUOTA_WINDOW_HOURS)
    if not (now < start <= window_end):
        return
    count = (
        db.query(Booking)
        .filter(
            Booking.user_id == user_id,
            Booking.status == "confirmed",
            Booking.start_time > now,
            Booking.start_time <= window_end,
        )
        .count()
    )
    if count >= QUOTA_LIMIT:
        raise AppError(409, "QUOTA_EXCEEDED", "Booking quota exceeded")


def _parse_booking_window(payload: BookingCreateRequest) -> tuple[datetime, datetime, int]:
    try:
        start = parse_input_datetime(payload.start_time)
        end = parse_input_datetime(payload.end_time)
    except ValueError:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid datetime")

    now = datetime.utcnow()
    if start <= now:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")

    duration = end - start
    if duration <= timedelta(0):
        raise AppError(400, "INVALID_BOOKING_WINDOW", "end_time must be after start_time")
    if duration % timedelta(hours=1) != timedelta(0):
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration must be a whole number of hours")

    duration_hours = int(duration / timedelta(hours=1))
    if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")

    return start, end, duration_hours


@router.post("/bookings", status_code=201)
def create_booking(
    payload: BookingCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ratelimit.record_and_check(user.id)

    start, end, duration_hours = _parse_booking_window(payload)
    now = datetime.utcnow()

    with _booking_lock:
        room = db.query(Room).filter(Room.id == payload.room_id, Room.org_id == user.org_id).first()
        if room is None:
            raise AppError(404, "ROOM_NOT_FOUND", "Room not found")

        if _has_conflict(db, room.id, start, end):
            raise AppError(409, "ROOM_CONFLICT", "Room already booked for this interval")

        _check_quota(db, user.id, now, start)

        price_cents = room.hourly_rate_cents * duration_hours
        booking = None
        for _ in range(20):
            code = reference.next_reference_code()
            if db.query(Booking).filter(Booking.reference_code == code).first() is not None:
                continue
            booking = Booking(
                room_id=room.id,
                user_id=user.id,
                start_time=start,
                end_time=end,
                status="confirmed",
                reference_code=code,
                price_cents=price_cents,
                created_at=now,
            )
            db.add(booking)
            try:
                db.commit()
                db.refresh(booking)
                break
            except IntegrityError:
                db.rollback()
                booking = None
        if booking is None:
            raise AppError(409, "ROOM_CONFLICT", "Could not create unique booking")

    stats.record_create(room.id, price_cents)
    cache.invalidate_availability(room.id, start.date().isoformat())
    notifications.notify_created(booking)

    return serialize_booking(booking)


@router.get("/bookings")
def list_bookings(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    base = db.query(Booking).filter(Booking.user_id == user.id)
    total = base.count()
    items = (
        base.order_by(Booking.start_time.asc(), Booking.id.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": [serialize_booking(b) for b in items],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/bookings/{booking_id}")
def get_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    response = serialize_booking(booking)
    response["refunds"] = [
        {
            "amount_cents": r.amount_cents,
            "status": r.status,
            "processed_at": iso_utc(r.processed_at),
        }
        for r in booking.refunds
    ]
    return response


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    with _booking_lock:
        booking = (
            db.query(Booking)
            .join(Room, Booking.room_id == Room.id)
            .filter(Booking.id == booking_id, Room.org_id == user.org_id)
            .first()
        )
        if booking is None:
            raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
        if user.role != "admin" and booking.user_id != user.id:
            raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

        if booking.status == "cancelled":
            raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")

        now = datetime.utcnow()
        notice = booking.start_time - now
        if notice >= timedelta(hours=48):
            refund_percent = 100
        elif notice >= timedelta(hours=24):
            refund_percent = 50
        else:
            refund_percent = 0

        refund = log_refund(db, booking, refund_percent)
        refund_amount_cents = refund.amount_cents
        booking.status = "cancelled"
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")
        db.refresh(booking)

    stats.record_cancel(booking.room_id, booking.price_cents)
    cache.invalidate_report(user.org_id)
    cache.invalidate_availability(booking.room_id, booking.start_time.date().isoformat())
    notifications.notify_cancelled(booking)

    return {
        "id": booking.id,
        "status": "cancelled",
        "refund_percent": refund_percent,
        "refund_amount_cents": refund_amount_cents,
    }
