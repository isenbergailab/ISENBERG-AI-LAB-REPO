import streamlit as st
import pandas as pd
from datetime import date, timedelta
from pathlib import Path
from calendar import monthrange


# --------------------------------------------------
# APP SETUP
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

DATA_FILE = BASE_DIR / "payments.csv"
OLD_DATA_FILE = BASE_DIR / "reminders.csv"
HISTORY_FILE = BASE_DIR / "payment_history.csv"

PAYMENT_COLUMNS = [
    "type",
    "name",
    "due_date",
    "estimated_amount",
    "repeats",
    "notes",
    "paid",
]

HISTORY_COLUMNS = [
    "name",
    "type",
    "amount",
    "paid_date",
    "original_due_date",
]


st.set_page_config(
    page_title="DueDeck",
    page_icon="💸",
    layout="wide",
    initial_sidebar_state="expanded"
)


# --------------------------------------------------
# VISUAL STYLE
# --------------------------------------------------

st.markdown(
    """
    <style>

    /* ----------------------------------
       OVERALL PAGE
    ---------------------------------- */

    .stApp {
        background-color: #F6F8FB;
        color: #25364A;
        color-scheme: light;
    }

    [data-testid="stAppViewContainer"] {
        background-color: #F6F8FB;
    }

    [data-testid="stHeader"] {
        background-color: #F6F8FB;
    }

    .block-container {
        max-width: 1450px;
        padding-top: 2rem;
        padding-left: 3rem;
        padding-right: 3rem;
        padding-bottom: 4rem;
    }


    /* ----------------------------------
       SIDEBAR
    ---------------------------------- */

    [data-testid="stSidebar"] {
        background-color: #EAF0F6;
        border-right: 1px solid #CBD5E1;
    }

    [data-testid="stSidebarContent"] {
        padding-top: 1.5rem;
    }


    /* ----------------------------------
       TEXT
    ---------------------------------- */

    h1 {
        color: #24364B !important;
        font-weight: 760 !important;
        letter-spacing: -0.7px;
    }

    h2,
    h3,
    h4 {
        color: #2E4158 !important;
        font-weight: 700 !important;
    }

    p,
    label,
    span {
        color: #40536A;
    }

    small {
        color: #718096 !important;
    }


    /* ----------------------------------
       EXPANDERS
    ---------------------------------- */

    [data-testid="stExpander"] {
        background-color: #FFFFFF;
        border: 1px solid #CBD5E1;
        border-radius: 14px;
        box-shadow: 0px 3px 10px rgba(37, 55, 79, 0.05);
        overflow: hidden;
    }

    [data-testid="stExpander"] summary {
        background-color: #FFFFFF !important;
        color: #2E4158 !important;
    }


    /* ----------------------------------
       METRIC CARDS
    ---------------------------------- */

    [data-testid="stMetric"] {
        background-color: #FFFFFF;
        border: 1px solid #CBD5E1;
        border-radius: 14px;
        padding: 18px;
        box-shadow: 0px 3px 10px rgba(37, 55, 79, 0.05);
    }

    [data-testid="stMetricLabel"] {
        color: #718096 !important;
    }

    [data-testid="stMetricValue"] {
        color: #24364B !important;
        font-weight: 760;
    }


    /* ----------------------------------
       PAYMENT CARDS
    ---------------------------------- */

    [data-testid="stVerticalBlockBorderWrapper"] {
        background-color: #FFFFFF !important;
        border: 1px solid #CBD5E1 !important;
        border-radius: 14px !important;
        box-shadow: 0px 3px 10px rgba(37, 55, 79, 0.05);
    }


    /* ----------------------------------
       ALL STANDARD INPUT BOXES
       White + black outline
    ---------------------------------- */

    input,
    textarea {
        background-color: #FFFFFF !important;
        color: #1F2937 !important;
        -webkit-text-fill-color: #1F2937 !important;
        border-radius: 8px !important;
    }

    input::placeholder,
    textarea::placeholder {
        color: #9CA3AF !important;
        -webkit-text-fill-color: #9CA3AF !important;
    }


    /* Text inputs */

    [data-baseweb="input"] {
        background-color: #FFFFFF !important;
        border: 1px solid #111827 !important;
        border-radius: 8px !important;
        box-shadow: none !important;
    }

    [data-baseweb="input"] input {
        background-color: #FFFFFF !important;
        color: #1F2937 !important;
        -webkit-text-fill-color: #1F2937 !important;
        border: none !important;
    }


    /* Text areas */

    [data-baseweb="textarea"] {
        background-color: #FFFFFF !important;
        border: 1px solid #111827 !important;
        border-radius: 8px !important;
        box-shadow: none !important;
    }

    [data-baseweb="textarea"] textarea {
        background-color: #FFFFFF !important;
        color: #1F2937 !important;
        -webkit-text-fill-color: #1F2937 !important;
        border: none !important;
    }


    /* Select boxes */

    [data-baseweb="select"] > div {
        background-color: #FFFFFF !important;
        color: #1F2937 !important;
        border: 1px solid #111827 !important;
        border-radius: 8px !important;
        box-shadow: none !important;
    }

    [data-baseweb="select"] span {
        color: #1F2937 !important;
    }

    [data-baseweb="select"] svg {
        color: #1F2937 !important;
        fill: #1F2937 !important;
    }


    /* ----------------------------------
       DATE FIELD
       Keep date + icon clearly visible
    ---------------------------------- */

    [data-testid="stDateInput"] > div {
        background-color: transparent !important;
    }

    [data-testid="stDateInput"] [data-baseweb="input"] {
        background-color: #FFFFFF !important;
        border: 1px solid #111827 !important;
        border-radius: 8px !important;
        box-shadow: none !important;
    }

    [data-testid="stDateInput"] input {
        background-color: #FFFFFF !important;
        color: #1F2937 !important;
        -webkit-text-fill-color: #1F2937 !important;
        border: none !important;
    }

    [data-testid="stDateInput"] svg {
        color: #1F2937 !important;
        fill: #1F2937 !important;
    }


    /* ----------------------------------
       CHECKBOX
       Familiar real checkbox appearance
    ---------------------------------- */

    [data-testid="stCheckbox"] label {
        color: #1F2937 !important;
        font-weight: 500;
        cursor: pointer;
    }

    [data-testid="stCheckbox"] label p {
        color: #1F2937 !important;
    }

    [data-testid="stCheckbox"] [data-baseweb="checkbox"] > div {
        background-color: #FFFFFF !important;
        border: 1.5px solid #111827 !important;
        border-radius: 4px !important;
    }

    [data-testid="stCheckbox"] label:hover
    [data-baseweb="checkbox"] > div {
        background-color: #F3F6FA !important;
        border-color: #111827 !important;
    }

    [data-testid="stCheckbox"] input:checked + div {
        background-color: #3B76C5 !important;
        border-color: #3B76C5 !important;
    }

    [data-testid="stCheckbox"] svg {
        color: #FFFFFF !important;
        fill: #FFFFFF !important;
    }


    /* ----------------------------------
       PRIMARY ACTION BUTTONS
    ---------------------------------- */

    div.stButton > button[kind="primary"] {
        background-color: #3B76C5 !important;
        color: #FFFFFF !important;
        border: 1px solid #3B76C5 !important;
        border-radius: 9px;
        font-weight: 600;
    }

    div.stButton > button[kind="primary"]:hover {
        background-color: #3269B2 !important;
        color: #FFFFFF !important;
        border-color: #3269B2 !important;
    }


    /* ----------------------------------
       NORMAL BUTTONS
    ---------------------------------- */

    div.stButton > button:not([kind="primary"]) {
        background-color: #FFFFFF !important;
        color: #2E4158 !important;
        border: 1px solid #111827 !important;
        border-radius: 9px;
        font-weight: 550;
    }

    div.stButton > button:not([kind="primary"]):hover {
        background-color: #EEF4FA !important;
        color: #2E5E98 !important;
        border-color: #2E5E98 !important;
    }


    /* ----------------------------------
       DOWNLOAD BUTTONS
    ---------------------------------- */

    div.stDownloadButton > button {
        width: 100%;
        background-color: #FFFFFF !important;
        color: #2E4158 !important;
        border: 1px solid #111827 !important;
        border-radius: 9px;
        font-weight: 550;
    }

    div.stDownloadButton > button:hover {
        background-color: #EEF4FA !important;
        color: #2E5E98 !important;
        border-color: #2E5E98 !important;
    }


    /* ----------------------------------
       INFO / ALERTS
    ---------------------------------- */

    [data-testid="stAlert"] {
        background-color: #FFFFFF !important;
        color: #344A66 !important;
        border: 1px solid #CBD5E1;
        border-radius: 12px;
    }


    /* ----------------------------------
       TABLE
    ---------------------------------- */

    [data-testid="stDataFrame"] {
        background-color: #FFFFFF;
        border-radius: 12px;
        overflow: hidden;
    }


    /* ----------------------------------
       DIVIDERS
    ---------------------------------- */

    hr {
        border-color: #CBD5E1 !important;
    }


    /* ----------------------------------
       FOOTER
    ---------------------------------- */

    .duedeck-footer {
        color: #8291A3;
        font-size: 0.85rem;
        text-align: center;
        margin-top: 2rem;
        padding-top: 1rem;
    }

    </style>
    """,
    unsafe_allow_html=True
)


