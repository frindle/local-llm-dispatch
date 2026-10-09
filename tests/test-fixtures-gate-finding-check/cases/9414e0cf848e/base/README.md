# Award Search

Web-based tool to search award flight availability across multiple loyalty programs.

## Quick Start

```bash
git clone https://github.com/frindle/award-search
cd award-search
docker build -t award-search .
docker run -d -p 8000:8000 \
  -e SEATS_AERO_API_KEY=your-seats-aero-key \
  -e AWARDWALLET_API_KEY=your-awardwallet-key \
  -e AWARDWALLET_USER_ID=your-user-id \
  -e SERPAPI_API_KEY=your-serpapi-key \
  -e PUSHOVER_APP_TOKEN=your-pushover-token \
  -e PUSHOVER_USER_KEY=your-pushover-key \
  award-search
```

Then open http://localhost:8000

## Features

| Feature | Description |
|---------|-------------|
| **Seats.aero Search** | Award availability across 27 programs via API |
| **AwardWallet Balances** | View your mileage/points balances |
| **Positioning Flights** | Find cheap flights to your hub via Google Flights |
| **Alert Notifications** | Pushover alerts when award space opens |

## API Keys

Create these files in the `credentials/` folder OR set environment variables:

| Service | File | Env Variable | Get from |
|---------|------|--------------|----------|
| Seats.aero | `seats_aero.yml` | `SEATS_AERO_API_KEY` | [seats.aero/settings](https://seats.aero/settings) |
| AwardWallet | `awardwallet.yml` | `AWARDWALLET_API_KEY`, `AWARDWALLET_USER_ID` | [business.awardwallet.com](https://business.awardwallet.com/profile/api) |
| SerpAPI | `serpapi.yml` | `SERPAPI_API_KEY` | [serpapi.com](https://serpapi.com) |
| Pushover | `pushover.yml` | `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | [pushover.net](https://pushover.net/api) |

Example `credentials/seats_aero.yml`:
```yaml
api_key: "your-key-here"
```

### Credential File Locations

Files are searched in order:
1. `./credentials/*.yml` (relative to project root)
2. `~/.config/award-search/*.yml`

## Web UI Pages

- `/` - Search awards
- `/balances` - View AwardWallet balances
- `/alerts` - Manage award alerts
- `/positioning` - Search positioning flights

## CLI Usage

```bash
# Build
docker build -t award-search .

# Run CLI
docker run --rm -v $(pwd)/credentials:/app/credentials:ro award-search python -m src.cli --help

# List programs
docker run --rm -v $(pwd)/credentials:/app/credentials:ro award-search python -m src.cli list-programs

# Search
docker run --rm -v $(pwd)/credentials:/app/credentials:ro \
  -e SEATS_AERO_API_KEY=your-key \
  award-search \
  python -m src.cli search -o JFK -d LAX --date 2024-07-15 -c business -p united,delta
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `SEATS_AERO_API_KEY` | Seats.aero Partner API key | For award search |
| `AWARDWALLET_API_KEY` | AwardWallet Business API key | For balances |
| `AWARDWALLET_USER_ID` | AwardWallet user ID | For balances |
| `SERPAPI_API_KEY` | SerpAPI key for Google Flights | For positioning |
| `PUSHOVER_APP_TOKEN` | Pushover app token | For alert notifications |
| `PUSHOVER_USER_KEY` | Pushover user key | For alert notifications |

## Docker Compose (optional)

```yaml
services:
  award-search:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./credentials:/app/credentials:ro
    environment:
      - SEATS_AERO_API_KEY=${SEATS_AERO_API_KEY}
      - AWARDWALLET_API_KEY=${AWARDWALLET_API_KEY}
      - AWARDWALLET_USER_ID=${AWARDWALLET_USER_ID}
      - SERPAPI_API_KEY=${SERPAPI_API_KEY}
      - PUSHOVER_APP_TOKEN=${PUSHOVER_APP_TOKEN}
      - PUSHOVER_USER_KEY=${PUSHOVER_USER_KEY}
```

```bash
docker-compose up -d
```

## Supported Programs

United, Delta, American, Southwest, JetBlue, Lufthansa, Air Canada, Flying Blue, Emirates, Virgin Atlantic, Alaska, British Airways, Cathay Pacific, Singapore Airlines, and more via Seats.aero.

## Development

```bash
# Install locally
pip install -r requirements.txt

# Run web UI
uvicorn src.webui.app:app --reload --port 8000

# Run CLI
python -m src.cli --help
```