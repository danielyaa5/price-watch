# price-watch

A self-hosted watcher for things you want to buy used or at a good price. It searches local and online marketplaces on a schedule, matches listings against exact specifications and a budget, and sends one email and/or [ntfy](https://ntfy.sh) phone alert per new qualifying listing.

It runs on an always-on Mac and is built around [ai-marketplace-monitor](https://github.com/BoPeng/ai-marketplace-monitor) for Facebook Marketplace, with its paid AI features turned off. Matching is local and rule-based. An optional research job asks an agent CLI (Antigravity or Claude Code) to look for deals and announced sales a few times a day.

This is a personal tool shared as-is: it is built for one Mac and one search area, and some presets match the products it was written for. Issues and pull requests are welcome.

## Sources

| Source | How | Notes |
|---|---|---|
| Facebook Marketplace | Browser automation with a saved session you sign in to once | Facebook's local-pickup filter; listings outside the radius are skipped before they are opened |
| eBay | Official Browse API | Buy It Now, US listings; budget checks include known shipping |
| OfferUp | Public search pages and item location details | Distance and neighborhood from item map coordinates and ZIP codes (OfferUp pads small radii with distant listings) |
| Craigslist | Public search and listing pages | Distance from the listing's map marker |
| Retail stores | Direct store searches | New or any-condition watches |
| Amazon | Public search pages | New or any-condition watches |
| Google Shopping | SerpApi (free tier) | Every few hours, within the monthly quota |
| Deal research (optional) | `agy` (Gemini) or `claude` CLI with web search only | Three times a day; results are saved for review and never alert |

## How matching and alerts work

- Each watch has search phrases, a price range (optionally separate for new and used), a condition, a radius and the sources to search. Presets cover a few product families with exact-variant rules (Mac Studio, 16-inch MacBook Pro, Bambu Lab AMS 2 Pro, Thunderbolt 4 and 5 hubs); custom watches use required and excluded phrases.
- Listings that pass every rule are **matches**. Listings missing a required detail are **candidates** for review and never alert.
- Pickup sources search around one center (`[search_area]` in `config.toml`). Towns are placed with Apple's geocoder (`bin/geocode`, cached in `state/places.json`); a town farther than the radius plus 3 miles is out of area. A bare big-city name that cannot place the seller stays a candidate.
- The first successful scan of each source and channel records existing matches quietly, so you never get a backlog. After that, each new listing ID is sent once per channel. Uncertain deliveries are held for review and never retried automatically.
- Changing a watch's radius hides pickup listings found with a wider radius; the next scan brings back the ones inside it.

## Requirements

- macOS (the geocoder uses Core Location, and browser checks pause during Screen Sharing)
- Python 3.11 or later, and [uv](https://docs.astral.sh/uv/) or pip
- A Facebook account for Marketplace; API keys only for the optional eBay and Google Shopping sources

## Setup

1. Install the collector: `uv tool install ai-marketplace-monitor==0.10.16`, then its browser: `~/.local/share/uv/tools/ai-marketplace-monitor/bin/python -m playwright install chromium`. Run price-watch with that Python (shown as `python` below).
2. `cp config.example.toml config.toml` and set your search area and watches. Each `[item.<name>]` is a watch; `[watch.<name>]` can set `notify` and `sources` (for example `['facebook', 'craigslist', 'offerup']`).
3. Build the geocoder: `swiftc -O geocode.swift -o bin/geocode`.
4. Sign in to Facebook once: `python watch.py --login`. The session is stored by the upstream tool with mode 0600; no password is kept.
5. Optional private files next to the code (all ignored by git):
   - `notification-private.json`: `{"server": "https://ntfy.sh", "topic": "<your topic>"}`
   - `mail-private.json`: `{"sender": "...", "recipient": "..."}`. Email goes through the Gmail API with credentials saved by [workspace-mcp](https://github.com/taylorwilsdon/google_workspace_mcp) in `~/.config/workspace-mcp/credentials/`, run with the Python in `~/.local/share/workspace-mcp/venv`
   - `ebay-private.json`: eBay API client ID, secret and account-deletion endpoint details
   - `shopping-private.json`: SerpApi key
   - `research-settings.json`: `{"engine": "claude"}` to research with Claude Code instead of Antigravity
6. Schedule `python watch.py --notify` every 30 minutes (for example with a LaunchAgent). Run `python watch.py` without `--notify` for a silent manual check, or `python watch.py --source craigslist` for one source.

Runtime state, logs, matches and research results live in `state/`. There is no web interface in this repository; `watch_config.py` validates the `watches.json` format that a separate dashboard can write, and `config.toml` alone is enough to run.

Browser checks wait while Screen Sharing is active or its status is unknown. The optional
`PRICE_WATCH_SHARING_PROBE` environment variable names a local LaunchAgent that can refresh
the status; otherwise the watcher waits for the separately scheduled probe.

## Tests

```bash
python -m unittest discover -p "test_*.py"
```

Run them with the Python that ai-marketplace-monitor is installed into. They use fixtures and fake geocoders, with no network access.

## Scope

Watching only. It never contacts sellers, makes offers or buys anything. Automating a signed-in Facebook account may conflict with Facebook's terms, and store pages change without notice; use it at your own risk.

## License

[MIT](LICENSE)
