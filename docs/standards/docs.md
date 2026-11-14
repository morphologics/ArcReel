---
paths:
  - "website/docs/**"
  - "README.md"
  - "README.en.md"
  - "CONTRIBUTING.md"
---

# User documentation

### Provider documentation describes capabilities and how to configure them; for specific models, prices and API parameters, the settings page and the provider's official documentation are authoritative

The README and the website pages explain which media types ArcReel covers, how to configure them in one place, how to choose between capabilities, and where to confirm the specifics. Paragraphs that list models, unit prices or API parameters one by one go stale every time a provider updates, and they go stale before the code does: the model catalog in the code changes, nobody goes back to update the list in the docs, and users try to configure an option that no longer exists. The README is aimed at people visiting the repository for the first time, so stale information of this kind is most conspicuous on the front page.

Allowed: when explaining behavior or billing rules specific to a model, the model may be named, for example the conversion formula for billing by video tokens; the sponsors section of the README is not provider documentation.
