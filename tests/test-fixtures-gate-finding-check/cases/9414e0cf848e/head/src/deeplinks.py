"""Deep links to external award-search services.

None of these expose a public API (verified July 2026 — the seats.aero
partner API is flights-only; point.me's API is enterprise/partner-only;
roame.travel has none), so the best we can do is hand the user a
prefilled search on their site. URL patterns are best-effort observations
and centralized here so they're one-line fixes when a site changes.
"""
from urllib.parse import quote


def pointme_url(origin: str, destination: str, departure_date: str, cabin: str = "economy") -> str:
    # point.me requires a signed-in session and builds search ids server-side;
    # no known prefill params. Land the user on their search page.
    return "https://point.me/en/search"


def roame_url(origin: str, destination: str, departure_date: str, cabin: str = "economy") -> str:
    # Roame SkyView search. Params observed from their SPA; if the pattern
    # drifts, the worst case is their search page ignores the params.
    cabin_map = {"economy": "Y", "premium_economy": "W", "business": "J", "first": "F"}
    c = cabin_map.get(cabin.lower(), "Y")
    return (
        "https://roame.travel/search?"
        f"origin={quote(origin.upper())}&destination={quote(destination.upper())}"
        f"&date={quote(departure_date)}&cabin={c}"
    )


def seats_aero_url(origin: str, destination: str, departure_date: str, cabin: str = "economy") -> str:
    return (
        "https://seats.aero/search?"
        f"origins={quote(origin.upper())}&destinations={quote(destination.upper())}"
        f"&start_date={quote(departure_date)}&end_date={quote(departure_date)}"
    )


def rooms_aero_url(city: str = "", check_in: str = "", check_out: str = "") -> str:
    # rooms.aero (hotel awards, same team as seats.aero) has no public API
    # and no documented prefill params yet.
    return "https://rooms.aero/"


def flight_links(origin: str, destination: str, departure_date: str, cabin: str = "economy") -> list[dict]:
    """All external search links for a flight query, for templates/pushes."""
    return [
        {"name": "seats.aero", "url": seats_aero_url(origin, destination, departure_date, cabin)},
        {"name": "point.me", "url": pointme_url(origin, destination, departure_date, cabin)},
        {"name": "roame.travel", "url": roame_url(origin, destination, departure_date, cabin)},
        {"name": "rooms.aero (hotels)", "url": rooms_aero_url()},
    ]