# --------------------------------------------------
# DATA FUNCTIONS
# --------------------------------------------------

def load_payments():

    file_to_use = DATA_FILE

    if not DATA_FILE.exists() and OLD_DATA_FILE.exists():
        file_to_use = OLD_DATA_FILE

    if not file_to_use.exists():
        return []

    try:
        df = pd.read_csv(file_to_use)

    except pd.errors.EmptyDataError:
        return []

    if df.empty:
        return []

    payments = []

    for _, row in df.iterrows():

        amount = row.get(
            "estimated_amount",
            None
        )

        if pd.isna(amount):
            amount = None

        else:
            amount = float(amount)


        notes = row.get(
            "notes",
            ""
        )

        if pd.isna(notes):
            notes = ""


        paid = str(
            row.get(
                "paid",
                False
            )
        ).lower() in [
            "true",
            "1",
            "yes"
        ]


        repeat_value = row.get(
            "repeats",
            "Once"
        )


        # Compatibility with older data
        if repeat_value == "One-time":
            repeat_value = "Once"


        payments.append(
            {
                "type": row.get(
                    "type",
                    "Bill"
                ),

                "name": str(
                    row.get(
                        "name",
                        "Unnamed"
                    )
                ),

                "due_date": pd.to_datetime(
                    row["due_date"]
                ).date(),

                "estimated_amount": amount,

                "repeats": repeat_value,

                "notes": str(notes),

                "paid": paid,
            }
        )

    return payments


