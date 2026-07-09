"""Human-facing booking reference codes."""
import uuid


def next_reference_code() -> str:
    return f"CW-{uuid.uuid4().hex[:12].upper()}"
