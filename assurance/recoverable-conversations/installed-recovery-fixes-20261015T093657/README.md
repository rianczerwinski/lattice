# Installed recovery fixes

This retained qualification run builds the frozen source revision through the normal pinned installer, including source checks, rollback capture, copied-store migration, publication and health. It dispatches no model work. The supervisor records terminal state and logs under `jobs/install/`; `progress.json` records observed phase boundaries. A replacement operator must inspect those records before launching another installer.