def save_payments():

    df = pd.DataFrame(
        st.session_state.payments,
        columns=PAYMENT_COLUMNS
    )

    df.to_csv(
        DATA_FILE,
        index=False
    )


def load_history():

    if not HISTORY_FILE.exists():
        return []

    try:
        df = pd.read_csv(
            HISTORY_FILE
        )

    except pd.errors.EmptyDataError:
        return []

    if df.empty:
        return []

    return df.to_dict(
        orient="records"
    )


def save_history():

    df = pd.DataFrame(
        st.session_state.history,
        columns=HISTORY_COLUMNS
    )

    df.to_csv(
        HISTORY_FILE,
        index=False
    )


def record_payment(payment):

    st.session_state.history.append(
        {
            "name": payment["name"],

            "type": payment["type"],

            "amount": (
                payment[
                    "estimated_amount"
                ]
            ),

            "paid_date": date.today(),

            "original_due_date": (
                payment[
                    "due_date"
                ]
            ),
        }
    )

    save_history()


# --------------------------------------------------
# DATE FUNCTIONS
# --------------------------------------------------

def add_months(
    original_date,
    months
):

    month_index = (
        original_date.month
        - 1
        + months
    )

    new_year = (
        original_date.year
        + month_index // 12
    )

    new_month = (
        month_index % 12
        + 1
    )

    last_day = monthrange(
        new_year,
        new_month
    )[1]

    new_day = min(
        original_date.day,
        last_day
    )

    return date(
        new_year,
        new_month,
        new_day
    )


