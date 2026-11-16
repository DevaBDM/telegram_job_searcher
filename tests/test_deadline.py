"""Tests for the deadline extraction module."""

from datetime import date

import pytest

from jobsaver.deadline import extract_deadline


# ── Positive cases ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    # Explicit "Deadline:" label
    ("Deadline: November 20, 2026", date(2026, 11, 20)),
    ("Deadline: Nov 20, 2026", date(2026, 11, 20)),
    ("deadline: 2026-11-20", date(2026, 11, 20)),
    ("Deadline: 20/11/2026", date(2026, 11, 20)),
    ("Deadline: 20-11-2026", date(2026, 11, 20)),
    ("DEADLINE : 20 November 2026", date(2026, 11, 20)),

    # Apply before / by
    ("Apply before November 30, 2026", date(2026, 11, 30)),
    ("Apply by Dec 1, 2026", date(2026, 12, 1)),
    ("apply until 2027-01-15", date(2027, 1, 15)),

    # Closing date
    ("Closing date: January 5, 2027", date(2027, 1, 5)),
    ("closing date 05/01/2027", date(2027, 1, 5)),

    # Last date to apply
    ("Last date to apply: 2026-12-31", date(2026, 12, 31)),
    ("Last day of application: March 1, 2027", date(2027, 3, 1)),

    # Application deadline
    ("Application deadline: Feb 28, 2027", date(2027, 2, 28)),
    ("Application closes December 15, 2026", date(2026, 12, 15)),

    # Due date / submission
    ("Due date: 2026-11-25", date(2026, 11, 25)),
    ("Submission deadline: 30/11/2026", date(2026, 11, 30)),

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

    # Expiry
    ("Expires on 2026-12-01", date(2026, 12, 1)),
    ("Expiry date: 15/12/2026", date(2026, 12, 15)),
])
def test_extracts_deadline(text: str, expected: date) -> None:
    assert extract_deadline(text) == expected


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
