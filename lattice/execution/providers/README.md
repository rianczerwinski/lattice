# Execution providers

Each module owns one official subscription-CLI boundary: capability qualification, isolated environment construction, command compilation, and provider-envelope parsing. The execution supervisor and journal remain provider-neutral; domain projects never import these modules directly.

The kernel recovery, journal, bound, and Claude system-instruction contracts live in the parent [`../README.md`](../README.md).
