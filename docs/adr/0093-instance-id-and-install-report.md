---
status: accepted
---

# Instance ID and install reporting: a random resettable ID, report only the official source, on by default with a first-time notice

For the official service to count how many ArcReel instances have installed each market entry, and to restrict who can rate on that basis, it needs a principal that can be deduplicated; but ArcReel has no accounts (`docs/adr/0078`). We decided to use the **instance ID** as the sole principal:

- The instance ID is a random UUID generated locally the first time it is needed and stored in system settings, unrelated to the machine, the user or any account; the user can reset it on the settings page, after which the official service treats it as a new instance.
- Every request to the official service carries the instance ID in a request header; the official service stores only its salted hash, never the original value, and IP is used only for rate limiting and never persisted.
- A report is sent only once, after an install from the **official market source** completes (market source canonical key, type, slug, version, application version); updates and uninstalls are not reported, and installs from third-party market sources are never reported. Repeated reports from the same instance for the same entry are idempotent, and install counts are not decremented.
- Reporting is on by default; the first time the user enters the market page, it explains what is reported and where to turn it off, and after confirmation it does not prompt again.
- The official service URL is built into each release and can be overridden by an environment variable; the user-visible master switch is stored in system settings. An empty URL or the switch turned off counts as off: the market page shows no official service elements, and the local server makes zero outbound connections to the official service.
- The report is sent after the install transaction commits; failure does not affect the install and is not retried; the frontend communicates only with the local server, which proxies all calls to the official service.

## Considered Options

- **Accounts or device fingerprints**: accounts require long-term operation of an account system; fingerprints cannot be reset by the user, and exceed the amount of information anonymous counting needs.
- **Off by default (opt-in)**: statistics would have almost no samples, and the "installed" precondition for rating would be meaningless; on by default plus a first-time notice and one-click disable is the trade for usable data.
- **Also report third-party market sources**: the official service would learn the source URLs users added privately, and counts for those entries mean nothing to the official market.

## Consequences

- Installation records (`docs/adr/0079`) are unchanged; reporting is a side path after the install transaction, and installs proceed as usual when the official service is unreachable.
- Rating requires that "this instance has reported an install of this entry"; resetting the instance ID means giving up the previous rating identity.
- White-label or self-hosted distributions can turn off the official service entirely by overriding the URL to empty.
