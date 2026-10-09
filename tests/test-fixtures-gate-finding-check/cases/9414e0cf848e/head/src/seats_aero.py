import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests
import yaml
import logging

logger = logging.getLogger(__name__)

API_BASE = "https://seats.aero/partnerapi"

SEATS_AERO_CREDS_PATHS = [
    Path("credentials/seats_aero.yml"),
    Path("credentials/seats_aero.yaml"),
    Path.home() / ".config" / "award-search" / "seats_aero.yml",
]


def _find_credentials_file() -> Optional[Path]:
    for path in SEATS_AERO_CREDS_PATHS:
        if path.exists():
            return path
    return None


def load_seats_aero_credentials() -> Optional[Dict[str, str]]:
    from .settings import load_settings
    settings = load_settings()
    api_key = settings.get("seats_aero_api_key") or os.environ.get("SEATS_AERO_API_KEY")
    if api_key:
        return {"api_key": api_key}
    return None


CABIN_MAP = {
    "economy": "economy",
    "premium": "premium",
    "business": "business",
    "first": "first",
}

SOURCE_TO_PROGRAM = {
    "eurobonus": "sas",
    "virginatlantic": "virgin_atlantic",
    "aeromexico": "air_canada",
    "american": "american",
    "delta": "delta",
    "etihad": "etihad",
    "united": "united",
    "emirates": "emirates",
    "aeroplan": "air_canada",
    "alaska": "alaska",
    "velocity": "virgin_australia",
    "qantas": "qantas",
    "connectmiles": "copa",
    "azul": "azul",
    "smiles": "gol",
    "flyingblue": "flying_blue",
    "jetblue": "jetblue",
    "qatar": "qatar",
    "turkish": "turkish",
    "singapore": "singapore",
    "ethiopian": "ethiopian",
    "saudia": "saudia",
    "finnair": "finnair",
    "lufthansa": "lufthansa",
    "frontier": "frontier",
    "spirit": "spirit",
}

PROGRAM_TO_SOURCE = {v: k for k, v in SOURCE_TO_PROGRAM.items()}


@dataclass
class SeatsAeroAvailability:
    availability_id: str
    origin: str
    destination: str
    departure_date: date
    source: str
    cabin_avail: Dict[str, bool]
    cabin_cost: Dict[str, int]
    cabin_seats: Dict[str, int]
    cabin_airlines: Dict[str, List[str]]
    cabin_direct: Dict[str, bool]
    last_seen: Optional[str] = None


@dataclass
class SeatsAeroTrip:
    cabin: str
    mileage_cost: int
    alliance_cost: int
    total_taxes: float
    taxes_currency: str
    remaining_seats: int
    stops: int
    total_duration: int
    carriers: List[str]
    flight_numbers: List[str]
    departs_at: str
    arrives_at: str
    segments: List[Dict]
    source: str
    booking_links: List[Dict]
    availability_id: str


