"""Persistent alert store + background checker.

Alerts used to live in an in-process dict — every restart wiped them and
nothing checked them unless the user clicked "Check Now". Now they persist
to data/alerts.json and a background scheduler re-runs each alert every
ALERT_CHECK_INTERVAL_HOURS (default 6), sending a Pushover notification
when *new* availability appears (deduped per alert+date+program+cost so
the same space doesn't re-notify every cycle).
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional

import logging

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
ALERTS_FILE = DATA_DIR / "alerts.json"

_lock = Lock()

DEFAULT_CHECK_INTERVAL_HOURS = 6.0


def load_alerts() -> Dict[str, Dict]:
    if not ALERTS_FILE.exists():
        return {}
    try:
        return json.loads(ALERTS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"alerts.json unreadable ({e}); starting empty")
        return {}


def save_alerts(alerts: Dict[str, Dict]) -> None:
    with _lock:
        tmp = ALERTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(alerts, indent=2))
        tmp.replace(ALERTS_FILE)


def upsert_alert(alert: Dict) -> None:
    alerts = load_alerts()
    alerts[alert["id"]] = alert
    save_alerts(alerts)


def delete_alert(alert_id: str) -> None:
    alerts = load_alerts()
    if alerts.pop(alert_id, None) is not None:
        save_alerts(alerts)


def _result_key(r: Dict) -> str:
    # Identity of a piece of award space for notification dedupe.
    return f'{r.get("date")}|{r.get("source")}|{r.get("cost")}'


def run_alert_check(alert: Dict, search_fn, notify_fn) -> List[Dict]:
    """Run one alert through search_fn; notify_fn per NEW result.

    search_fn(alert) -> list of result dicts (seats.aero shape).
    notify_fn(alert, result) -> None; only called for results not seen
    in a previous check of this alert.
    Mutates + persists the alert record (last_checked/last_results/
    notified_keys).
    """
    results = search_fn(alert)
    already = set(alert.get("notified_keys") or [])
    new = [r for r in results if _result_key(r) not in already]

    alert["last_checked"] = datetime.now().isoformat()
    alert["last_results"] = results

    if alert.get("notify_pushover"):
        for r in new:
            try:
                notify_fn(alert, r)
            except Exception as e:
                logger.warning(f"alert notify failed: {e}")
        already.update(_result_key(r) for r in new)
        # Keep the dedupe set bounded; old dates age out naturally but cap anyway.
        alert["notified_keys"] = sorted(already)[-200:]

    upsert_alert(alert)
    return results


def prune_expired(alerts: Dict[str, Dict]) -> List[str]:
    """Drop alerts whose departure date has passed. Returns pruned ids."""
    today = datetime.now().date().isoformat()
    stale = [aid for aid, a in alerts.items() if (a.get("departure_date") or "9999") < today]
    if stale:
        for aid in stale:
            alerts.pop(aid, None)
        save_alerts(alerts)
        logger.info(f"pruned {len(stale)} expired alert(s): {stale}")
    return stale


async def alert_scheduler(search_fn, notify_fn, interval_hours: Optional[float] = None):
    """Background loop: check every alert on an interval.

    Runs the (synchronous, network-bound) checks in a thread so the event
    loop stays responsive.
    """
    interval = (interval_hours or DEFAULT_CHECK_INTERVAL_HOURS) * 3600
    await asyncio.sleep(30)  # let the app finish booting
    while True:
        try:
            alerts = load_alerts()
            prune_expired(alerts)
            for alert in list(alerts.values()):
                try:
                    await asyncio.to_thread(run_alert_check, alert, search_fn, notify_fn)
                except Exception as e:
                    logger.warning(f'alert {alert.get("id")} check failed: {e}')
            if alerts:
                logger.info(f"alert scheduler: checked {len(alerts)} alert(s)")
        except Exception as e:
            logger.error(f"alert scheduler tick failed: {e}")
        await asyncio.sleep(interval)
