---
name: DueDeck
slug: payment-reminder-dashboard
lead: jrmckeever13
contributors: []
status: active
started: 2026-10-06
shipped:
summary: A simple Streamlit dashboard for tracking bills, subscriptions, credit card payments, due dates, recurring payments, and payment history.

models: []
stack: [python, streamlit, pandas]

data_tier: synthetic
environment: docker

sandbox:
  isolated: true
  disposable: true
  fake_inputs: true
  capped: true
  watched: true

approval_actions: []
unattended_runs: false
officer_review:
spend_cap_usd: 0
spend_to_date_usd: 0

kill_switch: Stop the Streamlit process with Ctrl+C or stop the Docker container.
logs: No external logs are collected; local payment history is stored in ignored CSV files.
teardown: not-started
incidents: []
---

# DueDeck

DueDeck is a simple payment-tracking dashboard built with Python and Streamlit.

It gives users one place to keep track of bills, subscriptions, and credit card payments without having to jump between banking apps, emails, calendars, statements, and notes.

## Status

The core DueDeck dashboard is working.

Current functionality includes:

- Adding payments
- Editing payments
- Deleting payments
- Marking payments as paid
- Tracking recurring payments
- Tracking completed one-time payments
- Payment history
- Search and filtering
- Sorting
- Dashboard summaries
- CSV export

The project currently uses local CSV files for storage and fake sample information for demonstration.

## Features

Users can:

- Add bills, subscriptions, and credit card payments
- Add an optional estimated amount
- Set a due date
- Choose whether a payment happens once, weekly, monthly, or yearly
- Add optional notes
- See what is due today, coming up soon, or overdue
- Search payments by name or notes
- Filter payments by type and due date
- Sort payments by due date, estimated amount, or name
- Mark payments as paid
- Automatically move recurring payments to their next due date
- View payment history
- View completed one-time payments
- Edit or delete existing payments
- Download payment data and payment history as CSV files

## Dashboard

DueDeck includes an "At a Glance" section that shows:

- Total estimated upcoming payments
- Estimated payments due within the next 7 days
- Estimated payments due within the next 30 days
- Estimated monthly recurring costs
- Number of payments due today
- Number of overdue payments

## Recurring Payments

When a recurring payment is marked as paid, DueDeck:

1. Records the payment in Payment History
2. Calculates the next due date
3. Keeps the payment active for the next billing cycle

Supported recurrence options:

- Once
- Weekly
- Monthly
- Yearly

Payments set to `Once` move to Completed Items after being marked paid.

## Technology

DueDeck uses:

- Python
- Streamlit
- pandas
- CSV files for local data storage

No AI model or external API is currently required to run the application.

## How to Run It

From the DueDeck project folder:

```bash
pip install -r requirements.txt
python -m streamlit run app.py

## Data and Privacy

This project is intended for demonstration and learning purposes.

Only fake or sample financial data should be used in the repository.

Do not commit:

- Real financial information
- Account numbers
- Passwords
- API keys
- Personal information
- Client information

## Future Improvements

Possible future additions include:

- Custom recurrence schedules
- Calendar view
- Due-date notifications
- Additional spending analytics
- Cloud-based storage
- User accounts
- Optional AI features for categorization or payment insights

## Project Purpose

DueDeck was created as an Isenberg AI Lab project to explore how a simple software tool can solve an everyday organizational problem while providing hands-on experience with Python, Streamlit, data storage, interface design, and iterative development.