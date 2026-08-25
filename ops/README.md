# Lattice operations

This directory contains the source-checkout deployment boundary for Lattice. [`install.py`](install.py) verifies a clean immutable source revision, runs the synthetic assurance suites, copies legacy AXISVI runtime state through a verified snapshot when necessary, installs a non-editable versioned Python environment, atomically switches the active runtime while retaining the prior one, and installs the user launchd service and daily backup jobs. The request-serving daemon uses launchd's `Interactive` process class because cockpit responsiveness depends on it; the scheduled snapshot remains `Background` work.

[`authenticated_acceptance.py`](authenticated_acceptance.py) authors the complete synthetic freestyle graph through the running service, optionally qualifies workspace and native provider sessions, induces a typed provider failure, restarts launchd, and writes a content-bound acceptance report under the canonical data root. It never imports a personal corpus or records provider response text in the report.

[`verify_execution_consolidation.py`](verify_execution_consolidation.py) is the independent closure boundary for Lattice 0.3 execution. It reads base SQLite tables, content-addressed blobs, raw runtime receipts, immutable provider profiles, installed package and binary attestations, consumer source trees, historical journal bytes, and exact credential canaries. It refuses `lattice_execution_ready` when the installed runtime manifest is absent or any execution evidence is unresolved.

```text
ops/
├── README.md                    Operations-boundary map and deployment contract
├── authenticated_acceptance.py Authenticated freestyle graph and recovery acceptance
├── install.py                   Verified installer, upgrade switch, state cutover, and launchd publisher
└── verify_execution_consolidation.py Independent execution-boundary verifier
```

The installed service never imports the development checkout. Runtime state, versioned environments, and backups live under `~/Library/Application Support/Lattice/`; logs live under `~/Library/Logs/Lattice/`. `~/.local/bin/lattice` is a stable executable shim that binds this personal deployment to AXISVI and follows the atomically selected runtime, so it works from any shell directory.
