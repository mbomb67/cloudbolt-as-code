"""
The search filters, read from a request's query string: a date window and
optional user, agent, session and free-text filters. No Django imports, so the
rules can be checked without CloudBolt.

CloudBolt stores naive appliance-local times (USE_TZ is False), so dates here
are appliance-local calendar days.
"""
import datetime

from . import config, markers

FACETS = ("user", "agent", "session")
MAX_TEXT = 200
# The user filter value for records with no owner.
NO_USER = 0


class Window(object):
    """Whole days, first_day to last_day inclusive."""

    def __init__(self, first_day, last_day):
        self.first_day = first_day
        self.last_day = last_day

    @property
    def start(self):
        return datetime.datetime.combine(self.first_day, datetime.time.min)

    @property
    def end(self):
        """Exclusive: midnight after last_day."""
        return datetime.datetime.combine(self.last_day + datetime.timedelta(days=1), datetime.time.min)

    @property
    def days(self):
        return (self.last_day - self.first_day).days + 1

    def as_json(self):
        return {"from": self.first_day.isoformat(), "to": self.last_day.isoformat(), "days": self.days}


def make_window(first_day=None, last_day=None, today=None, notices=None):
    """A window of at most MAX_DAYS; DEFAULT_DAYS ending today when no dates are given."""
    notices = notices if notices is not None else []
    today = today or datetime.date.today()
    last_day = last_day or today
    first_day = first_day or last_day - datetime.timedelta(days=config.DEFAULT_DAYS - 1)
    if first_day > last_day:
        first_day, last_day = last_day, first_day
    if (last_day - first_day).days + 1 > config.MAX_DAYS:
        first_day = last_day - datetime.timedelta(days=config.MAX_DAYS - 1)
        notices.append(
            "A search covers at most {} days, so it starts on {}.".format(config.MAX_DAYS, first_day.isoformat())
        )
    return Window(first_day, last_day)


def lookback_window(last_day):
    """The longest window ending on last_day, for finding where a session starts."""
    return Window(last_day - datetime.timedelta(days=config.MAX_DAYS - 1), last_day)


class Filters(object):
    def __init__(self, window, user=None, agent=None, session=None, text="", dates_given=False):
        self.window = window
        # An int user profile id, NO_USER for records with no owner, or None for any.
        self.user = user
        self.agent = agent
        self.session = session
        self.text = text
        self.dates_given = dates_given

    @property
    def needle(self):
        return self.text.lower()

    def get(self, facet):
        return getattr(self, facet)

    def any_set(self):
        return any(self.get(facet) is not None for facet in FACETS) or bool(self.text)

    def as_json(self):
        return {
            "from": self.window.first_day.isoformat(),
            "to": self.window.last_day.isoformat(),
            "user": "" if self.user is None else ("none" if self.user == NO_USER else str(self.user)),
            "agent": self.agent or "",
            "session": self.session or "",
            "q": self.text,
        }


def _date(value, label, notices):
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value.strip())
    except ValueError:
        notices.append("{} is not a date (YYYY-MM-DD), so it was ignored.".format(label))
        return None


def parse_filters(params, today=None):
    """Filters from a query string dict; returns (filters, notices)."""
    notices = []
    first_day = _date(params.get("from"), "From", notices)
    last_day = _date(params.get("to"), "To", notices)
    window = make_window(first_day, last_day, today=today, notices=notices)

    user = None
    raw_user = (params.get("user") or "").strip()
    if raw_user == "none":
        user = NO_USER
    elif raw_user.isdigit():
        user = int(raw_user)
    elif raw_user:
        notices.append("The user filter was not a user id, so it was ignored.")

    session = (params.get("session") or "").strip().lower() or None
    if session and not markers.is_session(session):
        notices.append("A session id is up to 12 hex characters, so the session filter was ignored.")
        session = None

    agent = (params.get("agent") or "").strip()[:MAX_TEXT] or None
    text = (params.get("q") or "").strip()[:MAX_TEXT]
    dates_given = bool(first_day or last_day)
    return Filters(window, user=user, agent=agent, session=session, text=text, dates_given=dates_given), notices