def get_next_due_date(
    current_date,
    frequency
):

    if frequency == "Weekly":

        return (
            current_date
            + timedelta(days=7)
        )


    if frequency == "Monthly":

        return add_months(
            current_date,
            1
        )


    if frequency == "Yearly":

        return add_months(
            current_date,
            12
        )


    return current_date


# --------------------------------------------------
# DISPLAY FUNCTIONS
# --------------------------------------------------

def get_status(
    due_date
):

    days_left = (
        due_date
        - date.today()
    ).days


    if days_left < 0:

        return (
            f"🔴 Overdue by "
            f"{abs(days_left)} day(s)"
        )


    if days_left == 0:

        return "🟠 Due today"


    if days_left == 1:

        return "🟡 Due tomorrow"


    if days_left <= 7:

        return (
            f"🟡 Due in "
            f"{days_left} days"
        )


    return (
        f"🟢 Due in "
        f"{days_left} days"
    )


def get_type_icon(
    payment_type
):

    icons = {
        "Bill": "🧾",
        "Subscription": "🔁",
        "Credit Card": "💳",
    }

    return icons.get(
        payment_type,
        "💵"
    )


def matches_time_filter(
    payment,
    time_filter
):

    days_left = (
        payment["due_date"]
        - date.today()
    ).days


    if time_filter == "All":

        return True


    if time_filter == "Overdue":

        return days_left < 0


    if time_filter == "Due Today":

        return days_left == 0


    if time_filter == "Next 7 Days":

        return (
            0
            <= days_left
            <= 7
        )


    if time_filter == "Next 30 Days":

        return (
            0
            <= days_left
            <= 30
        )


    return True


# --------------------------------------------------
# NEW FEATURE:
# ESTIMATED MONTHLY RECURRING COST
# --------------------------------------------------

def monthly_recurring_estimate(
    payments
):

    monthly_total = 0.0


    for payment in payments:

        amount = payment[
            "estimated_amount"
        ]


        if amount is None:

            continue


        frequency = payment[
            "repeats"
        ]


        if frequency == "Weekly":

            # 52 weeks / 12 months
            monthly_total += (
                amount * 52 / 12
            )


        elif frequency == "Monthly":

            monthly_total += amount


        elif frequency == "Yearly":

            monthly_total += (
                amount / 12
            )


    return monthly_total


# --------------------------------------------------
# SESSION STATE
# --------------------------------------------------

if "payments" not in st.session_state:

    st.session_state.payments = (
        load_payments()
    )


if "history" not in st.session_state:

    st.session_state.history = (
        load_history()
    )


if "editing_index" not in st.session_state:

    st.session_state.editing_index = None


if "delete_index" not in st.session_state:

    st.session_state.delete_index = None


# Sidebar state

if "search_filter" not in st.session_state:

    st.session_state.search_filter = ""


if "type_filter" not in st.session_state:

    st.session_state.type_filter = "All"


if "time_filter" not in st.session_state:

    st.session_state.time_filter = "All"


if "sort_filter" not in st.session_state:

    st.session_state.sort_filter = (
        "Soonest Due"
    )


# --------------------------------------------------
# SIDEBAR
# --------------------------------------------------

st.sidebar.title(
    "💸 DueDeck"
)

st.sidebar.caption(
    "Filter and search"
)

st.sidebar.divider()


search_term = (
    st.sidebar.text_input(
        "Search",
        placeholder="Name or notes",
        key="search_filter"
    )
)


