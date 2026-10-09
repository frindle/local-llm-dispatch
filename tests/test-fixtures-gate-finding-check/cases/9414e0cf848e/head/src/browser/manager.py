import asyncio
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class BrowserConfig:
    headless: bool = True
    visible: bool = False
    user_agent: Optional[str] = None
    viewport_width: int = 1920
    viewport_height: int = 1080
    slow_mo: int = 0
    timeout: int = 30000
    credentials_dir: Path = field(default_factory=lambda: Path("credentials"))

    def __post_init__(self):
        if self.visible:
            self.headless = False


class BrowserManager:
    _instance: Optional['BrowserManager'] = None

    def __init__(self, config: Optional[BrowserConfig] = None):
        if BrowserManager._instance is not None:
            return

        self._config = config or BrowserConfig()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        BrowserManager._instance = self

    @classmethod
    def get_instance(cls, config: Optional[BrowserConfig] = None) -> 'BrowserManager':
        if cls._instance is None:
            cls._instance = cls(config)
        return cls._instance

    async def initialize(self):
        if self._playwright is not None:
            return

        from playwright.async_api import async_playwright

        logger.info("Initializing Playwright")
        self._playwright = await async_playwright().start()

        launch_kwargs = {
            "headless": self._config.headless,
            "slow_mo": self._config.slow_mo,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ]
        }

        if self._config.user_agent:
            launch_kwargs["user_agent"] = self._config.user_agent

        logger.info(f"Launching browser (headless={self._config.headless}, visible={self._config.visible})")
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

    async def new_context(self):
        await self.initialize()

        if self._context:
            await self._context.close()

        context_options = {
            "viewport": {"width": self._config.viewport_width, "height": self._config.viewport_height},
            "ignore_https_errors": True,
        }

        if self._config.user_agent:
            context_options["user_agent"] = self._config.user_agent

        logger.debug("Creating new browser context")
        self._context = await self._browser.new_context(**context_options)
        return self._context

    async def new_page(self, context=None):
        if context is None:
            context = await self.new_context()

        self._page = await context.new_page()
        return self._page

    async def close(self):
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser closed")

    def get_page(self):
        return self._page

    @property
    def is_visible_mode(self) -> bool:
        return self._config.visible and not self._config.headless

    @classmethod
    def reset(cls):
        cls._instance = None


@dataclass
class VNCConfig:
    host: str = "localhost"
    port: int = 5900
    novnc_port: int = 6080
    password: Optional[str] = None


def check_vnc_available(config: VNCConfig = None) -> bool:
    import socket

    config = config or VNCConfig()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)

    try:
        result = sock.connect_ex((config.host, config.port))
        return result == 0
    finally:
        sock.close()


async def get_browser_manager(config: Optional[BrowserConfig] = None) -> BrowserManager:
    manager = BrowserManager.get_instance(config)
    await manager.initialize()
    return manager