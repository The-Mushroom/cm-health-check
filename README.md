# Creative Mushroom Website Health Check

Checks a website from the outside, scores it out of 100 and produces a branded A4 PDF report, ready for you to read before it goes to the client.

Scoring follows the Health Check scoring spec. All the thresholds live in `scoring.json`, so you can tune them without touching the code.

## One-time setup on your Mac

1. Open Terminal and go to this folder, for example:
   `cd ~/Documents/cm-health-check`
2. Install the tools (once). They go into a private `.venv` folder inside this folder, so nothing else on your Mac is touched:
   ```
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m playwright install chromium
   ```
3. Add your Google PageSpeed Insights key (see below). Without a key the tool still works, but speed is measured with our own rougher phone test, and the two Lighthouse scores are left out.

**Back this folder up.** Keep it in iCloud Drive, Google Drive or a private GitHub repo so a dead laptop cannot take it with it.

## Getting the free PageSpeed Insights key (about 5 minutes)

1. Go to console.cloud.google.com and sign in with the Creative Mushroom Google account.
2. Top bar: project picker, then **New project**. Name it `CM Health Check` and create it.
3. Search the top bar for **PageSpeed Insights API**, open it and click **Enable**.
4. Go to **APIs and services**, then **Credentials**, then **Create credentials**, then **API key**.
5. Click the new key, choose **Restrict key**, tick **PageSpeed Insights API** only, and save.
6. Copy `settings.example.ini` to `settings.ini` and paste the key after `pagespeed_api_key =`.

It is free (25,000 checks a day). Never paste the key into an email or chat.

## Running a check

```
.venv/bin/python health_check.py https://www.example.co.uk --name "Example Ltd"
```

Useful options:

| Option | What it does |
|---|---|
| `--business-type hospitality` | Adds the hospitality notes: PDF menus, no booking button, no newsletter sign-up, broken old maps. Not scored. |
| `--gbp match` | Your hand check of their Google Business Profile: `match`, `mismatch`, `none`, or `na` if they do not need one. Leave it out and that check is not scored. |
| `--rank "text"` | Your note on where they appear in Google, printed in the report exactly as written. |
| `--free-fix alt_text` | Choose the free fix yourself. Default `auto` picks the one costing them most. Options: meta_descriptions, alt_text, gbp, tel_links, privacy, copyright, titles, menu_pdf. |
| `--pages 25` | How many pages to crawl. |

A typical hospitality run:

```
.venv/bin/python health_check.py thesaltygull.co.uk --name "The Salty Gull" --business-type hospitality --gbp mismatch --rank "Not on page one for 'pub with rooms Falmouth'."
```

It takes two to four minutes. The PDF, a web copy and a data file land in `reports/`.

## Your 2 working day routine

1. Run the check when the request comes in.
2. Look up their Google Business Profile and Google position, then re-run with `--gbp` and `--rank`.
3. Read the PDF. If anything looks wrong, check the `.json` file next to it, which shows every measurement.
4. Send it with a personal note.

## Good to know

* It only looks from the outside, like a visitor. It never logs in or changes anything, and it waits a second between pages and respects robots.txt.
* Anything it cannot measure fairly (for example if Google's API is down) is left out, and that area is scored on what could be measured. The report says so.
* Plugin and WordPress version checks only work when the site shows version numbers publicly. Known vulnerability lookups are not included.
* The report never tells them how to fix things, apart from the one free fix.
* `tests/fixture_server.py` is a deliberately broken pub website for testing changes: run it with `python3 tests/fixture_server.py 8765`, then check `http://127.0.0.1:8765/`. It should score around 40 with all four hospitality notes.

## Automatic reports from the website form

The `automation` folder and `.github/workflows/health-check.yml` let the website's Health Check form run checks automatically on GitHub and email you each report to read before sending. Setup steps are in `automation/SETUP.md`.
