import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import click
import yaml
import logging

logger = logging.getLogger(__name__)

from .search import SearchEngine, load_programs_config, load_credentials
from .search.programs.base import SearchQuery
from .awardwallet import load_balances, BalanceSummary, AwardWalletClient


def setup_logging(verbose: bool = False):
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s - %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def cli(verbose):
    """Award Search CLI - Search award space across multiple loyalty programs"""
    setup_logging(verbose)


@cli.command()
def list_programs():
    """List all available loyalty programs"""
    programs = load_programs_config()

    click.echo("\nAvailable Programs:")
    click.echo("-" * 50)

    for program_id, config in programs.items():
        alliance = config.get("alliance", "none")
        requires_login = config.get("requires_login", False)
        login_marker = " [requires login]" if requires_login else ""
        click.echo(f"  {program_id:20s} - {config['name']} ({alliance}){login_marker}")

    click.echo()


@cli.command()
@click.option("--user-id", help="AwardWallet user ID to query (defaults to configured user)")
@click.option("--airlines-only", is_flag=True, help="Show airline balances only")
@click.option("--json-output", is_flag=True, help="Output as JSON")
def balances(user_id: Optional[str], airlines_only: bool, json_output: bool):
    """Fetch current loyalty program balances from AwardWallet"""
    creds = AwardWalletClient._load_cred()
    configured_user = creds.get("user_id") if creds else None

    api_key = os.environ.get("AWARDWALLET_API_KEY") or (creds.get("api_key") if creds else None)
    effective_user_id = user_id or configured_user

    if not api_key or not effective_user_id:
        click.echo("Error: AwardWallet credentials not configured.", err=True)
        click.echo("Set AWARDWALLET_API_KEY and AWARDWALLET_USER_ID env vars, or create credentials/awardwallet.yml", err=True)
        click.echo("See: https://business.awardwallet.com/profile/api", err=True)
        return

    try:
        client = AwardWalletClient(api_key, effective_user_id)
        summary = client.get_balances()
    except Exception as e:
        click.echo(f"Error fetching balances: {e}", err=True)
        return

    if json_output:
        data = {
            "timestamp": summary.timestamp,
            "user_id": effective_user_id,
            "accounts": [
                {
                    "code": a.code,
                    "name": a.display_name,
                    "kind": a.kind,
                    "balance": a.balance_raw,
                    "balance_formatted": a.balance_formatted,
                    "status": a.status,
                    "account_number": a.account_number,
                }
                for a in summary.accounts
            ]
        }
        click.echo(json.dumps(data, indent=2))
        return

    accounts = summary.airlines() if airlines_only else summary.accounts

    click.echo(f"\n{'='*60}")
    click.echo(f"AwardWallet Balances (user: {effective_user_id[:8]}..., as of {summary.timestamp})")
    click.echo(f"{'='*60}")

    if not accounts:
        click.echo("No accounts found.")
        return

    by_kind: Dict[str, List] = {}
    for account in accounts:
        by_kind.setdefault(account.kind, []).append(account)

    for kind, accs in by_kind.items():
        click.echo(f"\n{kind}:")
        for a in accs:
            status_str = f" | Status: {a.status}" if a.status else ""
            click.echo(f"  {a.display_name:40s} {a.balance_formatted:>15s}{status_str}")

    errors = summary.with_errors()
    if errors:
        click.echo(f"\nAccounts needing attention ({len(errors)}):")
        for a in errors:
            click.echo(f"  {a.display_name}: error code {a.error_code}")


@cli.command()
@click.option("--origin", "-o", required=True, help="Origin airport code (e.g., JFK)")
@click.option("--destination", "-d", required=True, help="Destination airport code (e.g., LAX)")
@click.option("--date", required=True, help="Departure date (YYYY-MM-DD)")
@click.option("--return", "return_date", help="Return date (YYYY-MM-DD) for round trip")
@click.option("--cabin", "-c", default="economy", type=click.Choice(["economy", "business", "first"]), help="Cabin class")
@click.option("--programs", "-p", help="Comma-separated program IDs (default: all)")
@click.option("--round-trip", is_flag=True, help="Search round trip")
@click.option("--visible", is_flag=True, help="Launch visible browser for login")
@click.option("--config", type=click.Path(exists=True, path_type=Path), help="Custom programs config file")
@click.option("--credentials-dir", type=click.Path(exists=True, path_type=Path), help="Directory with credentials")
@click.option("--output", type=click.Path(path_type=Path), help="Output file for results (JSON)")
@click.option("--max-concurrent", default=3, help="Max concurrent program searches")
@click.option("--user-id", help="AwardWallet user ID to check balances for")
@click.option("--show-balances", is_flag=True, help="Fetch and display AwardWallet balances for search context")
def search(
    origin: str,
    destination: str,
    date: str,
    return_date: Optional[str],
    cabin: str,
    programs: Optional[str],
    round_trip: bool,
    visible: bool,
    config: Optional[Path],
    credentials_dir: Optional[Path],
    output: Optional[Path],
    max_concurrent: int,
    user_id: Optional[str],
    show_balances: bool,
):
    """Search for award space across multiple programs"""
    from datetime import date as date_type

    query = SearchQuery(
        origin=origin.upper(),
        destination=destination.upper(),
        departure_date=date_type.fromisoformat(date),
        return_date=date_type.fromisoformat(return_date) if return_date else None,
        cabin=cabin.lower(),
        round_trip=round_trip,
    )

    if programs:
        program_list = [p.strip() for p in programs.split(",")]
    else:
        program_list = None

    engine = SearchEngine(visible_mode=visible, config_path=config)
    asyncio.run(_run_search(engine, query, program_list, credentials_dir, output, max_concurrent, user_id, show_balances))


