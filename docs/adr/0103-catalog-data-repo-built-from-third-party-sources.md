---
status: accepted
---

# Model catalog data is built automatically from third-party data sources by our own public repository; instances sync fully automatically, without going through the official service

Metadata was switched to syncing so that prices and new models keep pace with vendors, rather than waiting for a release. But no single third-party data source can handle this alone: coverage of text models is good, Chinese vendors' image, video and TTS models are largely absent, and formats differ from source to source. We decided that a public catalog data repository publishes catalog data in ArcReel's own format as the sole source of official sync, and the factory snapshot is taken from it too. The repository is built on a schedule from third-party data sources automatically, with manually maintained data layered on top; instances automatically check for and apply new data. This way the timeliness of text models equals that of upstream, and manual effort only maintains what third parties do not have.

## Rules

- Third-party data sources are used only as build input; instances do not read them directly. Manually maintained data takes precedence over automatically built results.
- Built-in provider lists for text models are generated automatically from an "upstream provider → built-in provider" mapping, with manual effort only maintaining filter rules and a few additions and removals; lists for media models are maintained manually.
- At build time, model aliases are generated for entries: dated snapshot names whose prices and capabilities match the main name, and the names used by relay services in third-party data sources. How model names map to entries, see `docs/adr/0105`.
- Build results are published only after passing the same validator as the main repository; on failure they are not published.
- Instances fetch only via raw URLs, reusing the market's GitHub raw proxy prefix, and requests do not carry the instance ID (see `docs/adr/0078`, `docs/adr/0093`). The official service is not on the sync path, and turning off the official service does not affect sync.
- When an instance finds a newer dataset version it applies it automatically as a whole, without user confirmation; model entries with official sync turned off are skipped.
- Fault tolerance follows the reference-cost standard: unknown fields are ignored; when a price depends on an unknown selection dimension it is recorded as unpriced per `docs/adr/0100`; when the whole dataset cannot be parsed, that fetch is rejected. There is no schema version number.
- Officially listed provider models can carry a minimum application version, marking models that need a newer invocation channel to be called. Instances below that version neither show nor enable them; when official data designates such a model as the default, those instances treat it as if no default model were designated. Instances ignore unknown fields, so this field has to exist from the first version of the data format, so that every instance able to sync will honour it.
- Sync does not delete model entries; entries deleted upstream keep their last version locally; officially listed provider models under built-in providers follow the data as a whole; user-added ones are unaffected until official data lists the same API model name (see `docs/adr/0099`).

## Considered Options

- **Instances read third-party data sources directly**: saves a repository, but as soon as upstream changes its format, all released clients break at once; reachability from mainland China is also uncertain. And media models would still have no source.
- **Deliver via the official service**: could do incremental updates and version filtering, but once the official service is turned off sync stops too, and the official service would learn each instance's sync behaviour. `docs/adr/0078` already chose "zero server + raw URLs" for the market, and the same path is followed here.
- **ArcReel maintains everything manually**: the data is most controllable, but text models update most often, and following them by hand brings back the old problem of "data not timely".
- **Manual sync, or automatic check followed by user confirmation**: this is what new-api does, but users who never click the button only get the data from release time. Entries users have changed are already protected by sync being turned off automatically, so full automation will not overwrite them.
- **Per-field preview with checkboxes (new-api's approach)**: users could accept only some fields, but entries would become half from upstream and half from the user, conflicting with the whole-copy rule of `docs/adr/0099`.
- **Schema version number plus entry-level validation**: could stop old clients from reading data they do not understand. But falling back to unpriced for prices they cannot understand is enough, since costs are only a reference; the only thing that would actually make calls fail is a new model the old invocation channel cannot encode, which can be stopped separately with the minimum application version on the provider model, without versioning the whole dataset.
- **No minimum application version; the catalog data repository just does not list models the old channels cannot call**: a simpler data format. But the listing timing becomes a forced choice: list early and old instances fail to call it; list late and new instances wait a long time to use it. When a new model is set as the default model, every old instance without its own default model set would fail.
- **Delete entries deleted upstream during sync**: a cleaner catalog, but provider models still referencing those entries would lose the entry they reference, and all subsequent calls would be recorded as unpriced.

## Consequences

- Changes to built-in models' prices and metadata no longer require code changes and a release.
- New maintenance item: upstream format changes will make the build fail, requiring changes to the conversion logic.
- The official service no longer needs a model information interface.
