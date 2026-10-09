# Award Search Docker Tool

## Concept & Vision

A Docker-based browser automation system for searching award space across multiple loyalty programs. Runs headless Chrome in a GPU-accelerated container with the ability to drop into a visible browser window for authentication flows. Configurable per-search to specify which programs to query.

## Architecture

### Container Setup
- **Base**: `nvidia/cuda:11.8-cudnn8-runtime-ubuntu22.04` for GPU support
- **Browser**: Chromium via Playwright (headless by default, visible mode on-demand)
- **Display**: VNC server (noVNC) for visible browser windows when login is required
- **Languages**: Python 3.11+ with async support

### Key Components
```
├── Dockerfile                 # Container definition with GPU + browser support
├── docker-compose.yml         # Service orchestration
├── config/
│   └── programs.yml           # Loyalty program configurations
├── src/
│   ├── browser/
│   │   ├── manager.py         # Browser lifecycle management
│   │   ├── headless.py        # Headless Chrome via Playwright
│   │   └── vnc.py             # VNC server for visible windows
│   ├── search/
│   │   ├── engine.py          # Core search orchestration
│   │   └── programs/          # Per-program search implementations
│   │       ├── base.py        # Abstract base class
│   │       ├── united.py      # United MileagePlus
│   │       ├── delta.py       # Delta SkyMiles
│   │       ├── american.py    # AAdvantage
│   │       └── ...
│   └── cli.py                 # Command-line interface
├── requirements.txt
└── README.md
```

## Core Features

### 1. Configurable Program Selection
- YAML config listing all supported loyalty programs with metadata
- CLI flag `--programs` to specify which programs to search (comma-separated)
- Programs: United, Delta, American, Southwest, JetBlue, Lufthansa, Air Canada, Flying Blue, Emirates, etc.

### 2. Headless Search
- Default mode: headless Chromium via Playwright
- Stealth mode: randomize user agent, viewport, timing
- Rate limiting per-site to avoid blocks
- Retry logic with exponential backoff

### 3. Visible Window Mode
- Triggered automatically when login is required
- VNC/noVNC stack for browser access
- Session persists for re-authentication
- Graceful fallback if VNC fails

### 4. Search Parameters
```
--origin          Origin airport code (e.g., JFK)
--destination     Destination airport code (e.g., LAX)
--date            Departure date (YYYY-MM-DD)
--return          Return date (optional, YYYY-MM-DD)
--cabin           Economy, Business, First
--programs        Comma-separated program names
--round-trip      Boolean flag
```

### 5. Program Adapters
Each loyalty program has an adapter that:
- Handles site-specific authentication flow
- Implements search parameter mapping
- Parses availability results into normalized format
- Manages rate limits and captcha handling

## Normalized Output Format

```json
{
  "search_id": "uuid",
  "timestamp": "ISO8601",
  "query": {
    "origin": "JFK",
    "destination": "LAX",
    "departure": "2024-06-15",
    "return": null,
    "cabin": "business"
  },
  "results": [
    {
      "program": "united",
      "flight": "UA123",
      "date": "2024-06-15",
      "departure": "08:00",
      "arrival": "11:30",
      "duration": "5h30m",
      "stops": 0,
      "availability": "standard",
      "price": {
        "miles": 60000,
        "cabin": "business",
        "taxes": 11.20
      }
    }
  ]
}
```

## Technical Decisions

### Playwright over Selenium
- Native async support
- Built-in stealth mode options
- Better headless performance
- Cleaner API

### VNC Stack
- `tigervnc` or `x11vnc` for VNC server
- `novnc` for web-based access
- WebSocket bridge for browser access

### GPU Utilization
- Primarily for OCR (pytesseract) if captcha handling needed
- CUDA加速 for any ML-based page parsing
- Optional via `--gpus all` flag

## CLI Examples

```bash
# Basic search (headless)
python -m src.cli search --origin JFK --destination LAX --date 2024-06-15 --cabin business --programs united,delta

# Visible mode for login
python -m src.cli search --origin JFK --destination LAX --date 2024-06-15 --programs american --visible

# List available programs
python -m src.cli list-programs

# Run with custom config
python -m src.cli search --origin SFO --destination NRT --date 2024-07-01 --programs lufthansa,flyingblue --config ./my-programs.yml
```