_balances_cache: Optional[BalanceSummary] = None
_effective_user_id: Optional[str] = None


def _show_balances_header(user_id: Optional[str]):
    global _balances_cache, _effective_user_id

    creds = AwardWalletClient._load_cred()
    configured_user = creds.get("user_id") if creds else None
    _effective_user_id = user_id or configured_user

    if not _effective_user_id:
        click.echo("\nNote: No AwardWallet user ID configured (set --user-id or credentials/awardwallet.yml)")
        return

    click.echo(f"\n--- Balances for user {_effective_user_id[:8]}... ---")


def _display_balance_summary(user_id: Optional[str]):
    global _balances_cache, _effective_user_id

    if not _effective_user_id:
        return

    try:
        api_key = os.environ.get("AWARDWALLET_API_KEY")
        if not api_key and AwardWalletClient._load_cred():
            api_key = AwardWalletClient._load_cred().get("api_key")

        if not api_key:
            click.echo("\nNo AwardWallet API key configured")
            return

        client = AwardWalletClient(api_key, _effective_user_id)
        summary = client.get_balances()
        _balances_cache = summary

        airlines = summary.airlines()
        if not airlines:
            click.echo("\nNo airline accounts found for this user")
            return

        click.echo(f"\n{'='*50}")
        click.echo(f"Available Miles (as of {summary.timestamp})")
        click.echo(f"{'='*50}")
        for a in airlines:
            status_str = f" | {a.status}" if a.status else ""
            click.echo(f"  {a.display_name:35s} {a.balance_formatted:>12s}{status_str}")

    except Exception as e:
        click.echo(f"\nCould not fetch balances: {e}")


async def _run_search(
    engine: SearchEngine,
    query: SearchQuery,
    program_list: Optional[List[str]],
    credentials_dir: Optional[Path],
    output: Optional[Path],
    max_concurrent: int,
    user_id: Optional[str],
    show_balances: bool,
):
    await engine.initialize()

    try:
        credentials = load_credentials(credentials_dir) if credentials_dir else {}

        if program_list is None:
            program_list = list(engine.programs_config.keys())

        click.echo(f"\nSearching {len(program_list)} programs...")
        if engine.visible_mode:
            click.echo("Visible mode enabled - VNC available at port 5900")

        if show_balances:
            _show_balances_header(user_id)

        results = await engine.search(
            query,
            program_list,
            max_concurrent=max_concurrent,
            credentials=credentials,
        )

        _display_results(results)

        if show_balances:
            _display_balance_summary(user_id)

        if output:
            _write_output(results, output)

    finally:
        await engine.close()


def _display_results(results):
    click.echo(f"\n{'='*60}")
    click.echo(f"Search Results ({results.search_id[:8]})")
    click.echo(f"{'='*60}")
    click.echo(f"Query: {results.query.origin} → {results.query.destination} on {results.query.departure_date}")
    click.echo(f"Duration: {results.duration_seconds:.1f}s")
    click.echo(f"Programs searched: {len(set(r.program for r in results.results))}")
    click.echo(f"Total results: {len(results.results)}")

    if results.errors:
        click.echo(f"\nErrors ({len(results.errors)}):")
        for err in results.errors:
            click.echo(f"  - {err}")

    click.echo(f"\n{'Flight':<10} {'From':<6} {'To':<6} {'Time':<12} {'Duration':<10} {'Stops':<6} {'Miles':<8}")
    click.echo("-" * 70)

    for result in results.results:
        for seg in result.segments:
            click.echo(
                f"{seg.flight_number:<10} {seg.departure_airport:<6} {seg.arrival_airport:<6} "
                f"{seg.departure_time} - {seg.arrival_time:<12} "
                f"{seg.duration_minutes//60}h {seg.duration_minutes%60:02d}m "
                f"{seg.stops:<6} {result.price.miles:,}"
            )
        click.echo()


def _write_output(results, output: Path):
    data = {
        "search_id": results.search_id,
        "timestamp": results.timestamp.isoformat(),
        "query": {
            "origin": results.query.origin,
            "destination": results.query.destination,
            "departure_date": results.query.departure_date.isoformat(),
            "return_date": results.query.return_date.isoformat() if results.query.return_date else None,
            "cabin": results.query.cabin,
            "round_trip": results.query.round_trip,
        },
        "results": [
            {
                "program": r.program,
                "segments": [
                    {
                        "airline": s.airline,
                        "flight_number": s.flight_number,
                        "departure_airport": s.departure_airport,
                        "arrival_airport": s.arrival_airport,
                        "departure_time": s.departure_time,
                        "arrival_time": s.arrival_time,
                        "duration_minutes": s.duration_minutes,
                        "stops": s.stops,
                        "cabin": s.cabin,
                    }
                    for s in r.segments
                ],
                "price": {
                    "miles": r.price.miles,
                    "cabin": r.price.cabin,
                    "taxes": r.price.taxes,
                    "currency": r.price.currency,
                },
                "availability": r.availability,
            }
            for r in results.results
        ],
        "errors": results.errors,
        "duration_seconds": results.duration_seconds,
    }

    with open(output, "w") as f:
        json.dump(data, f, indent=2)

    click.echo(f"\nResults written to {output}")


@cli.command()
def version():
    """Show version information"""
    click.echo("Award Search CLI v0.1.0")


if __name__ == "__main__":
    cli()