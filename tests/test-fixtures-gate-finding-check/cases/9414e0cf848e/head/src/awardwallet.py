import os
import requests
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)

import yaml

API_BASE = "https://business.awardwallet.com/api/export/v1"

AWARDWALLET_CREDS_PATHS = [
    Path("credentials/awardwallet.yml"),
    Path("credentials/awardwallet.yaml"),
    Path.home() / ".config" / "award-search" / "awardwallet.yml",
]


def _find_credentials_file() -> Optional[Path]:
    for path in AWARDWALLET_CREDS_PATHS:
        if path.exists():
            return path
    return None


def load_awardwallet_credentials() -> Optional[Dict[str, str]]:
    from .settings import load_settings
    settings = load_settings()
    api_key = settings.get("awardwallet_api_key")
    user_id = settings.get("awardwallet_user_id")
    if api_key and user_id:
        return {"api_key": api_key, "user_id": user_id}

    if os.environ.get("AWARDWALLET_API_KEY") and os.environ.get("AWARDWALLET_USER_ID"):
        return {
            "api_key": os.environ["AWARDWALLET_API_KEY"],
            "user_id": os.environ["AWARDWALLET_USER_ID"],
        }

    creds_file = _find_credentials_file()
    if creds_file:
        with open(creds_file) as f:
            data = yaml.safe_load(f)
        return {"api_key": data.get("api_key"), "user_id": data.get("user_id")}

    return None


@dataclass
class AccountBalance:
    account_id: str
    code: str
    display_name: str
    kind: str
    balance_raw: int
    balance_formatted: str
    status: Optional[str] = None
    account_number: Optional[str] = None
    expiration: Optional[str] = None
    error_code: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict) -> "AccountBalance":
        props = {p["kind"]: p.get("value") for p in data.get("properties", [])}

        return cls(
            account_id=data.get("accountId", ""),
            code=data.get("code", ""),
            display_name=data.get("displayName", ""),
            kind=data.get("kind", ""),
            balance_raw=data.get("balanceRaw", 0),
            balance_formatted=data.get("balance", ""),
            status=props.get(3),
            account_number=props.get(1),
            expiration=props.get(2),
            error_code=data.get("errorCode"),
        )


@dataclass
class BalanceSummary:
    timestamp: str
    accounts: List[AccountBalance] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def airlines(self) -> List[AccountBalance]:
        return [a for a in self.accounts if a.kind == "Airlines"]

    def hotels(self) -> List[AccountBalance]:
        return [a for a in self.accounts if a.kind == "Hotels"]

    def credit_cards(self) -> List[AccountBalance]:
        return [a for a in self.accounts if a.kind == "Credit Cards"]

    def transferable(self) -> List[AccountBalance]:
        return [a for a in self.accounts if a.kind in ("Airlines", "Credit Cards") and a.balance_raw > 0]

    def with_errors(self) -> List[AccountBalance]:
        return [a for a in self.accounts if a.error_code and a.error_code != 1]


class AwardWalletClient:
    def __init__(self, api_key: Optional[str] = None, user_id: Optional[str] = None):
        creds = load_awardwallet_credentials() if not (api_key and user_id) else None

        self.api_key = api_key or creds.get("api_key") if creds else None
        self.user_id = user_id or creds.get("user_id") if creds else None

        if not self.api_key or not self.user_id:
            raise ValueError(
                "AwardWallet credentials not found. "
                "Set AWARDWALLET_API_KEY and AWARDWALLET_USER_ID environment variables, "
                "or create credentials/awardwallet.yml"
            )

        self.session = requests.Session()
        self.session.headers.update({"X-Authentication": self.api_key})

    @classmethod
    def _load_cred(cls) -> Optional[Dict[str, str]]:
        return load_awardwallet_credentials()

    def get_balances(self) -> BalanceSummary:
        url = f"{API_BASE}/connectedUser/{self.user_id}"

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to fetch AwardWallet balances")
            return BalanceSummary(timestamp="", errors=[str(e)])

        accounts = [AccountBalance.from_dict(a) for a in data.get("accounts", [])]
        timestamp = data.get("timestamp", "")

        return BalanceSummary(timestamp=timestamp, accounts=accounts)

    def get_all_providers(self) -> List[Dict]:
        url = f"{API_BASE}/providers/list"

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.json().get("providers", [])
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to fetch providers")
            return []


def load_balances(airlines_only: bool = False) -> Optional[BalanceSummary]:
    try:
        client = AwardWalletClient()
        summary = client.get_balances()

        if summary.errors:
            for err in summary.errors:
                logger.warning(f"AwardWallet error: {err}")
            return None

        logger.info(f"Fetched {len(summary.accounts)} accounts from AwardWallet")
        return summary

    except ValueError as e:
        logger.debug(f"AwardWallet not configured: {e}")
        return None
    except Exception as e:
        logger.exception("Failed to load AwardWallet balances")
        return None