# Job collector

Every morning at about 06:20 UK time, GitHub runs `collect.py`, which gathers London marketing jobs posted in the last day and saves them in `data/`. A separate scheduled assistant reads `data/latest.md` and `data/latest.json` afterwards and does the scoring.

## Where the jobs come from

| Source | How | Needs |
|---|---|---|
| LinkedIn, Indeed, Glassdoor, Google Jobs | [JobSpy](https://github.com/speedyapply/JobSpy) (no login) | nothing |
| Reed | official API, full adverts | `REED_API_KEY` secret |
| Adzuna (also covers Totaljobs, CV-Library, company sites) | official API | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` secrets |
| Employers' own job boards (Greenhouse, Lever, Ashby) | public board feeds, career-ops style | the list in `config.yml` |

## Files

- `config.yml`: search terms, title and location filters, minimum salary, sources, employer boards. Edit this to change what gets collected.
- `data/latest.json`: today's jobs with full descriptions (`is_new: true` = posted or first seen in the last ~24h).
- `data/latest.md`: a quick list of today's new jobs.
- `data/run-report.md`: what each source returned, and any errors.
- `data/seen.json`: when each job was first seen.
- `data/archive/`: one file per day, kept for 30 days.

## Running it by hand

On GitHub: **Actions → Collect jobs → Run workflow**.
