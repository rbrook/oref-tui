# oref-tui

Terminal UI for [oref.live](https://oref.live) — real-time Israeli rocket/missile alerts in the terminal with a braille map, live feed, and area-of-interest tracking.

## Features

- **Braille map** of Israel with alert zones rendered in color (red = rockets, orange = UAV, purple = infiltration, yellow = pre-warning)
- **Live feed** of alert events with type, area count, districts, and relative timestamps
- **Threat bar** showing active threat types
- **Areas of Interest (AOI)** — save locations to watch; terminal bell rings on new alerts
- Auto-reconnect with exponential backoff
- Toggle panels on/off; map expands to fill available space

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/rbrook/oref-tui
cd oref-tui
```

Install dependencies using **one** of:
```bash
uv sync                                  # with uv (recommended)
pip install -r requirements.txt           # with pip
```

To install without orjson (uses stdlib json instead, e.g. on Termux/Android):
```bash
uv sync --no-group fast              # with uv
pip install -r requirements-minimal.txt  # with pip
```

## Usage

```bash
uv run tui.py       # with uv
python tui.py        # without uv
```

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `m` | Toggle map |
| `f` | Toggle feed |
| `i` | Toggle AOI panel |
| `a` | Add area of interest |
| `d` | Delete area of interest |
| `Enter` | Open feed card detail |
| `Tab` | Switch focus between panels |
| `Escape` | Unfocus / go back |

## Areas of Interest

Press `a` to search for an area or district by name. Matching AOIs are highlighted on the map and the terminal bell rings on new alerts in those areas.

Configuration is saved to `~/.config/oref-tui/aoi.json`.