class SeatsAeroClient:
    def __init__(self, api_key: Optional[str] = None):
        creds = load_seats_aero_credentials() if not api_key else None
        self.api_key = api_key or (creds.get("api_key") if creds else None)

        if not self.api_key:
            raise ValueError(
                "Seats.aero API key not found. "
                "Set SEATS_AERO_API_KEY env var or create credentials/seats_aero.yml"
            )

        self.session = requests.Session()
        self.session.headers.update({"Partner-Authorization": self.api_key})

    def search(
        self,
        origin: str,
        destination: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        cabins: Optional[List[str]] = None,
        programs: Optional[List[str]] = None,
        only_direct: bool = False,
        include_filtered: bool = False,
    ) -> List[SeatsAeroAvailability]:
        params = {
            "origin_airport": origin,
            "destination_airport": destination,
            "include_filtered": str(include_filtered).lower(),
        }

        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        if cabins:
            params["cabins"] = ",".join(cabins)
        if programs:
            sources = ",".join(p for p in programs if p in PROGRAM_TO_SOURCE)
            if sources:
                params["sources"] = sources
        if only_direct:
            params["only_direct_flights"] = "true"

        try:
            response = self.session.get(f"{API_BASE}/search", params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.exception("Seats.aero search failed")
            return []

        results = []
        for item in data.get("data", []):
            cabins_avail = {}
            cabins_cost = {}
            cabins_seats = {}
            cabins_airlines = {}
            cabins_direct = {}

            for cabin in ["economy", "premium", "business", "first"]:
                key_avail = f"{cabin[0].upper()}Available"
                key_cost = f"{cabin[0].upper()}MileageCost"
                key_seats = f"{cabin[0].upper()}RemainingSeats"
                key_airlines = f"{cabin[0].upper()}Airlines"
                key_direct = f"{cabin[0].upper()}Direct"

                cabins_avail[cabin] = item.get(key_avail, False)
                cost_val = item.get(key_cost)
                cabins_cost[cabin] = int(str(cost_val).replace(",", "")) if cost_val else 0
                cabins_seats[cabin] = item.get(key_seats, 0)
                cabins_airlines[cabin] = item.get(key_airlines, [])
                cabins_direct[cabin] = item.get(key_direct, False)

            results.append(SeatsAeroAvailability(
                availability_id=item.get("id", ""),
                origin=item.get("route", {}).get("originAirport", ""),
                destination=item.get("route", {}).get("destinationAirport", ""),
                departure_date=date.fromisoformat(item["date"]) if "date" in item else start_date,
                source=item.get("source", ""),
                cabin_avail=cabins_avail,
                cabin_cost=cabins_cost,
                cabin_seats=cabins_seats,
                cabin_airlines=cabins_airlines,
                cabin_direct=cabins_direct,
                last_seen=item.get("computedLastSeen"),
            ))

        logger.info(f"Seats.aero returned {len(results)} availability records")
        return results

    def get_trip(self, availability_id: str, include_filtered: bool = False) -> Optional[SeatsAeroTrip]:
        params = {"include_filtered": str(include_filtered).lower()} if include_filtered else {}

        try:
            response = self.session.get(
                f"{API_BASE}/trips/{availability_id}",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException:
            return None

        if not data:
            return None

        trip_data = data[0] if isinstance(data, list) else data

        return SeatsAeroTrip(
            cabin=trip_data.get("cabin", ""),
            mileage_cost=trip_data.get("mileageCost", 0),
            alliance_cost=trip_data.get("allianceCost", 0),
            total_taxes=trip_data.get("totalTaxes", 0) / 100.0,
            taxes_currency=trip_data.get("taxesCurrency", "USD"),
            remaining_seats=trip_data.get("remainingSeats", 0),
            stops=trip_data.get("stops", 0),
            total_duration=trip_data.get("totalDuration", 0),
            carriers=trip_data.get("carriers", []),
            flight_numbers=trip_data.get("flightNumbers", "").split(",") if trip_data.get("flightNumbers") else [],
            departs_at=trip_data.get("departsAt", ""),
            arrives_at=trip_data.get("arrivesAt", ""),
            segments=trip_data.get("availabilitySegments", []),
            source=trip_data.get("source", ""),
            booking_links=trip_data.get("booking_links", []),
            availability_id=availability_id,
        )

    def get_routes(self, program: str) -> List[Dict]:
        source = PROGRAM_TO_SOURCE.get(program)
        if not source:
            return []

        try:
            response = self.session.get(f"{API_BASE}/routes", params={"source": source}, timeout=30)
            response.raise_for_status()
            return response.json().get("data", [])
        except requests.exceptions.RequestException:
            return []

    def bulk_availability(
        self,
        program: str,
        cabin: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        origin_region: Optional[str] = None,
        destination_region: Optional[str] = None,
    ) -> List[SeatsAeroAvailability]:
        source = PROGRAM_TO_SOURCE.get(program)
        if not source:
            return []

        params = {"source": source}
        if cabin:
            params["cabin"] = cabin
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        if origin_region:
            params["origin_region"] = origin_region
        if destination_region:
            params["destination_region"] = destination_region

        try:
            response = self.session.get(f"{API_BASE}/availability", params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException:
            return []

        return self._parse_bulk_results(data)


def _parse_bulk_results(data: Dict) -> List[SeatsAeroAvailability]:
    results = []
    for item in data.get("data", []):
        results.append(SeatsAeroAvailability(
            availability_id=item.get("id", ""),
            origin=item.get("route", {}).get("originAirport", ""),
            destination=item.get("route", {}).get("destinationAirport", ""),
            departure_date=date.fromisoformat(item["date"]) if "date" in item else None,
            source=item.get("source", ""),
            cabin_avail={},
            cabin_cost={},
            cabin_seats={},
            cabin_airlines={},
            cabin_direct={},
            last_seen=item.get("computedLastSeen"),
        ))
    return results