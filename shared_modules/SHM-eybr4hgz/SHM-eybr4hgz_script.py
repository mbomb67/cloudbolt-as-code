"""
GitHub API client for CloudBolt CMP content.

The GitHubConnection class interacts with the GitHub API as either a GitHub App
or with a personal access token (PAT). It is a requests.Session subclass and is
used as a context manager. The class is ConnectionInfo-backed: it is initialized
with a connection_info_id (or, via from_connection_name, the ConnectionInfo
name) and reads its credentials from that record.

For the Bicep deployment engine this module adds:
  * get_repo_contents() / get_repo_file() — GitHub Contents API listing and raw
    file download, used by the engine's SPARSE fetch (only the template's own
    directory is pulled, so a template hosted in a huge public repo such as
    Azure/azure-quickstart-templates costs kilobytes, not hundreds of MB);
  * get_repo_archive() — the full repository tarball at a ref, the fallback
    when a template references files outside its own directory.

Auth tokens appear ONLY in the Authorization header — never in a URL, log line,
or job output (AGENTS.md cardinal rule 3 / docs/agents/rbac-and-security.md).

The connection-class shape (auth_mode + ConnectionInfo) leaves an explicit slot
for an Azure DevOps sibling module later; ADO is out of scope here.
"""
from datetime import datetime, timedelta

import requests
from jwt import encode as jwt_encode
from requests import Session

from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)

VERIFY_CERTS = True

# Bound every HTTP call so a hung GitHub request cannot pin a jobengine thread.
HTTP_TIMEOUT_S = 60

# The ConnectionInfo that holds the GitHub credential. The build/day-2 plugins
# resolve this by name so the integration is config-as-code; the token lives in
# the ConnectionInfo password (PAT) field and is re-entered after every repo
# sync (export redacts it — docs/agents/metadata-schemas.md round-trip caveat).
CONNECTION_INFO_NAME = "GitHub"

# GitHub api host. Cross-host redirects (api.github.com -> codeload.github.com)
# cause requests to strip the Authorization header automatically, which is the
# desired behavior: the redirect target is a short-lived pre-signed URL and must
# not receive the token.
GITHUB_API_BASE = "https://api.github.com"


