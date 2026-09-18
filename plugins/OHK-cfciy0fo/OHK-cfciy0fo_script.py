import datetime

"""
Returns an initial value of one day from now for Expiration date
"""


def get_options_list(field, environment=None, group=None, **kwargs):

    return {
        "initial_value": datetime.datetime.now() + datetime.timedelta(days=7)
    }