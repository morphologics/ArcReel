// Brand configuration — single source of truth for product naming.
// Override at build time via Vite env vars
// (VITE_BRAND_NAME / VITE_BRAND_TAGLINE / VITE_BRAND_DESCRIPTION).
//
// Source code references BRAND.name (or the [[brand]] placeholder in i18n
// resources) so the displayed product name is not hardcoded across files.
// Upstream defaults to "ArcReel"; this fork defaults to YouReel Studio (the ArcReel name and logo are not
// licensed under AGPL-3.0, see NOTICE). The "Powered by ArcReel" credit is separate and must stay.

const env = import.meta.env as Record<string, string | undefined>;

function fallback(value: string | undefined, defaultValue: string): string {
  // Trim + empty check so VITE_BRAND_NAME="" (or whitespace) falls back to the
  // default, matching the documented "Empty = upstream defaults" contract.
  if (typeof value !== "string") return defaultValue;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : defaultValue;
}

export const BRAND = {
  name: fallback(env.VITE_BRAND_NAME, "YouReel Studio"),
  tagline: fallback(env.VITE_BRAND_TAGLINE, "AI video creation studio"),
  description: fallback(
    env.VITE_BRAND_DESCRIPTION,
    "AI video creation studio: projects, scripts, storyboards, video generation and the agent in one place.",
  ),
} as const;

export const BRAND_DOCUMENT_TITLE = `${BRAND.name} · ${BRAND.tagline}`;

