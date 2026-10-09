from typing import List, Optional
from datetime import date
import logging

logger = logging.getLogger(__name__)

from .base import SearchQuery, AwardResult, AwardSegment, AwardPrice, ProgramAdapter


class UnitedAdapter(ProgramAdapter):
    @property
    def program_id(self) -> str:
        return "united"

    @property
    def program_name(self) -> str:
        return "United MileagePlus"

    async def login(self) -> bool:
        try:
            login_url = self.get_login_url()
            await self.page.goto(login_url, wait_until="networkidle")

            username = self.credentials.get("username")
            password = self.credentials.get("password")

            if not username or not password:
                self.logger.warning("No credentials provided for United")
                return False

            await self.page.fill('[data-testid="username"]', username)
            await self.page.fill('[data-testid="password"]', password)
            await self.page.click('[data-testid="submit"]')

            await self.page.wait_for_load_state("networkidle", timeout=10000)
            return await self.is_logged_in()

        except Exception as e:
            self.logger.exception("Login failed")
            return False

    async def is_logged_in(self) -> bool:
        try:
            await self.page.goto("https://www.united.com/en/us/account", wait_until="domcontentloaded")
            content = await self.page.content()
            return "sign in" not in content.lower() or "welcome" in content.lower()
        except Exception:
            return False

    async def search(self, query: SearchQuery) -> List[AwardResult]:
        try:
            search_url = self.get_search_url()
            await self.page.goto(search_url, wait_until="networkidle")

            await self.page.select_option('[data-testid="cabin-select"]', query.cabin.capitalize())
            await self.page.fill('[data-testid="originAirport"]', query.origin)
            await self.page.fill('[data-testid="destinationAirport"]', query.destination)

            departure_str = query.departure_date.isoformat()
            await self.page.fill('[data-testid="departureDate"]', departure_str)

            if query.return_date:
                return_str = query.return_date.isoformat()
                await self.page.fill('[data-testid="returnDate"]', return_str)

            await self.page.click('[data-testid="searchButton"]')
            await self.page.wait_for_load_state("networkidle", timeout=15000)

            return await self._parse_results(query)

        except Exception as e:
            self.logger.exception("Search failed")
            return []

    async def _parse_results(self, query: SearchQuery) -> List[AwardResult]:
        results: List[AwardResult] = []

        try:
            flight_cards = await self.page.query_selector_all('[data-testid="flightCard"]')

            for card in flight_cards:
                try:
                    flight_number = await card.query_selector_eval('[data-testid="flightNumber"]', 'el => el.textContent')
                    departure_time = await card.query_selector_eval('[data-testid="departureTime"]', 'el => el.textContent')
                    arrival_time = await card.query_selector_eval('[data-testid="arrivalTime"]', 'el => el.textContent')
                    duration = await card.query_selector_eval('[data-testid="duration"]', 'el => el.textContent')
                    stops_text = await card.query_selector_eval('[data-testid="stops"]', 'el => el.textContent')

                    miles_element = await card.query_selector('[data-testid="miles"]')
                    miles_text = await miles_element.text_content() if miles_element else "0"
                    miles = int(''.join(filter(str.isdigit, miles_text)))

                    taxes_element = await card.query_selector('[data-testid="taxes"]')
                    taxes_text = await taxes_element.text_content() if taxes_element else "$0"
                    taxes = float(''.join(filter(lambda c: c.isdigit() or c == '.', taxes_text)))

                    stops = 0 if "nonstop" in stops_text.lower() else int(stops_text.split()[0]) if stops_text.split()[0].isdigit() else 1

                    duration_minutes = self._parse_duration(duration)

                    segment = AwardSegment(
                        airline="United",
                        flight_number=flight_number.strip(),
                        departure_airport=query.origin,
                        arrival_airport=query.destination,
                        departure_time=departure_time.strip(),
                        arrival_time=arrival_time.strip(),
                        duration_minutes=duration_minutes,
                        stops=stops,
                        cabin=query.cabin,
                    )

                    price = AwardPrice(
                        program=self.program_id,
                        miles=miles,
                        cabin=query.cabin,
                        taxes=taxes,
                    )

                    result = AwardResult(
                        program=self.program_id,
                        segments=[segment],
                        price=price,
                        availability="standard",
                    )

                    results.append(result)

                except Exception as e:
                    self.logger.warning(f"Failed to parse flight card: {e}")
                    continue

        except Exception as e:
            self.logger.exception("Failed to parse results page")

        return results

    def _parse_duration(self, duration_str: str) -> int:
        import re
        match = re.search(r'(\d+)h\s*(\d+)m', duration_str)
        if match:
            hours, minutes = match.groups()
            return int(hours) * 60 + int(minutes)
        return 0


ProgramRegistry.register("united", UnitedAdapter)