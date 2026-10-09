import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

import yaml
import logging

logger = logging.getLogger(__name__)
from tqdm.asyncio import tqdm_asyncio

from .programs.base import (
    SearchQuery, SearchResponse, AwardResult,
    ProgramAdapter, ProgramRegistry, load_programs_config
)
from ..browser.manager import BrowserManager, BrowserConfig


class SearchEngine:
    def __init__(
        self,
        programs_config: Optional[Dict[str, Any]] = None,
        config_path: Optional[Path] = None,
        visible_mode: bool = False,
    ):
        if programs_config is None:
            programs_config = load_programs_config(config_path)

        self.programs_config = programs_config
        self.visible_mode = visible_mode
        self.browser_manager: Optional[BrowserManager] = None

    async def initialize(self):
        browser_config = BrowserConfig(headless=not self.visible_mode, visible=self.visible_mode)
        self.browser_manager = BrowserManager.get_instance(browser_config)
        await self.browser_manager.initialize()

    async def search(
        self,
        query: SearchQuery,
        programs: List[str],
        max_concurrent: int = 3,
        credentials: Optional[Dict[str, Dict]] = None,
    ) -> SearchResponse:
        search_id = str(uuid.uuid4())
        start_time = datetime.now()

        logger.info(f"Starting search {search_id} for {len(programs)} programs")
        logger.debug(f"Query: {query}")

        results: List[AwardResult] = []
        all_errors: List[str] = []

        context = await self.browser_manager.new_context()
        await context.clear_cookies()

        semaphore = asyncio.Semaphore(max_concurrent)

        async def search_program(program_id: str) -> tuple[List[AwardResult], List[str]]:
            program_results = []
            program_errors = []
            async with semaphore:
                try:
                    adapter = ProgramRegistry.get(
                        program_id,
                        self.programs_config[program_id],
                        page=None,
                        credentials=credentials.get(program_id) if credentials else None
                    )

                    page = await context.new_page()
                    adapter.page = page

                    if adapter.requires_login():
                        if not await adapter.is_logged_in():
                            self.logger.info(f"Logging in to {program_id}")
                            if not await adapter.login():
                                program_errors.append(f"Failed to login to {program_id}")
                                await page.close()
                                return [], program_errors

                    await asyncio.sleep(adapter.rate_limit_seconds())
                    program_results = await adapter.search(query)
                    await page.close()

                    return program_results, program_errors

                except Exception as e:
                    logger.exception(f"Error searching {program_id}")
                    program_errors.append(f"{program_id}: {str(e)}")
                    return [], program_errors

        tasks = [search_program(p) for p in programs if p in self.programs_config]

        task_results = await tqdm_asyncio.gather(*tasks, desc="Searching programs")

        for task_result, task_errors in task_results:
            results.extend(task_result)
            all_errors.extend(task_errors)

        duration = (datetime.now() - start_time).total_seconds()

        response = SearchResponse(
            search_id=search_id,
            timestamp=datetime.now(),
            query=query,
            results=results,
            errors=all_errors,
            duration_seconds=duration,
        )

        logger.info(f"Search {search_id} completed in {duration:.1f}s with {len(results)} results")

        return response

    async def close(self):
        if self.browser_manager:
            await self.browser_manager.close()

    @property
    def logger(self):
        return logger


async def run_search(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str] = None,
    cabin: str = "economy",
    programs: Optional[List[str]] = None,
    round_trip: bool = False,
    visible_mode: bool = False,
    config_path: Optional[Path] = None,
    credentials: Optional[Dict[str, Dict]] = None,
) -> SearchResponse:
    from datetime import date as date_type

    query = SearchQuery(
        origin=origin.upper(),
        destination=destination.upper(),
        departure_date=date_type.fromisoformat(departure_date),
        return_date=date_type.fromisoformat(return_date) if return_date else None,
        cabin=cabin.lower(),
        round_trip=round_trip,
    )

    engine = SearchEngine(visible_mode=visible_mode, config_path=config_path)
    await engine.initialize()

    try:
        if programs is None:
            programs = list(engine.programs_config.keys())

        response = await engine.search(query, programs, credentials=credentials)
        return response
    finally:
        await engine.close()


def load_credentials(credentials_dir: Path = None) -> Dict[str, Dict]:
    if credentials_dir is None:
        credentials_dir = Path("credentials")

    credentials = {}

    if not credentials_dir.exists():
        return credentials

    for file in credentials_dir.glob("*.yml"):
        program_id = file.stem
        with open(file, "r") as f:
            data = yaml.safe_load(f)
            credentials[program_id] = data

    return credentials