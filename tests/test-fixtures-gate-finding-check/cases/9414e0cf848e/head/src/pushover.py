import os
from pathlib import Path
from typing import Optional, Dict, Any

import requests
import yaml
import logging

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

PUSHOVER_CREDS_PATHS = [
    Path("credentials/pushover.yml"),
    Path("credentials/pushover.yaml"),
    Path.home() / ".config" / "award-search" / "pushover.yml",
]


def _find_credentials_file() -> Optional[Path]:
    for path in PUSHOVER_CREDS_PATHS:
        if path.exists():
            return path
    return None


def load_pushover_credentials() -> Optional[Dict[str, str]]:
    from .settings import load_settings
    settings = load_settings()
    app_token = settings.get("pushover_app_token")
    user_key = settings.get("pushover_user_key")
    if app_token and user_key:
        return {"app_token": app_token, "user_key": user_key}

    if os.environ.get("PUSHOVER_APP_TOKEN") and os.environ.get("PUSHOVER_USER_KEY"):
        return {
            "app_token": os.environ["PUSHOVER_APP_TOKEN"],
            "user_key": os.environ["PUSHOVER_USER_KEY"],
        }

    creds_file = _find_credentials_file()
    if creds_file:
        with open(creds_file) as f:
            data = yaml.safe_load(f)
        return {"app_token": data.get("app_token"), "user_key": data.get("user_key")}

    return None


class PushoverClient:
    def __init__(self, app_token: Optional[str] = None, user_key: Optional[str] = None):
        creds = load_pushover_credentials() if not (app_token and user_key) else None

        self.app_token = app_token or (creds.get("app_token") if creds else None)
        self.user_key = user_key or (creds.get("user_key") if creds else None)

        if not self.app_token or not self.user_key:
            raise ValueError(
                "Pushover credentials not found. "
                "Set PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY env vars, "
                "or create credentials/pushover.yml"
            )

    def send(
        self,
        message: str,
        title: Optional[str] = None,
        priority: int = 0,
        sound: Optional[str] = None,
        url: Optional[str] = None,
        url_title: Optional[str] = None,
    ) -> bool:
        payload = {
            "token": self.app_token,
            "user": self.user_key,
            "message": message,
            "priority": priority,
        }

        if title:
            payload["title"] = title
        if sound:
            payload["sound"] = sound
        if url:
            payload["url"] = url
        if url_title:
            payload["url_title"] = url_title

        try:
            response = requests.post(PUSHOVER_API_URL, data=payload, timeout=10)
            response.raise_for_status()
            logger.info("Pushover notification sent")
            return True
        except requests.exceptions.RequestException as e:
            logger.warning(f"Pushover notification failed: {e}")
            return False

    def send_award_alert(
        self,
        origin: str,
        destination: str,
        date: str,
        program: str,
        miles: int,
        cabin: str,
        seats: int,
        booking_url: Optional[str] = None,
    ) -> bool:
        title = f"Award Space Found: {origin} → {destination}"

        message = (
            f"{program} | {cabin.title()}\n"
            f"Date: {date} | Miles: {miles:,}\n"
            f"Seats available: {seats}"
        )

        return self.send(
            message=message,
            title=title,
            priority=1,
            sound="alien",
            url=booking_url,
            url_title=f"Book on {program}" if booking_url else None,
        )


def send_award_notification(
    origin: str,
    destination: str,
    date: str,
    program: str,
    miles: int,
    cabin: str,
    seats: int,
    booking_url: Optional[str] = None,
) -> bool:
    try:
        client = PushoverClient()
        return client.send_award_alert(
            origin=origin,
            destination=destination,
            date=date,
            program=program,
            miles=miles,
            cabin=cabin,
            seats=seats,
            booking_url=booking_url,
        )
    except ValueError:
        logger.debug("Pushover not configured, skipping notification")
        return False
    except Exception as e:
        logger.warning(f"Pushover notification error: {e}")
        return False