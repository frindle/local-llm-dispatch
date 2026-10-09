import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests
import logging

logger = logging.getLogger(__name__)

from .settings import load_settings

SERPA_API_BASE = "https://serpapi.com"


CABIN_MAP = {
    "economy": "1",
    "premium": "2",
    "business": "3",
    "first": "4",
}


@dataclass
class FlightOption:
    airline: str
    flight_number: str
    origin: str
    destination: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    stops: int
    price: float
    currency: str
    cabin: str
    booking_link: Optional[str] = None


@dataclass
class PositionSearchResult:
    origin: str
    destination: str
    date: date
    cabin: str
    flights: List[FlightOption] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _parse_duration(dur_str: str) -> int:
    import re
    match = re.search(r'(\d+)h\s*(\d+)m', dur_str)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    return 0


def _stops_from_segments(segments: int) -> int:
    return max(0, segments - 1)


class SerpApiClient:
    def __init__(self, api_key: Optional[str] = None):
        creds = load_settings() if not api_key else None
        self.api_key = api_key or (creds.get("serpapi_api_key") if creds else None)

        if not self.api_key:
            raise ValueError(
                "SerpAPI key not found. "
                "Set SERPAPI_API_KEY env var or add in Settings page."
            )

    def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        cabin: str = "economy",
        nearby_origins: Optional[List[str]] = None,
    ) -> PositionSearchResult:
        if nearby_origins:
            origin_param = ",".join([origin] + nearby_origins)
        else:
            origin_param = origin

        params = {
            "engine": "google_flights",
            "flight_type": "1",  # one-way
            "departure_airports": origin_param,
            "arrival_airports": destination,
            "date": departure_date.strftime("%Y-%m-%d"),
            "cabin": CABIN_MAP.get(cabin.lower(), "1"),
            "api_key": self.api_key,
        }

        result = PositionSearchResult(
            origin=origin,
            destination=destination,
            date=departure_date,
            cabin=cabin,
        )

        try:
            response = requests.get(f"{SERPA_API_BASE}/search", params=params, timeout=60)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.exception("SerpAPI search failed")
            result.errors.append(f"SerpAPI error: {e}")
            return result

        flights_data = data.get("flights", {}).get("best_flights", []) + data.get("flights", {}).get("other_flights", [])

        for flight in flights_data[:20]:
            try:
                segments = flight.get("flights", [])
                if not segments:
                    continue

                first_seg = segments[0]
                last_seg = segments[-1]

                price_str = flight.get("price", "0")
                price = int(price_str.replace(",", "")) if isinstance(price_str, str) else price_str

                dep_time = first_seg.get("departure_time", "")
                arr_time = last_seg.get("arrival_time", "")

                flight_option = FlightOption(
                    airline=first_seg.get("airline", "Unknown"),
                    flight_number=first_seg.get("flight_number", ""),
                    origin=first_seg.get("departure_airport", {}).get("code", origin),
                    destination=last_seg.get("arrival_airport", {}).get("code", destination),
                    departure_time=dep_time,
                    arrival_time=arr_time,
                    duration_minutes=_parse_duration(flight.get("duration", "0h 0m")),
                    stops=_stops_from_segments(len(segments)),
                    price=price,
                    currency=data.get("currency", "USD"),
                    cabin=cabin,
                    booking_link=flight.get("link", ""),
                )
                result.flights.append(flight_option)

            except Exception as e:
                logger.warning(f"Failed to parse flight: {e}")
                continue

        return result


def search_positioning_flights(
    origin: str,
    destination: str,
    departure_date: date,
    cabin: str = "economy",
    nearby_origins: Optional[List[str]] = None,
) -> Optional[PositionSearchResult]:
    try:
        client = SerpApiClient()
        result = client.search_flights(
            origin=origin.upper(),
            destination=destination.upper(),
            departure_date=departure_date,
            cabin=cabin,
            nearby_origins=nearby_origins,
        )
        return result
    except ValueError:
        logger.debug("SerpAPI not configured")
        return None
    except Exception as e:
        logger.exception("Positioning search failed")
        return None


def search_positioning_multi(
    origin: str,
    destination: str,
    departure_date: date,
    cabin: str = "economy",
    nearby_origins: Optional[List[str]] = None,
) -> PositionSearchResult:
    combined = PositionSearchResult(
        origin=origin,
        destination=destination,
        date=departure_date,
        cabin=cabin,
    )

    try:
        client = SerpApiClient()
        result = client.search_flights(
            origin=origin.upper(),
            destination=destination.upper(),
            departure_date=departure_date,
            cabin=cabin,
            nearby_origins=nearby_origins,
        )
        combined.flights.extend(result.flights)
        combined.errors.extend(result.errors)
    except ValueError:
        combined.errors.append("SerpAPI not configured")
    except Exception as e:
        combined.errors.append(str(e))

    combined.flights.sort(key=lambda f: f.price)
    return combined