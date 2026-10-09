from typing import List, Optional
import logging

logger = logging.getLogger(__name__)

from .base import SearchQuery, AwardResult, AwardSegment, AwardPrice, ProgramAdapter


class DeltaAdapter(ProgramAdapter):
    @property
    def program_id(self) -> str:
        return "delta"

    @property
    def program_name(self) -> str:
        return "Delta SkyMiles"

    async def login(self) -> bool:
        try:
            await self.page.goto(self.get_login_url(), wait_until="networkidle")

            username = self.credentials.get("username")
            password = self.credentials.get("password")

            if not username or not password:
                return False

            await self.page.fill('#username', username)
            await self.page.fill('#password', password)
            await self.page.click('button[type="submit"]')

            await self.page.wait_for_load_state("networkidle", timeout=10000)
            return await self.is_logged_in()

        except Exception:
            return False

    async def is_logged_in(self) -> bool:
        try:
            await self.page.goto("https://www.delta.com/", wait_until="domcontentloaded")
            content = await self.page.content()
            return "sign in" not in content.lower()[-500:]
        except Exception:
            return False

    async def search(self, query: SearchQuery) -> List[AwardResult]:
        try:
            await self.page.goto(self.get_search_url(), wait_until="networkidle")

            await self.page.select_option('#cabinType', query.cabin.capitalize())
            await self.page.fill('#fromAirport', query.origin)
            await self.page.fill('#toAirport', query.destination)
            await self.page.fill('#departureDate', query.departure_date.isoformat())

            if query.return_date:
                await self.page.fill('#returnDate', query.return_date.isoformat())

            await self.page.click('button[type="submit"].search-flight')

            try:
                await self.page.wait_for_selector('.flight-card', timeout=15000)
            except Exception:
                pass

            return await self._parse_results(query)

        except Exception as e:
            self.logger.exception("Search failed")
            return []

    async def _parse_results(self, query: SearchQuery) -> List[AwardResult]:
        results: List[AwardResult] = []

        try:
            cards = await self.page.query_selector_all('.flight-card')

            for card in cards:
                try:
                    flight_el = await card.query_selector('.flight-number')
                    flight = await flight_el.text_content() if flight_el else ""

                    dep_el = await card.query_selector('.departure-time')
                    dep = await dep_el.text_content() if dep_el else ""

                    arr_el = await card.query_selector('.arrival-time')
                    arr = await arr_el.text_content() if arr_el else ""

                    dur_el = await card.query_selector('.duration')
                    dur = await dur_el.text_content() if dur_el else "0h 0m"

                    stops_el = await card.query_selector('.stops')
                    stops_text = await stops_el.text_content() if stops_el else "0 stops"
                    stops = 0 if "nonstop" in stops_text.lower() else int(stops_text.split()[0])

                    miles_el = await card.query_selector('.miles-price')
                    miles_text = await miles_el.text_content() if miles_el else "0"
                    miles = int(''.join(filter(str.isdigit, miles_text)))

                    segment = AwardSegment(
                        airline="Delta",
                        flight_number=flight.strip(),
                        departure_airport=query.origin,
                        arrival_airport=query.destination,
                        departure_time=dep.strip(),
                        arrival_time=arr.strip(),
                        duration_minutes=self._parse_duration(dur),
                        stops=stops,
                        cabin=query.cabin,
                    )

                    price = AwardPrice(
                        program=self.program_id,
                        miles=miles,
                        cabin=query.cabin,
                        taxes=0.0,
                    )

                    results.append(AwardResult(
                        program=self.program_id,
                        segments=[segment],
                        price=price,
                        availability="standard",
                    ))

                except Exception:
                    continue

        except Exception:
            pass

        return results

    def _parse_duration(self, dur: str) -> int:
        import re
        match = re.search(r'(\d+)h\s*(\d+)m', dur)
        if match:
            return int(match.group(1)) * 60 + int(match.group(2))
        return 0


ProgramRegistry.register("delta", DeltaAdapter)