# Security

price-watch keeps every credential in ignored files next to the code (`*-private.json`) or in the
tools it calls (the upstream Facebook session, workspace-mcp's Gmail credentials, the agent CLIs'
own sign-ins). Don't commit those files, paste them into issues, or share `state/`, which holds
listings, locations and alert history.

The optional eBay account-deletion receiver (`ebay_receiver.py`) listens on loopback only and checks
eBay's signature on every notification. If you publish it for eBay, for example through a tunnel,
publish only that receiver. Deal research runs agent CLIs with web search
and page reading only; it never signs in to stores or buys anything.

To report a vulnerability, use GitHub's private vulnerability reporting on this repository, or email
support@danielyakobian.com without exploit details and ask for a private channel.