type_filter = (
    st.sidebar.selectbox(
        "Type",
        [
            "All",
            "Bill",
            "Subscription",
            "Credit Card"
        ],
        key="type_filter"
    )
)


time_filter = (
    st.sidebar.selectbox(
        "When",
        [
            "All",
            "Due Today",
            "Overdue",
            "Next 7 Days",
            "Next 30 Days"
        ],
        key="time_filter"
    )
)


sort_option = (
    st.sidebar.selectbox(
        "Sort",
        [
            "Soonest Due",
            "Highest Amount",
            "Name"
        ],
        key="sort_filter"
    )
)


if st.sidebar.button(
    "Clear Filters"
):

    st.session_state.search_filter = ""

    st.session_state.type_filter = "All"

    st.session_state.time_filter = "All"

    st.session_state.sort_filter = (
        "Soonest Due"
    )

    st.rerun()


st.sidebar.divider()

st.sidebar.subheader(
    "Your Data"
)


payments_export = (
    pd.DataFrame(
        st.session_state.payments,
        columns=PAYMENT_COLUMNS
    ).to_csv(
        index=False
    )
)


st.sidebar.download_button(
    "⬇️ Download Current List",
    payments_export,
    file_name="duedeck_payments.csv",
    mime="text/csv"
)


history_export = (
    pd.DataFrame(
        st.session_state.history,
        columns=HISTORY_COLUMNS
    ).to_csv(
        index=False
    )
)


st.sidebar.download_button(
    "⬇️ Download Payment History",
    history_export,
    file_name="duedeck_history.csv",
    mime="text/csv"
)


# --------------------------------------------------
# HEADER
# --------------------------------------------------

st.title(
    "💸 DueDeck"
)

st.markdown(
    """
    Keep your bills, subscriptions, and card payments in one easy spot so you
    can quickly see what's coming up, what you've already handled, and what
    might need your attention — without digging through banking apps,
    calendars, emails, statements, or random notes just to remember what
    needs to get paid.
    """
)


# --------------------------------------------------
# ADD PAYMENT
# --------------------------------------------------

with st.expander(
    "➕ Add Something",
    expanded=True
):

    col1, col2 = st.columns(2)


    with col1:

        payment_type = st.selectbox(
            "Type",
            [
                "Bill",
                "Subscription",
                "Credit Card"
            ],
            key="new_type"
        )


        name = st.text_input(
            "Name",
            placeholder=(
                "Example: Electric Bill"
            ),
            key="new_name"
        )


    with col2:

        due_date = st.date_input(
            "Due Date",
            key="new_due_date"
        )


        repeats = st.selectbox(
            "Repeats",
            [
                "Once",
                "Weekly",
                "Monthly",
                "Yearly"
            ],
            key="new_repeats"
        )


    # Familiar checkbox interaction

    use_amount = st.checkbox(
        "Estimated Amount",
        key="new_use_amount"
    )


    estimated_amount = None


    if use_amount:

        estimated_amount = (
            st.number_input(
                "Estimated Amount (Optional)",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                key="new_amount"
            )
        )


    notes = st.text_area(
        "Notes (Optional)",
        placeholder=(
            "Example: Autopay from checking"
        ),
        key="new_notes"
    )


    if st.button(
        "Add",
        type="primary"
    ):

        if not name.strip():

            st.warning(
                "Give this item a name first."
            )


        else:

            st.session_state.payments.append(
                {
                    "type": payment_type,

                    "name": name.strip(),

                    "due_date": due_date,

                    "estimated_amount": (
                        estimated_amount
                    ),

                    "repeats": repeats,

                    "notes": notes.strip(),

                    "paid": False,
                }
            )


            save_payments()


            st.toast(
                f"{name} added."
            )


            st.rerun()


st.divider()


# --------------------------------------------------
# ACTIVE PAYMENTS
# --------------------------------------------------

active_indices = [
    index
    for index, payment
    in enumerate(
        st.session_state.payments
    )
    if not payment["paid"]
]


