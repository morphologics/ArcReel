---
status: accepted
---

# Price tables use a general "meter × selection dimension" structure, a single currency with no conversion, and a missing unit price is recorded as unpriced

> In this ADR, the rule "a missing unit price is recorded as unpriced" has been refined by "Meter classification and unpriced when everything misses (revision, 2026-10-15)" at the end, and the rule that selection dimensions are "defined by code" has been supplemented with a note that they can be added along with code; everything else remains in effect.

A model entry's price table is no longer split into several pricing shapes, but unified into one structure: one currency, plus a number of meters, each giving unit prices keyed by selection dimension values, optionally with a per-request free allowance. The calculator only sums over meters: cost = multiplier × Σ over meters max(usage − free allowance, 0) × unit price(selection dimensions), with no dispatch by pricing shape or media type. Mainstream vendors' pricing all fits this formula, whereas mixed pricing such as "per second + surcharge per reference image + base price per call" could only be patched together from fixed shapes.

## Rules

- Request-level tiers and service tiers are both handled as selection dimensions, with no separate mechanism: context tiers are bucketed by total input tokens, and service tiers are not modelled as multipliers.
- Selection dimensions are defined by code; catalog data cannot invent dimensions. Selection dimension values come from the request fields actually sent to the provider (see `docs/adr/0056`). Code can add dimensions as needed (e.g. `reference_to_video` to distinguish whether it is reference-to-video), adding them to the closed set of dimensions and adding validation at the same time.
- Usage of different meters does not overlap: uncached input, cache reads and the cache writes of each tier sum to total input. Differences in vendors' usage semantics are handled by the calling side before reporting; the calculator does not recognise specific providers.
- Each entry has only one price table, in the currency of the model vendor's main site, with no exchange-rate conversion.
- Channel price differences are expressed only by multipliers, multiplied across two levels: the provider multiplier is a user preference; the provider model multiplier comes with official data on officially listed provider models, and is filled in by the user on user-responsible provider models. The multiplier applies to the whole cost, not set per meter.
- A missing unit price is recorded as unpriced, with no fallback to prices of other models or other tiers.
- When the multiplier is 0, the cost is recorded as 0 without looking up prices, so unpriced cannot occur either.

## Considered Options

- **Merge into 5 fixed shapes: per token, per image, per second, per call, per character**: the form is simple, but mixed pricing would need patching across shapes, and every new combination would require changing the calculator. These 5 shapes can serve as presets for the custom model form, with the preset generating the general structure.
- **Let one entry carry multiple price tables by site**: several Chinese vendors have different prices on their domestic and international sites, and the two are not related by exchange rate. But introducing the concept of "which price table a provider model uses" for a small number of international-site users costs too much; LiteLLM and new-api also have only one set of prices. International-site users can modify the entry.
- **Unify into a single currency, converting at a fixed exchange rate in the snapshot**: models with only RMB prices, such as Ark, would carry exchange-rate error, and who maintains the rate and how often it updates would become a new sync problem.
- **Fall back to the default model or the nearest tier when the unit price is missing**: this was the old approach. The computed amount looks normal but is actually the price of another model or tier, which misleads users more than showing "unpriced".

## Consequences

- Cost in `api_calls` must be able to represent unpriced, distinct from 0; usage records and cost summaries must show unpriced calls separately.

## Meter classification and unpriced when everything misses (revision, 2026-10-15)

Applying "a missing unit price is recorded as unpriced" uniformly to all meters would make almost every video call wholly unpriced: once channels faithfully report output seconds, reference image count and call count, even entries that charge only per second would receive a reference image count. Channels should not need to know how an entry is priced, so meters are distinguished by class:

- Meters are classified in the vocabulary as token-type or count-type. The calculator reads only this declaration, with no hard-coded list.
- Token-type: when reported usage is greater than 0 but the price table has no unit price for that meter, the whole call is recorded as unpriced. These usages are parts split from the same token total, and one missing part cannot be counted as 0.
- Count-type (seconds, images, calls, characters, video tokens, etc.): if the price table has no such meter, the vendor does not charge by it, and it is not billed.
- At settlement, if none of the meters in the price table have reported usage, the whole call is recorded as unpriced, not 0; for example, calls where usage cannot be obtained. During estimation, meters missing a quantity still get an estimate rather than unpriced.

The decision not to fall back to other models or tiers when a unit price is missing is unchanged.
