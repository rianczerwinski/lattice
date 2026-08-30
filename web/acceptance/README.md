# Installed browser acceptance

`installed-conversation.mjs` drives the shipped Lattice cockpit through its single-use operator bootstrap. The bootstrap arrives only on process stdin, the browser uses a fresh ephemeral profile, and the driver returns only synthetic action markers. `ops/qualify_browser.py` independently resolves those markers against the base SQLite ledger before minting a browser-acceptance receipt.
