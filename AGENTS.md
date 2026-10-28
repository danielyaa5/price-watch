# price-watch project instructions

Self-hosted marketplace watcher. Read `README.md` and the affected modules before editing.

- Watching only: never contact sellers, make offers, buy, or sign in to stores. Deal research uses web search and page reading only, and its results never alert.
- Matching is local and rule-based. A listing missing a required detail stays a candidate and never alerts; keep the quiet first-scan baseline and once-per-channel delivery.
- Credentials, search areas, watches and runtime state stay in ignored files (`config.toml`, `*-private.json`, `watches.json`, `state/`). This repository is public: use fictional listings and public landmarks in tests and docs.
- Validation: `python -m unittest discover -p "test_*.py"` with the Python that ai-marketplace-monitor is installed into. Tests must not use the network.
- Keep commits authored by the repository owner without tool attribution.
