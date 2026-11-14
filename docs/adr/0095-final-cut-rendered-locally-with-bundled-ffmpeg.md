---
status: accepted
---

# The final cut is rendered on a local render lane with the bundled ffmpeg; the final cut and Jianying (CapCut) draft are written to disk and registered as artifacts

> Jianying (CapCut) drafts with hold extensions need the bundled ffmpeg to extract the clip's out-point frame, and loudness measurement at BGM registration also uses the bundled ffmpeg; when the bundled ffmpeg cannot run, these two are unavailable just like final cut rendering. "Jianying draft export is unaffected" below applies only to Jianying drafts without hold extensions.

The final cut is rendered by the server calling ffmpeg as a subprocess. Rendering the final cut and exporting the Jianying (CapCut) draft are both tasks in the generation queue, going through a local lane `render` not bound to any provider, with global concurrency fixed at 1 and not configurable: peak rendering memory is measured in GB, and higher concurrency would blow up a Docker container. They do not call providers, so they do not write usage records (`docs/adr/0076`).

**The final cut always uses the ffmpeg bundled with `imageio-ffmpeg`, does not look at the system PATH, and offers no path configuration.** The supported deployment forms are Docker images (x86_64 and aarch64) and running directly on Windows and macOS, and `imageio-ffmpeg` ships its own static builds with libx264, libass and xfade for all of these. System ffmpeg capabilities vary: Homebrew's default build has no libass and cannot burn subtitles; on Windows it is usually not installed at all. Always using the bundled one makes final cuts rendered in every environment consistent, with no need for capability selection or item-by-item degradation. When the bundled ffmpeg cannot run in the current environment, final cut rendering is unavailable with the reason stated, and Jianying draft export is unaffected. Other places in the repository that call ffmpeg obtain it through the same locator; it does not include ffprobe, so media probing is handled separately. Likewise, an OFL-licensed Chinese font is bundled in the repository for subtitles, with no dependence on system fonts.

**The final cut and Jianying (CapCut) draft are written to disk inside the project directory and registered in the artifact manifest.** The artifact identity is "episode + edit timeline + narration version", with the final cut additionally keyed by whether subtitles are burned in (`docs/adr/0089`, `docs/adr/0090`). The generation basis snapshots the revision specified when rendering starts; if the edit timeline is changed during rendering, the artifact is honestly judged stale as soon as it comes out. Parameters that only affect the form landing on the user's machine, such as the user's local Jianying draft directory and Jianying version, are applied only at download and are not part of the artifact identity or generation basis. Each artifact identity keeps only the latest file; project archives do not include final cuts or Jianying drafts, which are judged missing after import and can simply be re-rendered.

## Explicitly rejected

- **Look up "explicit configuration → PATH → bundled binary" and select by capability**: introduces capability probing and inconsistent output for a need that barely exists (choosing a newer version yourself, hardware encoding).
- **Degrade item by item for missing capabilities** (overlay subtitles as images when there is no libass, fall back to hard cuts when there is no xfade): the final cut would silently diverge from the Jianying draft, violating the same-source principle for the two.
- **MoviePy, MLT, GES, Remotion and browser-side rendering**: excluded respectively for dependency conflicts and performance, not being installable via pip, licensing and per-render billing, and encoding capability and performance.
- **Schedule the final cut on a provider lane or make concurrency configurable**: there is no provider to attach to, and peak memory dictates that concurrency can only be 1.
- **Keep generating Jianying drafts on the fly and deleting them after download**: currency cannot be determined, and it does not fit the definition of an artifact.
- **Keep historical version files of the final cut**: a final cut can be re-rendered locally from the edit timeline revision and material at no cost, while its size is in the hundreds of MB; video unit videos are paid generations, which is why all their versions are kept.

## Consequences

- `imageio-ffmpeg` is stuck at FFmpeg 7.x and has had no new release since 2025-01; replacing it only requires changing the locator. The bundled ffmpeg and font are both redistributions, and releases include the corresponding license notices and how to obtain the source code.
- The bundled libass does not wrap Chinese lines, so whole sentences run off the screen; when generating ASS, line breaks are inserted explicitly into Chinese subtitles based on available width and font size, while English and Vietnamese are left to libass to wrap at spaces. Jianying wraps automatically, and the two sides are not required to break lines at the same positions.
- The rendered final cut follows the edit timeline: transitions do not change the total duration (`docs/adr/0090`), the visual effects of the two sides are tested item by item, and transitions that do not match are removed from the vocabulary.
