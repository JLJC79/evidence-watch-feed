# Evidence & Policy Watch — Crossref feed

A small, dependency-free pipeline that pulls recent journal-article records for the 14
journals tracked by the Threshold Research Group *Evidence & Policy Watch* from the Crossref
REST API once a week and publishes them as static files on GitHub Pages. The weekly
issue-building run then makes **one** fetch of `data/index.json` instead of twenty-odd calls to
`api.crossref.org` through a rate-limited proxy.

## What gets published

| File | Purpose |
|---|---|
| `data/index.json` | One entry per journal, in the fixed order the issue uses, with compact item records (DOI, title, authors, dates, open-access flag, link to the abstract file). |
| `data/index.md` | The same listing as readable markdown, for checking by eye. |
| `data/abstracts/<slug>.json` | One file per article with the full abstract (JATS tags stripped), or `"abstract": null` where the publisher deposits none. |

The pull looks back 35 days so that the issue's 21-day window is always fully covered; the
issue run applies the 21-day cut itself using each item's `online_date`.

Corrections, errata, corrigenda, retractions, editorials, book reviews and Campbell
protocols are not included in `items`; they are listed under each journal's `dropped` key so
nothing disappears silently. *Journal of Criminal Justice* records use the Crossref
`created` date as the online date and are labelled "(in press)", matching the issue's rule.

## One-time setup (about 15 minutes)

1. **Create the repository.** On GitHub, create a new **public** repository (Pages on a
   private repository needs a paid plan). A name like `evidence-watch-feed` is fine.
2. **Add these files** at the paths shown: `fetch_crossref.py`, `README.md`,
   `.github/workflows/crossref.yml`. The `data/` folder is created by the first run.
3. **Turn on GitHub Pages.** Repository *Settings → Pages → Build and deployment → Source:
   Deploy from a branch → Branch: `main`, folder `/ (root)` → Save.*
4. **Run the workflow once by hand.** *Actions → Weekly Crossref pull → Run workflow.* It takes
   about a minute. When it finishes, a `data/` folder appears in the repository and, a minute
   or two later, the files are live at

   `https://<your-github-username>.github.io/<repository-name>/data/index.json`

5. **Check the file in a browser.** You should see `"generated_at_brisbane"` near the top and
   fourteen journal entries. `data/index.md` is easier to read.
6. **Send the URL to the issue-building task** so its prompt can be updated to read the file
   instead of calling Crossref directly.

After that, the workflow runs every Sunday at 20:00 Brisbane time with no further action.
If a run fails you will get a GitHub email; the previous week's files stay in place.

## Notes

- The workflow commits with the built-in `GITHUB_TOKEN`; no secrets are needed. The
  `permissions: contents: write` line in the workflow grants that.
- Crossref's polite pool is used (the `mailto` parameter and a descriptive User-Agent). Change
  the address in the workflow's `MAILTO` variable if needed.
- The script honours `Retry-After` on 429/503 and backs off; one journal failing does not
  prevent the others from being written (`journals_failed` in `index.json` says how many did).
- `open_access` is set only when a Creative Commons licence URL is present. Publisher
  text-and-data-mining licences do not count.
- Running locally: `python fetch_crossref.py --out data --mailto you@example.org --days 35`.
