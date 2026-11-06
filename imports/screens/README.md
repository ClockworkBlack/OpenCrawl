# Screenshot inbox

Drop screenshots here and the next workflow run reads them into the **Screenshots box** (`screenshots.json` in
the export bundle).

- `ordinances/`  ordinances, resolutions, codes, public-hearing notices, agendas, minutes
- `posts/`       social media posts (Facebook, Instagram, X, Bluesky, Nextdoor, YouTube...)
- `text/`        anything else with text worth keeping (flyers, mailers, web pages)
- this folder    not sure? leave it here and the box is chosen from what the screenshot shows

From a phone: open this folder on github.com (browser), tap the box folder, then
**Add file > Upload files**, pick the screenshots, and **Commit changes**.

What happens to each screenshot: duplicates are skipped; the phone's status and navigation
bars are removed; dark mode is handled; long scrolls are read in tiles and consecutive shots of
one scroll are merged; the platform, account handle, dates, likes/views and any visible URL are
picked out; the target it is about and any subjects named in it are matched.
Processed files move to `processed/`. Records received by email through `opencrawl records` are
filed into `ordinances/` the same way, named `<target id>__<file>`.

A screenshot is not an opened page, so every item stays **needs review**: open the original
post or page before citing it. A visible post URL is checked against the verified-accounts list.
