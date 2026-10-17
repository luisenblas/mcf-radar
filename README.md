# mcf-radar

Unofficial job radar and salary tracker for [MyCareersFuture](https://www.mycareersfuture.gov.sg) (Singapore).

It reads the public JSON API that the MyCareersFuture website uses, stores every posting above a salary floor in one CSV, scores each posting against a profile you write, and writes Markdown and text reports. No LLM calls, no third-party packages.

This project is not affiliated with or endorsed by MyCareersFuture, Workforce Singapore or the Singapore government. The API is undocumented and can change or disappear. Check the site's terms of use before you run it.

## Requirements

Python 3.9 or newer. Standard library only.

## Configure

Copy the example profile and edit it:

    cp profile.example.json profile.json

The profile holds everything personal: salary floor and band (SGD per month), role families (regex on job title), target families, negative title words, boost keywords, years-of-experience limit, score weights, and a watchlist of company names (tier 1, tier 2, agency). The example is a generic product manager profile.

If `profile.json` is missing, the script falls back to `profile.example.json`.

## Usage

    python3 mcf_radar.py --backfill      # first run: pull all live postings above the floor
    python3 mcf_radar.py                 # later runs: only new postings
    python3 mcf_radar.py --report        # re-score and re-render from the CSV, no network
    python3 mcf_radar.py --out ~/Documents/mcf --profile my.json

Options: `--max-details N` caps detail requests per run (default 800), `--since-days N` with `--report` treats the last N days as new.

Output goes to `./data` by default:

- `mcf_jobs.csv`: full history, one row per posting.
- `radar.md`: new postings in your band, new postings at watchlist companies, other new relevant postings, open postings.
- `digest.txt`: a short plain-text summary.
- `salary_benchmarks.md`: median and p75 salary by role family and years of experience.
- `state.json`, `run.lock`: run state.

To run it daily, use cron or launchd.

## Be polite

The script waits 0.4 seconds between requests and uses one worker. Parallel requests trigger HTTP 429 and a multi-minute block. The first backfill makes several hundred requests, so run it once and use incremental runs after that. Do not lower the delay.

## Limitations

- Singapore postings only, and only those that publish a salary.
- Relevance scoring is regex based. Tune the profile for your own search.
- Salary figures are base monthly ranges published by employers.
- The API may change without notice.

## License

MIT. See `LICENSE`.