class GitHubConnection(Session):
    """
    Manage interacting with the GitHub API as a GitHub App or with a PAT.

    Using the app's id and private pem we craft a JWT to request an access
    token; after authenticating, use the requests.Session methods to perform
    requests. Should be used as a context manager.

    Required Init Parameters:
        auth_mode: str - 'token' or 'app'
            - token mode: the ConnectionInfo password field holds the PAT
            - app mode: the ConnectionInfo username field holds the app_id and
              the private key is added as an SSH key on the ConnectionInfo
        conn_info_id: int - id of the ConnectionInfo to use

    Optional Init Parameters (app mode only; precedence
    installation_id > org > username > repo):
        installation_id, username, org, repo

    Example:
        from shared_modules.github import GitHubConnection, CONNECTION_INFO_NAME
        with GitHubConnection.from_connection_name(CONNECTION_INFO_NAME) as gh:
            data = gh.get_repo_archive("octocat/Hello-World", "main")
    """

    def __init__(self, auth_mode: str, conn_info_id: int,
                 installation_id: str = None, username: str = None,
                 org: str = None, repo: str = None):
        super(GitHubConnection, self).__init__()
        self.base_url = GITHUB_API_BASE
        self.headers.update({"Accept": "application/vnd.github+json"})
        self.auth_mode = auth_mode
        self.verify = VERIFY_CERTS
        try:
            self.conn_info = ConnectionInfo.objects.get(id=conn_info_id)
        except ConnectionInfo.DoesNotExist:
            raise Exception(
                f"ConnectionInfo with id {conn_info_id} does not exist."
            )
        if self.auth_mode == "token":
            self.token = self.conn_info.password
        else:
            self.headers.update(
                {"User-Agent": "CloudBolt GitHub Application"}
            )
            self.app_id = self.conn_info.username
            ssh_key = self.conn_info.ssh_key.sshkey.first().storedsshkey
            self.pk = ssh_key.private_key
            self.jwt = None
            self.token_expires_at = None
            # Precedence: installation_id, org, username, repo
            self.installation_id = installation_id
            self.username = username
            self.org = org
            # If used, repo should be in the format: {owner}/{repo}
            self.repo = repo

    @classmethod
    def from_connection_name(cls, name: str = CONNECTION_INFO_NAME,
                             auth_mode: str = "token", **kwargs):
        """
        Resolve a ConnectionInfo by name and build a connection. This is the
        entry point the Bicep plugins use so the integration references the
        connection by a stable name rather than a numeric id.
        """
        try:
            conn = ConnectionInfo.objects.get(name=name)
        except ConnectionInfo.DoesNotExist:
            raise Exception(
                f'ConnectionInfo "{name}" does not exist. Create it per '
                f"docs/bicep-deployment-setup.md (PAT in the password field)."
            )
        return cls(auth_mode, conn.id, **kwargs)

    def update_bearer(self, bearer_token):
        self.headers.update(
            {"Authorization": f'Bearer {bearer_token.decode("ascii")}'}
        )

    def update_auth(self, auth_token):
        self.headers.update({"Authorization": "token {}".format(auth_token)})

    def update_agent(self, agent_string):
        self.headers.update({"User-Agent": "{}".format(agent_string)})

    def create_jwt(self):
        payload = {
            # Issued at time
            "iat": int(datetime.now().timestamp()),
            # JWT expiration time (10 minute maximum)
            "exp": int((datetime.now() + timedelta(minutes=9)).timestamp()),
            # GitHub App's identifier
            "iss": self.app_id,
            "alg": "RS256",
        }
        return jwt_encode(payload, self.pk, algorithm="RS256")

    def request_token(self):
        resp = None
        if self.installation_id:
            url = (
                f"{self.base_url}/app/installations/{self.installation_id}/"
                f"access_tokens"
            )
            resp = self.post(url, timeout=HTTP_TIMEOUT_S)
            if resp.status_code == 201:
                self.update_auth(resp.json().get("token"))
                self.token_expires_at = datetime.strptime(
                    resp.json().get("expires_at"), "%Y-%m-%dT%H:%M:%SZ"
                )
            else:
                raise Exception(resp.content)

    def __enter__(self):
        if self.auth_mode == "token":
            self.headers.update({"Authorization": f"token {self.token}"})
        else:
            self.jwt = self.create_jwt()
            self.update_bearer(self.jwt)
            if not self.installation_id:
                app_installation = self.get_app_installation()
                self.installation_id = app_installation["id"]
            self.request_token()
        return self

    def __exit__(self, *args):
        self.close()

    def get_app_installation(self):
        # Per the docs, this should either be: /users/{username}/installation,
        # /orgs/{org}/installation, /repos/{owner}/{repo}/installation,
        # or /app/installations
        if self.org:
            url = f"{self.base_url}/orgs/{self.org}/installation"
        elif self.username:
            url = f"{self.base_url}/users/{self.username}/installation"
        elif self.repo:
            url = f"{self.base_url}/repos/{self.repo}/installation"
        else:
            # Fallback documented above; without it an app-mode connection with
            # no installation_id/org/username/repo would hit UnboundLocalError.
            url = f"{self.base_url}/app/installations"
        resp = self.get(url, timeout=HTTP_TIMEOUT_S)
        return resp.json()

    def list_workflows(self, repo):
        """
        Get all GitHub Apps Workflows installed on a repo.
        :param repo: in ownername/repo format. i.e. myuser/myrepo
        """
        url = f"/repos/{repo}/actions/workflows"
        return self.submit_request(url)["workflows"]

    def get_workflow(self, repo, workflow_id):
        """
        Get a GitHub Apps Workflow installed on a repo.
        :param repo: in ownername/repo format
        :param workflow_id: the id of the workflow
        """
        url = f"/repos/{repo}/actions/workflows/{workflow_id}"
        return self.submit_request(url)

    def dispatch_workflow(self, repo, workflow_id, ref, inputs=None):
        """
        Dispatch a GitHub Apps Workflow on a repo. (Retained from the base
        client; the GitHub Actions delegation execution model is a documented
        follow-up alternative — see the Bicep plan's Scope Boundaries.)
        :param repo: in ownername/repo format
        :param workflow_id: the id of the workflow
        :param ref: the branch or tag to run the workflow on
        :param inputs: a dictionary of input key value pairs
        """
        url = f"/repos/{repo}/actions/workflows/{workflow_id}/dispatches"
        data = {"ref": ref}
        if inputs:
            data["inputs"] = inputs
        return self.submit_request(url, method="post", json=data)

    def get_repo_contents(self, repo: str, path: str, ref: str = None):
        """
        Describe one path via the Contents API: a LIST of entries for a
        directory, a DICT for a single file, or None when the path does not
        exist at that ref. Each entry carries `type` ("file" | "dir" |
        "symlink" | "submodule"), `name`, `path`, `sha`, `size`.

        GitHub REST: GET /repos/{owner}/{repo}/contents/{path}?ref={ref}
        https://docs.github.com/en/rest/repos/contents#get-repository-content
        Limits (confirmed against current docs; re-verify per cardinal rule 5):
        a directory listing is capped at 1,000 entries; file bodies are only
        inlined up to 1 MB, which is why get_repo_file() uses the raw media
        type instead of the base64 `content` field.

        :param repo: "owner/repo"
        :param path: repo-relative path, "" for the root
        :param ref: branch, tag, or commit SHA; default branch when None.
        """
        url = f"{self.base_url}/repos/{repo}/contents/{path.strip('/')}"
        params = {"ref": ref} if ref else None
        resp = self.get(url, params=params, timeout=HTTP_TIMEOUT_S)
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            # Status + coordinates only — never the response body.
            raise Exception(
                f"Failed to list '{path}' in {repo}@{ref or 'default-branch'} "
                f"(HTTP {resp.status_code}). Verify the repo, ref, and that the "
                f'"{CONNECTION_INFO_NAME}" ConnectionInfo token has read access.'
            )
        return resp.json()

    def get_repo_file(self, repo: str, path: str, ref: str = None,
                      max_bytes: int = None):
        """
        Download one file's raw bytes via the Contents API with the
        `application/vnd.github.raw+json` media type (serves files up to
        100 MB; the default JSON form inlines only up to 1 MB). Streams and
        enforces `max_bytes` so an unexpectedly large file cannot exhaust the
        jobengine's memory.

        https://docs.github.com/en/rest/repos/contents#get-repository-content
        (custom media types section; re-verify per cardinal rule 5).
        """
        url = f"{self.base_url}/repos/{repo}/contents/{path.strip('/')}"
        params = {"ref": ref} if ref else None
        resp = self.get(url, params=params, stream=True, timeout=HTTP_TIMEOUT_S,
                        headers={"Accept": "application/vnd.github.raw+json"})
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            raise Exception(
                f"Failed to download '{path}' from {repo}@{ref or 'default-branch'} "
                f"(HTTP {resp.status_code})."
            )
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                resp.close()
                raise Exception(
                    f"'{path}' exceeds the {max_bytes}-byte per-file limit for a "
                    f"sparse template fetch."
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def get_repo_archive(self, repo: str, ref: str = None):
        """
        Download a repository archive (tar.gz) at a ref and return the raw
        bytes. The caller extracts it (safely — see the bicep_engine module's
        tarball validation) to a temp working directory.

        GitHub REST: GET /repos/{owner}/{repo}/tarball/{ref}
        https://docs.github.com/en/rest/repos/contents#download-a-repository-archive-tar
        (endpoint + redirect behavior confirmed against current docs at author
        time; re-verify on major GitHub API changes per cardinal rule 5).

        GitHub responds 302 to a short-lived pre-signed codeload URL; requests
        follows the redirect and strips the Authorization header on the
        cross-host hop, so the token reaches only api.github.com. The token is
        never placed in a URL or emitted to logs. For private repos the
        pre-signed link expires in ~5 minutes, which is fine for an immediate
        single fetch.

        :param repo: "owner/repo"
        :param ref: branch, tag, or commit SHA; default branch when None.
        """
        ref_path = f"/{ref}" if ref else ""
        url = f"{self.base_url}/repos/{repo}/tarball{ref_path}"
        resp = self.get(url, allow_redirects=True, stream=False,
                        timeout=HTTP_TIMEOUT_S)
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            # Surface status + target repo/ref only — never the response body
            # or redirect URL, which can carry a signed token in its query.
            logger.debug(
                f"GitHub archive fetch failed for "
                f"{repo}@{ref or 'default-branch'}: status {resp.status_code}"
            )
            raise Exception(
                f"Failed to fetch archive for {repo}@{ref or 'default-branch'} "
                f"(HTTP {resp.status_code}). Verify the repo, ref, and that the "
                f'"{CONNECTION_INFO_NAME}" ConnectionInfo token has read access.'
            )
        return resp.content

    def submit_request(self, url_path: str, method: str = "get", **kwargs):
        """
        Submit a JSON request to the GitHub API.
        """
        url = f"{self.base_url}{url_path}"
        kwargs.setdefault("timeout", HTTP_TIMEOUT_S)
        if method == "get":
            response = self.get(url, **kwargs)
        elif method == "post":
            response = self.post(url, **kwargs)
        elif method == "put":
            response = self.put(url, **kwargs)
        elif method == "delete":
            response = self.delete(url, **kwargs)
        else:
            raise Exception(f"Invalid method: {method}")
        try:
            response.raise_for_status()
        except Exception as e:
            logger.debug(
                f"Error encountered for URL: {url}, details: "
                f"{e.response.content}"
            )
            raise
        return response.json()
