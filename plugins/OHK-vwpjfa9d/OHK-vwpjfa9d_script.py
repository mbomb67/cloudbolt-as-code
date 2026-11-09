"""
Generate options for 'Maintenance Window' (Generated Parameter Options plugin).

Fills the maintenance_window dropdown on the Standard Server blueprint with
weekly two-hour windows computed in Python: an early-morning and a late-night
start on each day of the week, in UTC. Change DAYS, START_HOURS or
LENGTH_HOURS to match the customer's change calendar.
"""

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
START_HOURS = [2, 22]
LENGTH_HOURS = 2


def _window(day, start):
    return "{} {:02d}:00-{:02d}:00 UTC".format(day, start, (start + LENGTH_HOURS) % 24)


def get_options_list(field, environment=None, group=None, **kwargs):
    options = [(_window(day, start), _window(day, start)) for day in DAYS for start in START_HOURS]
    return {"options": options, "initial_value": options[0][0]}
