# How to publish the monthly report

Owner: VP of Operations. Covers the previous calendar month.

| When | Step |
|---|---|
| Last week of month | Remind project leads to update their README headers |
| 1st to 3rd | Run `python scripts/build_report.py YYYY-MM` |
| 1st to 3rd | Fill in the human sections: summary, how built, metrics, next month |
| 1st to 3rd | Resolve every FLAG the script prints, or explain it in the report |
| By the 5th | Open a PR. A second officer reviews and sets `reviewed_by` |
| By the 5th | Merge. Add the date to `published`. Add to `docs/reports/index.md` |
| After merge | Officer shares link on LinkedIn and emails advisor and sponsor |

## Rules

- Incidents and failed runs are always reported. "None" is a valid answer.
- Numbers come from the headers and repo, not memory.
- Only officers publish. Members are credited by GitHub username with consent.
