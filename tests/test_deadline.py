"""Tests for the deadline extraction module."""

from datetime import date

import pytest

from jobsaver.deadline import extract_deadline


# ── Positive cases: exact trigger phrases ─────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    # Deadline
    ("Deadline: November 20, 2026", date(2026, 11, 20)),
    ("Deadline: Nov 20, 2026", date(2026, 11, 20)),
    ("deadline: 2026-11-20", date(2026, 11, 20)),
    ("Deadline: 20/11/2026", date(2026, 11, 20)),
    ("Deadline: 20-11-2026", date(2026, 11, 20)),
    ("DEADLINE : 20 November 2026", date(2026, 11, 20)),

    # Apply Before / By / Until
    ("Apply before November 30, 2026", date(2026, 11, 30)),
    ("Apply by Dec 1, 2026", date(2026, 12, 1)),
    ("apply until 2027-01-15", date(2027, 1, 15)),
    ("Apply No Later Than: 2027-02-01", date(2027, 2, 1)),

    # Closing Date / Close Date / Closing On
    ("Closing date: January 5, 2027", date(2027, 1, 5)),
    ("closing date 05/01/2027", date(2027, 1, 5)),
    ("Close Date: 2026-12-31", date(2026, 12, 31)),
    ("Closing on 15/12/2026", date(2026, 12, 15)),

    # Application Close / Closed / Closes
    ("Application Close: March 1, 2027", date(2027, 3, 1)),
    ("Application Closed: Feb 28, 2027", date(2027, 2, 28)),
    ("Application Closes December 15, 2026", date(2026, 12, 15)),

    # Last Date / Last Day To Apply
    ("Last date: 2026-12-31", date(2026, 12, 31)),
    ("Last Day To Apply: March 1, 2027", date(2027, 3, 1)),

    # Due Date
    ("Due date: 2026-11-25", date(2026, 11, 25)),

    # End Date
    ("End Date: 2027-01-31", date(2027, 1, 31)),

    # Submit Before / Submit By
    ("Submit Before: 30/11/2026", date(2026, 11, 30)),
    ("Submit By 2027-03-15", date(2027, 3, 15)),

    # Cut-Off / Cutoff Date
    ("Cut-Off Date: 2027-01-10", date(2027, 1, 10)),
    ("Cutoff Date: Jan 15, 2027", date(2027, 1, 15)),

    # Expiration Date / Expires
    ("Expiration Date: 2027-06-30", date(2027, 6, 30)),
    ("Expires 2026-12-01", date(2026, 12, 1)),

    # Final Date
    ("Final Date: 20/12/2026", date(2026, 12, 20)),

    # No Later Than
    ("No Later Than 2027-02-28", date(2027, 2, 28)),

    # Open Until / Valid Until
    ("Open Until: Dec 31, 2026", date(2026, 12, 31)),
    ("Valid Until 2027-01-01", date(2027, 1, 1)),

    # Ordinal suffixes
    ("Deadline: November 20th, 2026", date(2026, 11, 20)),
    ("Apply by Dec 1st, 2026", date(2026, 12, 1)),
    ("Closing date: 3rd January 2027", date(2027, 1, 3)),

    # ISO with slashes
    ("Deadline: 2026/12/25", date(2026, 12, 25)),

    # Surrounding text
    (
        "We are hiring a Python developer!\n\n"
        "Location: Addis Ababa\nDeadline: November 20, 2026\n"
        "Apply at hr@example.com",
        date(2026, 11, 20),
    ),

    # Amharic triggers
    ("ቀነ ገደብ 2026-12-01", date(2026, 12, 1)),
    ("የማመልከቻ ቀን: 2027-01-15", date(2027, 1, 15)),
    ("የመጨረሻ ቀን 25/12/2026", date(2026, 12, 25)),
])
def test_extracts_deadline(text: str, expected: date) -> None:
    assert extract_deadline(text) == expected


# ── Fuzzy matching (misspelled triggers) ──────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    # Misspelled "Deadline"
    ("Deadlin: November 20, 2026", date(2026, 11, 20)),
    ("Dedline: 2026-11-20", date(2026, 11, 20)),
    ("Dead line 20/11/2026", date(2026, 11, 20)),

    # Misspelled "Closing Date"
    ("Closin Date: Jan 5, 2027", date(2027, 1, 5)),
    ("Clossing Date: 05/01/2027", date(2027, 1, 5)),

    # Misspelled "Apply Before"
    ("Aply Before November 30, 2026", date(2026, 11, 30)),
    ("Apply Befor: Dec 1, 2026", date(2026, 12, 1)),

    # Misspelled "Application Closes"
    ("Aplicaton Closes: 2026-12-15", date(2026, 12, 15)),

    # Misspelled "Submit Before"
    ("Submitt Before: 30/11/2026", date(2026, 11, 30)),

    # Misspelled "Expiration Date"
    ("Expiraton Date: 2027-06-30", date(2027, 6, 30)),
])
def test_fuzzy_extracts_deadline(text: str, expected: date) -> None:
    result = extract_deadline(text)
    assert result == expected


# ── Negative cases (should return None) ───────────────────────────────────
@pytest.mark.parametrize("text", [
    "",
    "We are hiring a Python developer!",
    "Posted on November 20, 2026",  # "Posted on" is not a trigger
    "Salary: $80k-$100k\nLocation: Remote",
    None,
    "Join us! Great opportunity.",
    "The date is 2026-11-20 for reference.",  # no trigger word
])
def test_no_deadline(text: str | None) -> None:
    assert extract_deadline(text) is None


# ── Edge cases ────────────────────────────────────────────────────────────
def test_invalid_date_returns_none() -> None:
    # Feb 30 doesn't exist
    assert extract_deadline("Deadline: February 30, 2026") is None


def test_first_deadline_wins() -> None:
    text = (
        "Application deadline: Jan 10, 2027.\n"
        "Final deadline: Feb 20, 2027."
    )
    assert extract_deadline(text) == date(2027, 1, 10)


def test_deadline_on_next_line() -> None:
    text = "Deadline:\n2026-12-25"
    # The trigger and date may be on separate lines — fuzzy should catch it
    result = extract_deadline(text)
    assert result == date(2026, 12, 25)
