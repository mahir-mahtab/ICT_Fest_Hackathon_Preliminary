# Bug Report

## Datetime and booking windows
- `app/timeutils.py` discarded timezone offsets instead of converting to UTC.
- `app/routers/bookings.py` allowed bookings up to five minutes in the past, missed minimum/negative duration checks, and used inclusive overlap checks that rejected valid back-to-back bookings.
- Fixed by normalizing offset inputs to UTC, enforcing the exact booking-window rules, and using strict interval overlap.

## Authentication and registration
- `app/auth.py` issued 15-hour access tokens instead of 900-second tokens and checked revoked tokens by user id instead of token `jti`.
- `app/routers/auth.py` let refresh tokens be reused and returned an existing user on duplicate registration instead of `409 USERNAME_TAKEN`.
- Fixed token lifetimes, logout revocation, single-use refresh tracking, and duplicate username errors.

## Concurrency and liveness
- Booking creation, cancellation, rate limiting, and reference-code issuance had race windows; artificial sleeps and inverted notification locks could slow or hang valid concurrent requests.
- Reference codes also restarted from the same in-memory counter after app restarts, colliding with persisted SQLite data.
- Fixed with process-local locks around shared state and critical booking operations, unique DB constraints, non-deterministic reference codes, and non-blocking notification stubs.

## Booking reads and cancellation
- `GET /bookings` used descending order, skipped the first page, and ignored the requested limit.
- `GET /bookings/{id}` exposed same-org members' bookings and replaced `start_time` with `created_at`.
- Cancellation used wrong refund tiers, banker's rounding, and separate commits for refund/status updates.
- Fixed pagination, member/admin visibility, response serialization, refund half-up rounding, and atomic cancellation.

## Reports, stats, availability, and export
- Room stats were in-memory counters that could drift from the database and reset on restart.
- Cached usage reports/availability could return stale data after changes.
- Admin export could leak cross-organization bookings when `include_all=true&room_id=...`.
- Fixed stats and report endpoints to derive current scoped DB state and added export room scoping.
