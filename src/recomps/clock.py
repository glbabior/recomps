"""Local time for display, UTC for everything else.

Runs are stamped, stored and named in UTC, and that stays true: an archive
directory has to sort chronologically, compare against another run, and mean
the same thing on any machine. None of that survives a local stamp crossing a
daylight-saving boundary.

But a person reading "the run of 2026-09-05 05:30" when they started it at
half past ten the previous evening is being told something false about their
own afternoon. Worse, the *date* is wrong by a day, and a date is what someone
uses to find a run again.

So the conversion happens at the edge, on the way to a screen or a page, and
nowhere else. A naive datetime is assumed to be UTC, because that is what this
project writes -- guessing local for a stored stamp would silently shift every
archived run by the offset.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def to_local(moment: datetime) -> datetime:
    """The same instant, in this machine's timezone."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone()


def local_date(moment: datetime) -> date:
    """The calendar date this instant fell on locally.

    Not `moment.date()`: an evening run in a western timezone is already
    tomorrow in UTC, and the workbook would date itself a day ahead.
    """
    return to_local(moment).date()


#: Twelve-hour, because that is how the owner reads a clock. Written out
#: rather than using a %-I / %#I directive, which differ between platforms.
CLOCK_FORMAT = "%Y-%m-%d %I:%M %p"


def local_stamp(moment: datetime, fmt: str = CLOCK_FORMAT) -> str:
    """A run's time as someone who was there would write it."""
    local = to_local(moment)
    text = local.strftime(fmt)
    # "01:40 PM" reads as a timestamp; "1:40 PM" reads as a time. Done by
    # substitution rather than a %-I / %#I directive, which is not portable.
    if "%I" in fmt and local.strftime("%I").startswith("0"):
        text = text.replace(local.strftime("%I"), local.strftime("%I")[1:], 1)
    return text