# --------------------------------------------------
# SORTING
# --------------------------------------------------

if sort_option == "Soonest Due":

    active_indices.sort(
        key=lambda index:
        st.session_state.payments[
            index
        ]["due_date"]
    )


elif sort_option == "Highest Amount":

    active_indices.sort(
        key=lambda index: (
            st.session_state.payments[
                index
            ]["estimated_amount"]
            if st.session_state.payments[
                index
            ]["estimated_amount"]
            is not None
            else -1
        ),
        reverse=True
    )


elif sort_option == "Name":

    active_indices.sort(
        key=lambda index:
        st.session_state.payments[
            index
        ]["name"].lower()
    )


# --------------------------------------------------
# CALCULATIONS
# --------------------------------------------------

total_estimated = sum(
    st.session_state.payments[
        index
    ]["estimated_amount"]
    for index in active_indices
    if st.session_state.payments[
        index
    ]["estimated_amount"] is not None
)


next_7_total = sum(
    st.session_state.payments[
        index
    ]["estimated_amount"]
    for index in active_indices
    if (
        st.session_state.payments[
            index
        ]["estimated_amount"] is not None

        and

        0 <= (
            st.session_state.payments[
                index
            ]["due_date"]
            - date.today()
        ).days <= 7
    )
)


next_30_total = sum(
    st.session_state.payments[
        index
    ]["estimated_amount"]
    for index in active_indices
    if (
        st.session_state.payments[
            index
        ]["estimated_amount"] is not None

        and

        0 <= (
            st.session_state.payments[
                index
            ]["due_date"]
            - date.today()
        ).days <= 30
    )
)


overdue_count = sum(
    1
    for index in active_indices
    if st.session_state.payments[
        index
    ]["due_date"]
    < date.today()
)


# NEW FEATURE:
# Number due today

due_today_count = sum(
    1
    for index in active_indices
    if st.session_state.payments[
        index
    ]["due_date"]
    == date.today()
)


active_payments = [
    st.session_state.payments[index]
    for index in active_indices
]


recurring_monthly = (
    monthly_recurring_estimate(
        active_payments
    )
)


paid_this_month = 0.0


for history_item in (
    st.session_state.history
):

    try:

        paid_date = pd.to_datetime(
            history_item["paid_date"]
        ).date()


        amount = history_item.get(
            "amount",
            None
        )


        if (
            paid_date.year
            == date.today().year

            and

            paid_date.month
            == date.today().month

            and

            amount is not None

            and

            not pd.isna(amount)
        ):

            paid_this_month += float(
                amount
            )


    except Exception:

        pass


# --------------------------------------------------
# AT A GLANCE
# --------------------------------------------------

st.subheader(
    "At a Glance"
)


row1_col1, row1_col2, row1_col3 = (
    st.columns(3)
)


row1_col1.metric(
    "Coming Up",
    f"${total_estimated:,.2f}"
)


row1_col2.metric(
    "Next 7 Days",
    f"${next_7_total:,.2f}"
)


row1_col3.metric(
    "Next 30 Days",
    f"${next_30_total:,.2f}"
)


row2_col1, row2_col2, row2_col3 = (
    st.columns(3)
)


row2_col1.metric(
    "Monthly Recurring Est.",
    f"${recurring_monthly:,.2f}"
)


row2_col2.metric(
    "Due Today",
    due_today_count
)


row2_col3.metric(
    "Overdue",
    overdue_count
)


st.caption(
    f"{len(active_indices)} item"
    f"{'' if len(active_indices) == 1 else 's'} on deck"
    f" • ${paid_this_month:,.2f} paid this month"
)


st.divider()


# --------------------------------------------------
# APPLY FILTERS
# --------------------------------------------------

filtered_indices = []


