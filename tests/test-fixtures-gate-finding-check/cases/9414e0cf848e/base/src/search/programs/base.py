from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)
from pathlib import Path

import yaml


@dataclass
class SearchQuery:
    origin: str
    destination: str
    departure_date: date
    return_date: Optional[date] = None
    cabin: str = "economy"
    passengers: int = 1
    round_trip: bool = False


@dataclass
class AwardSegment:
    airline: str
    flight_number: str
    departure_airport: str
    arrival_airport: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    stops: int
    cabin: str
    equipment: Optional[str] = None


@dataclass
class AwardPrice:
    program: str
    miles: int
    cabin: str
    taxes: float
    currency: str = "USD"
    additional_fees: float = 0.0


@dataclass
class AwardResult:
    program: str
    segments: List[AwardSegment]
    price: AwardPrice
    availability: str
    fare_class: Optional[str] = None
    currency_pair: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class SearchResponse:
    search_id: str
    timestamp: datetime
    query: SearchQuery
    results: List[AwardResult]
    errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0


class ProgramAdapter(ABC):
    def __init__(self, config: Dict[str, Any], page=None, credentials: Optional[Dict] = None):
        self.config = config
        self.page = page
        self.credentials = credentials or {}
        self.logger = logger

    @property
    @abstractmethod
    def program_id(self) -> str:
        pass

    @property
    @abstractmethod
    def program_name(self) -> str:
        pass

    @abstractmethod
    async def login(self) -> bool:
        pass

    @abstractmethod
    async def search(self, query: SearchQuery) -> List[AwardResult]:
        pass

    @abstractmethod
    async def is_logged_in(self) -> bool:
        pass

    def requires_login(self) -> bool:
        return self.config.get("requires_login", False)

    def rate_limit_seconds(self) -> int:
        return self.config.get("rate_limit_seconds", 5)

    def get_search_url(self) -> str:
        return self.config.get("search_url", "")

    def get_login_url(self) -> str:
        return self.config.get("login_url", "")


class ProgramRegistry:
    _programs: Dict[str, ProgramAdapter] = {}

    @classmethod
    def register(cls, program_id: str, adapter_class: type):
        cls._programs[program_id] = adapter_class

    @classmethod
    def get(cls, program_id: str, config: Dict, page=None, credentials: Optional[Dict] = None) -> ProgramAdapter:
        if program_id not in cls._programs:
            raise ValueError(f"Unknown program: {program_id}. Available: {list(cls._programs.keys())}")

        adapter_class = cls._programs[program_id]
        return adapter_class(config, page=page, credentials=credentials)

    @classmethod
    def available_programs(cls) -> List[str]:
        return list(cls._programs.keys())


def load_programs_config(config_path: Path = None) -> Dict[str, Any]:
    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "programs.yml"

    with open(config_path, "r") as f:
        data = yaml.safe_load(f)

    programs = {p["id"]: p for p in data["programs"]}
    return programs