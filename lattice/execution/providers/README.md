# Execution providers

Each module owns one official subscription-CLI boundary: capability qualification, isolated environment construction, command compilation, and provider-envelope parsing. The execution supervisor and journal remain provider-neutral; domain projects never import these modules directly.

`muse.py` admits Meta Spark only through Muse Code's owner-only device-code OAuth metadata and keychain session. Muse requires that metadata at its native config root to resolve the Keychain item, so the adapter censes that root, admits only auth plus the bounded provider/model/TUI settings and exact-path trust registry, and overrides the execution-relevant settings on the command line. Muse data, cache, state, and workspace paths remain disposable; developer credentials and proxy overrides are excluded; foreign personal context, durable native session logs, shell, writes, and parallel tool calls are disabled; and the web-search surface is gated by the selected Lattice profile.

The kernel recovery, journal, bound, and Claude system-instruction contracts live in the parent [`../README.md`](../README.md).
