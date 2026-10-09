import json
import os
from pathlib import Path
from typing import Dict, Optional

CREDENTIALS_DIR = Path("credentials")
CREDENTIALS_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = CREDENTIALS_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "seats_aero_api_key": "",
    "awardwallet_api_key": "",
    "awardwallet_user_id": "",
    "serpapi_api_key": "",
    "pushover_app_token": "",
    "pushover_user_key": "",
    "alert_check_interval_hours": "6",
}

ENV_VAR_MAP = {
    "seats_aero_api_key": "SEATS_AERO_API_KEY",
    "awardwallet_api_key": "AWARDWALLET_API_KEY",
    "awardwallet_user_id": "AWARDWALLET_USER_ID",
    "serpapi_api_key": "SERPAPI_API_KEY",
    "pushover_app_token": "PUSHOVER_APP_TOKEN",
    "pushover_user_key": "PUSHOVER_USER_KEY",
    "alert_check_interval_hours": "ALERT_CHECK_INTERVAL_HOURS",
}


def load_settings() -> Dict:
    settings = DEFAULT_SETTINGS.copy()

    for key in settings:
        env_key = ENV_VAR_MAP.get(key, key.upper())
        if os.environ.get(env_key):
            settings[key] = os.environ[env_key]

    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                file_settings = json.load(f)
            for key in settings:
                if file_settings.get(key):
                    settings[key] = file_settings[key]
        except json.JSONDecodeError:
            import warnings
            warnings.warn("Settings file corrupted, ignoring")
        except IOError:
            import warnings
            warnings.warn("Could not read settings file")

    return settings


def save_settings(settings: Dict) -> None:
    save_data = {k: v for k, v in settings.items() if v}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(save_data, f, indent=2)


def get_setting(key: str) -> str:
    return load_settings().get(key, "")


def is_configured(key: str) -> bool:
    return bool(get_setting(key))