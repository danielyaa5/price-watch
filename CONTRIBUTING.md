# Contributing

Use Python 3.11 or later with ai-marketplace-monitor installed (`pip install -r requirements.txt`),
and run the tests before proposing a change:

```bash
python -m unittest discover -p "test_*.py"
```

Tests use fixtures and fake geocoders and must not touch the network or a real account. Add a small
made-up fixture when a marketplace's page format changes. Keep real listings, locations, accounts and
alert history out of issues, fixtures and screenshots.
