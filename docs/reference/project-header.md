# Project header fields

Every `projects/<slug>/README.md` starts with a YAML header. The monthly
report script reads it. Template: `templates/PROJECT_README_TEMPLATE.md`.

| Field | Values | Notes |
|---|---|---|
| name, slug, lead | text | slug matches the folder name |
| status | proposed, active, shipped, paused, failed, archived | failed projects stay listed |
| started, shipped | YYYY-MM-DD | shipped blank until shipped |
| summary | one sentence | |
| models, stack | list | |
| data_tier | public, synthetic, personal, restricted | when unsure, pick the higher tier |
| environment | docker, vm, cloud-workspace | never a laptop's main file system |
| sandbox | five true/false values | any false needs `officer_review` |
| approval_actions | list | agent actions a member approves one at a time |
| unattended_runs | true/false | true needs `officer_review` |
| officer_review | username + date | |
| spend_cap_usd, spend_to_date_usd | number | default cap is $10 |
| kill_switch, logs | one line each | |
| teardown | not-started, done, n/a | must be done before archiving |
| incidents | list of date, summary, writeup | |
