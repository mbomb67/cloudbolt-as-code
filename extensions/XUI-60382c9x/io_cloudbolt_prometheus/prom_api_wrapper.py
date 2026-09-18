import requests

from utilities.models import ConnectionInfo
from utilities.rest import RestConnection


class PromConnection(RestConnection):
    def __init__(self, ci: ConnectionInfo, username="",
                 password=""):
        super().__init__(username, password)
        self.base_url = f"{ci.protocol}://{ci.ip}:{ci.port}/api/v1"

    def __getattr__(self, item):
        if item == "get":
            return lambda url, **kwargs: requests.get(
                f"{self.base_url}{url}", auth=None, **kwargs
            )
        elif item in ["post", "delete", "put", "patch"]:
            raise NotImplementedError
        else:
            return item

    def __repr__(self):
        return f"PromConnection to {self.base_url}"