for index in active_indices:

    payment = (
        st.session_state.payments[
            index
        ]
    )


    search_text = (
        payment["name"]
        + " "
        + payment.get(
            "notes",
            ""
        )
    ).lower()


    search_match = (
        search_term.lower().strip()
        in search_text
    )


    type_match = (
        type_filter == "All"

        or

        payment["type"]
        == type_filter
    )


    time_match = (
        matches_time_filter(
            payment,
            time_filter
        )
    )


    if (
        search_match
        and type_match
        and time_match
    ):

        filtered_indices.append(
            index
        )


# --------------------------------------------------
# WHAT'S ON DECK
# --------------------------------------------------

st.subheader(
    "What's On Deck"
)


if not filtered_indices:

    st.info(
        "You're all clear — nothing's on deck for these filters."
    )


else:

    for actual_index in filtered_indices:

        payment = (
            st.session_state.payments[
                actual_index
            ]
        )


        icon = get_type_icon(
            payment["type"]
        )


        status = get_status(
            payment["due_date"]
        )


        if (
            payment[
                "estimated_amount"
            ]
            is None
        ):

            amount_text = (
                "No estimate"
            )


        else:

            amount_text = (
                f"${payment['estimated_amount']:,.2f} estimated"
            )


        with st.container(
            border=True
        ):

            left, right = st.columns(
                [5, 1]
            )


            with left:

                st.markdown(
                    f"### {icon} "
                    f"{payment['name']}"
                )


                st.write(
                    f"**{payment['type']}** "
                    f"• {amount_text} "
                    f"• {payment['repeats']}"
                )


                st.write(
                    status
                )


                st.caption(
                    "Due "
                    + payment[
                        "due_date"
                    ].strftime(
                        "%B %d, %Y"
                    )
                )


                if payment.get(
                    "notes",
                    ""
                ).strip():

                    st.caption(
                        "📝 "
                        + payment["notes"]
                    )


            with right:

                if st.button(
                    "✏️ Edit",
                    key=f"edit_{actual_index}"
                ):

                    st.session_state.editing_index = (
                        actual_index
                    )

                    st.rerun()


                if st.button(
                    "✅ Paid",
                    key=f"paid_{actual_index}"
                ):

                    record_payment(
                        payment
                    )


                    if (
                        payment["repeats"]
                        == "Once"
                    ):

                        payment["paid"] = True


                    else:

                        payment[
                            "due_date"
                        ] = get_next_due_date(
                            payment[
                                "due_date"
                            ],
                            payment[
                                "repeats"
                            ]
                        )


                    save_payments()

                    st.rerun()


                if (
                    st.session_state.delete_index
                    != actual_index
                ):

                    if st.button(
                        "🗑️ Delete",
                        key=f"delete_{actual_index}"
                    ):

                        st.session_state.delete_index = (
                            actual_index
                        )

                        st.rerun()


                else:

                    st.warning(
                        "Delete this item?"
                    )


                    if st.button(
                        "Yes, delete",
                        key=f"confirm_{actual_index}"
                    ):

                        del (
                            st.session_state.payments[
                                actual_index
                            ]
                        )


                        st.session_state.delete_index = None


                        st.session_state.editing_index = None


                        save_payments()

                        st.rerun()


                    if st.button(
                        "Cancel",
                        key=f"cancel_delete_{actual_index}"
                    ):

                        st.session_state.delete_index = None

                        st.rerun()


        # ------------------------------------------
        # EDIT PAYMENT
        # ------------------------------------------

        if (
            st.session_state.editing_index
            == actual_index
        ):

            with st.container(
                border=True
            ):

                st.markdown(
                    "#### ✏️ Edit"
                )


                edit_type = st.selectbox(
                    "Type",
                    [
                        "Bill",
                        "Subscription",
                        "Credit Card"
                    ],
                    index=[
                        "Bill",
                        "Subscription",
                        "Credit Card"
                    ].index(
                        payment["type"]
                    ),
                    key=f"edit_type_{actual_index}"
                )


                edit_name = st.text_input(
                    "Name",
                    value=payment["name"],
                    key=f"edit_name_{actual_index}"
                )


                edit_due_date = (
                    st.date_input(
                        "Due Date",
                        value=payment[
                            "due_date"
                        ],
                        key=f"edit_due_{actual_index}"
                    )
                )


                repeat_options = [
                    "Once",
                    "Weekly",
                    "Monthly",
                    "Yearly"
                ]


                current_repeat = (
                    payment["repeats"]
                    if payment["repeats"]
                    in repeat_options
                    else "Once"
                )


                edit_repeats = (
                    st.selectbox(
                        "Repeats",
                        repeat_options,
                        index=repeat_options.index(
                            current_repeat
                        ),
                        key=f"edit_repeat_{actual_index}"
                    )
                )


                edit_use_amount = (
                    st.checkbox(
                        "Estimated Amount",
                        value=(
                            payment[
                                "estimated_amount"
                            ]
                            is not None
                        ),
                        key=f"edit_amount_check_{actual_index}"
                    )
                )


                edit_amount = None


                if edit_use_amount:

                    edit_amount = (
                        st.number_input(
                            "Estimated Amount (Optional)",
                            min_value=0.0,
                            value=(
                                payment[
                                    "estimated_amount"
                                ]
                                if payment[
                                    "estimated_amount"
                                ]
                                is not None
                                else 0.0
                            ),
                            step=1.0,
                            format="%.2f",
                            key=f"edit_amount_{actual_index}"
                        )
                    )


                edit_notes = (
                    st.text_area(
                        "Notes",
                        value=payment.get(
                            "notes",
                            ""
                        ),
                        key=f"edit_notes_{actual_index}"
                    )
                )


                save_col, cancel_col = (
                    st.columns(2)
                )


                with save_col:

                    if st.button(
                        "Save",
                        type="primary",
                        key=f"save_{actual_index}"
                    ):

                        if not edit_name.strip():

                            st.warning(
                                "Give this item a name."
                            )


                        else:

                            payment["type"] = (
                                edit_type
                            )

                            payment["name"] = (
                                edit_name.strip()
                            )

                            payment["due_date"] = (
                                edit_due_date
                            )

                            payment[
                                "estimated_amount"
                            ] = edit_amount

                            payment["repeats"] = (
                                edit_repeats
                            )

                            payment["notes"] = (
                                edit_notes.strip()
                            )


                            save_payments()


                            st.session_state.editing_index = (
                                None
                            )


                            st.rerun()


                with cancel_col:

                    if st.button(
                        "Cancel",
                        key=f"cancel_edit_{actual_index}"
                    ):

                        st.session_state.editing_index = (
                            None
                        )

                        st.rerun()


