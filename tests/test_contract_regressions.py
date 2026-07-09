from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.main import app


client = TestClient(app)


def _future(hours: int, minutes: int = 0) -> str:
    base = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    )
    return (base + timedelta(minutes=minutes)).isoformat()


def _register_login(prefix: str):
    org = f"{prefix}-{datetime.now(timezone.utc).timestamp()}"
    reg = client.post(
        "/auth/register",
        json={"org_name": org, "username": "admin", "password": "pw12345"},
    )
    assert reg.status_code == 201
    login = client.post(
        "/auth/login",
        json={"org_name": org, "username": "admin", "password": "pw12345"},
    )
    assert login.status_code == 200
    return org, {"Authorization": f"Bearer {login.json()['access_token']}"}, login.json()


def _room(headers, rate=1000):
    response = client.post(
        "/rooms",
        json={"name": "Focus", "capacity": 4, "hourly_rate_cents": rate},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["id"]


def _booking(headers, room_id, start, end):
    return client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=headers,
    )


def test_auth_lifetime_logout_refresh_and_duplicate_username():
    org, headers, tokens = _register_login("auth")
    claims = jwt.decode(tokens["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert claims["exp"] - claims["iat"] == 900

    duplicate = client.post(
        "/auth/register",
        json={"org_name": org, "username": "admin", "password": "pw12345"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USERNAME_TAKEN"

    refresh = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 200
    reused = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401

    logout = client.post("/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert client.get("/rooms", headers=headers).status_code == 401


def test_booking_window_overlap_pagination_and_detail_visibility():
    org, headers, _ = _register_login("booking")
    room_id = _room(headers)

    first = _booking(headers, room_id, _future(30), _future(31))
    assert first.status_code == 201
    second = _booking(headers, room_id, _future(31), _future(32))
    assert second.status_code == 201
    overlap = _booking(headers, room_id, _future(30), _future(32))
    assert overlap.status_code == 409
    assert overlap.json()["code"] == "ROOM_CONFLICT"

    past = _booking(headers, room_id, _future(-1), _future(1))
    assert past.status_code == 400
    short = _booking(headers, room_id, _future(40), _future(40, 30))
    assert short.status_code == 400

    listing = client.get("/bookings?page=1&limit=1", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["limit"] == 1
    assert len(listing.json()["items"]) == 1
    assert listing.json()["items"][0]["id"] == first.json()["id"]

    detail = client.get(f"/bookings/{first.json()['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["start_time"] == first.json()["start_time"]

    client.post(
        "/auth/register",
        json={"org_name": org, "username": "member", "password": "pw12345"},
    )
    member_login = client.post(
        "/auth/login",
        json={"org_name": org, "username": "member", "password": "pw12345"},
    )
    member_headers = {"Authorization": f"Bearer {member_login.json()['access_token']}"}
    hidden = client.get(f"/bookings/{first.json()['id']}", headers=member_headers)
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "BOOKING_NOT_FOUND"


def test_timezone_stats_reports_availability_and_refund_rounding():
    _, headers, _ = _register_login("report")
    room_id = _room(headers, rate=1001)

    start = (datetime.now(timezone.utc) + timedelta(hours=50)).replace(
        minute=0, second=0, microsecond=0
    )
    offset_start = start.astimezone(timezone(timedelta(hours=6)))
    offset_end = (start + timedelta(hours=1)).astimezone(timezone(timedelta(hours=6)))
    booking = _booking(headers, room_id, offset_start.isoformat(), offset_end.isoformat())
    assert booking.status_code == 201
    assert booking.json()["start_time"].startswith(start.isoformat().replace("+00:00", ""))

    stats = client.get(f"/rooms/{room_id}/stats", headers=headers)
    assert stats.json()["total_confirmed_bookings"] == 1
    assert stats.json()["total_revenue_cents"] == 1001

    day = start.date().isoformat()
    availability = client.get(f"/rooms/{room_id}/availability?date={day}", headers=headers)
    assert availability.status_code == 200
    assert len(availability.json()["busy"]) == 1

    report = client.get(f"/admin/usage-report?from={day}&to={day}", headers=headers)
    assert report.status_code == 200
    assert report.json()["rooms"][0]["confirmed_bookings"] == 1
    assert report.json()["rooms"][0]["revenue_cents"] == 1001

    cancelled = client.post(f"/bookings/{booking.json()['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["refund_percent"] == 100
    assert cancelled.json()["refund_amount_cents"] == 1001

    assert client.get(f"/rooms/{room_id}/stats", headers=headers).json()["total_confirmed_bookings"] == 0
    refreshed_report = client.get(f"/admin/usage-report?from={day}&to={day}", headers=headers)
    assert refreshed_report.json()["rooms"][0]["confirmed_bookings"] == 0


def test_quota_rate_limit_and_export_scoping():
    _, headers, _ = _register_login("limits")
    room_id = _room(headers)

    for hour in (1, 3, 5):
        response = _booking(headers, room_id, _future(hour), _future(hour + 1))
        assert response.status_code == 201
    quota = _booking(headers, room_id, _future(7), _future(8))
    assert quota.status_code == 409
    assert quota.json()["code"] == "QUOTA_EXCEEDED"

    _, other_headers, _ = _register_login("other")
    other_room = _room(other_headers)
    export = client.get(f"/admin/export?room_id={other_room}&include_all=true", headers=headers)
    assert export.status_code == 404
    assert export.json()["code"] == "ROOM_NOT_FOUND"

    _, rate_headers, _ = _register_login("rate")
    rate_room = _room(rate_headers)
    statuses = [
        _booking(rate_headers, rate_room, _future(80), _future(81)).status_code
        for _ in range(21)
    ]
    assert statuses[-1] == 429
