# Execution providers

Each module owns one official subscription-CLI boundary: capability qualification, isolated environment construction, command compilation, and provider-envelope parsing. The execution supervisor and journal remain provider-neutral; domain projects never import these modules directly.

`muse.py` admits Meta Spark only through Muse Code's owner-only device-code OAuth metadata and keychain session. Muse requires that metadata at its native config root to resolve the Keychain item, so the adapter censes that root, admits only auth plus the bounded provider/model/TUI settings and exact-path trust registry, and overrides the execution-relevant settings on the command line. Muse data, cache, state, and workspace paths remain disposable; developer credentials and proxy overrides are excluded; foreign personal context, durable native session logs, shell, writes, and parallel tool calls are disabled; and the web-search surface is gated by the selected Lattice profile.

The kernel recovery, journal, bound, and Claude system-instruction contracts live in the parent [`../README.md`](../README.md).

## Model catalog

`DEFAULT_ADAPTERS` in [`../adapters.py`](../adapters.py) contains the explicit model-ID catalog used by route admission. It is an implementation-maintained catalog, not an owner-independent authorization policy. Workflow `allowed_models` is a separate, intentional restriction. Catalog membership does not establish account entitlement, every model/effort combination, or a successful live invocation; native authentication and capability checks still apply. Existing operational defaults and workflow pins are preserved when adding newer choices.

The catalog was checked against these provider-owned surfaces on 20260923:

| Route | Catalog evidence | Scope |
|---|---|---|
| Claude | [Model configuration](https://code.claude.com/docs/en/model-config), [model IDs](https://platform.claude.com/docs/en/models/overview) | Opus 5.5, Sonnet 5, Fable 5.1, Haiku 4.5, family aliases, and retained existing IDs. Aliases track the provider's resolution; use an exact ID when the version matters. |
| Codex | Local `~/.codex/models_cache.json`, fetched 20260923T080919, client 0.153.4 | All five visible models: Astra, Sol, Terra, Luna, and GPT-5.5. Hidden reserve and automatic-review routes are not general-purpose choices. |
| Antigravity | Authenticated `agy models`, CLI 1.2.8 | All eleven Gemini IDs returned: Flash 3.8/3.7/3.6 at high/medium/low and Pro 3.1 at high/low. Foreign-provider models returned by the same harness are outside this Google-model route. Match `effort` to the model suffix; the native probe rejects any effective-model substitution. |
| Grok | Authenticated `grok --no-auto-update models`, CLI 1.0.40 | All four returned models: 4.7, 4.7 Build Fast, 4.6, and 4.5. |
| Muse | Local Muse `model-catalog` provider cache, last modified 20260920; [current release](https://research.meta.ai/blog/introducing-muse-spark-1-3) | Spark 1.3 and 1.2, each with the ordinary and contributor ID. The cached catalog is not a fresh entitlement check. |
| Kimi | [Official model configuration](https://www.kimi.com/code/docs/en/kimi-code/models.html) | Correct IDs are `k3`, `k3-256k`, `kimi-for-coding`, and `kimi-for-coding-highspeed`. This remains an unavailable, unqualified route; catalog maintenance does not enable it. |

Two established designs distinguish catalog maintenance from access policy: Claude Code accepts aliases and full IDs while separately exposing administrator `availableModels` restrictions; [OpenCode](https://opencode.ai/v2/docs/models) combines a provider catalog with custom model definitions. Lattice currently retains explicit catalog admission. Refreshing it does not introduce arbitrary-ID pass-through, automatic model fallback, a developer-API route, or additional execution authority.

The Google operational default and release-qualification route use `gemini-3.8-flash-high` at `high` effort. The authenticated `agy models` account listing on 20261020 exposes Flash 3.8 and no longer exposes Flash 3.7. Historical model IDs remain admitted for provenance and explicit route inspection; their catalog membership does not claim current availability. Workflow-specific model pins remain independent of the operational default.