# --------------------------------------------------
# PAYMENT HISTORY
# --------------------------------------------------

st.divider()


with st.expander(
    "📚 Payment History"
):

    if not st.session_state.history:

        st.caption(
            "Once you mark something paid, you'll start seeing your history here."
        )


    else:

        history_df = pd.DataFrame(
            st.session_state.history
        )


        history_df = (
            history_df.iloc[::-1]
        )


        st.dataframe(
            history_df,
            use_container_width=True,
            hide_index=True
        )


# --------------------------------------------------
# COMPLETED ITEMS
# --------------------------------------------------

with st.expander(
    "✅ Completed Items"
):

    completed_indices = [
        index
        for index, payment
        in enumerate(
            st.session_state.payments
        )
        if payment["paid"]
    ]


    if not completed_indices:

        st.caption(
            "Nothing completed yet."
        )


    else:

        for actual_index in completed_indices:

            payment = (
                st.session_state.payments[
                    actual_index
                ]
            )


            st.write(
                f"✅ **{payment['name']}** "
                f"— {payment['type']}"
            )


            if st.button(
                "Restore",
                key=f"restore_{actual_index}"
            ):

                payment["paid"] = False

                save_payments()

                st.rerun()


# --------------------------------------------------
# FOOTER
# --------------------------------------------------

st.markdown(
    """
    <div class="duedeck-footer">
        DueDeck • simple payment tracking without the clutter
    </div>
    """,
    unsafe_allow_html=True
)