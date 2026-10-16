"""
README files for the details page.

A unit's documentation is the README.md in its own folder on the branch its
Remote Source URL points at, the repository and branch it was last synced
from or pushed to. Members and dependencies follow the same rule, except a
parameter member: it has no Remote Source URL, so its folder is
parameters/<CF-id>/ on its package's home branch. Files are
read from CloudBolt's global clone of that repository, the clone the branch
pickers fetch, which is fetched at most once a minute here; nothing is
written.

README markdown is rendered with the Markdown library behind CloudBolt's
markdown descriptions and cleaned with CloudBolt's bleach_html. Relative links
are pointed at the file on the Git host, and images become links because a
private repository's images cannot load inside CloudBolt.
"""
import os
import posixpath
import time
from collections import OrderedDict, namedtuple
from urllib.parse import urlsplit

import markdown
from bs4 import BeautifulSoup
from django.conf import settings

from common.methods import bleach_html
from customforms.models import CustomForm, CustomFormFunction
from utilities.logger import ThreadLogger

from xui.content_packages import store

logger = ThreadLogger(__name__)

# File names that count as a README, compared in lower case.
README_NAMES = ("readme.md", "readme.markdown")

# Seconds a fetch of the global clone stays fresh enough to read from.
FETCH_EVERY = 60

# Larger READMEs are cut off rather than rendered.
MAX_BYTES = 512 * 1024

MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "sane_lists"]

# bleach_html's defaults, plus target and rel so links open on the Git host
# in a new tab.
LINK_ATTRS = ["href", "title", "target", "rel"]

Location = namedtuple("Location", "repo branch folder")


class NoReadme(Exception):
    """Why a unit has no README to show, worded for the dialog."""


# ---------------------------------------------------------------------------
# Objects and where they live
# ---------------------------------------------------------------------------
def _querysets():
    """Prefix -> queryset for every unit an export can write, including the
    forms and form functions that only travel as dependencies."""
    sets = OrderedDict((t.prefix, t.with_repo(t.resolvable())) for t in store.MEMBER_TYPES.values())
    sets["FRM"] = CustomForm.objects.select_related("source_code_repo")
    sets["FJS"] = CustomFormFunction.objects.select_related("source_code_repo")
    return sets


def resolve(global_ids):
    """{global_id: object} for the ids that exist here, one query per prefix."""
    wanted = OrderedDict()
    for global_id in global_ids:
        if isinstance(global_id, str) and "-" in global_id:
            wanted.setdefault(global_id.split("-", 1)[0], []).append(global_id)
    sets = _querysets()
    found = {}
    for prefix, ids in wanted.items():
        queryset = sets.get(prefix)
        if queryset is None:
            continue
        for obj in queryset.filter(global_id__in=ids):
            found[obj.global_id] = obj
    return found


def name_of(obj):
    prefix = obj.global_id.split("-", 1)[0]
    for member_type in store.MEMBER_TYPES.values():
        if member_type.prefix == prefix:
            return member_type.name_of(obj)
    return getattr(obj, "name", "") or obj.global_id


def location(obj):
    """Location(repo, branch, folder) of obj's folder, from its Remote Source
    URL, or None when it is not linked to a repository."""
    url = getattr(obj, "remote_source_url", "") or ""
    if not url or not getattr(obj, "source_code_repo_id", None):
        return None
    repo = obj.source_code_repo.cast()
    try:
        if "{{" in url and hasattr(obj, "remote_source_url_from_template"):
            # A blueprint URL may hold parameter references; CloudBolt
            # renders them before every push and refresh.
            url = obj.remote_source_url_from_template
        __, __, __, branch, remainder = repo.parse_repo_url(url)
    except Exception:
        return None
    remainder = remainder.replace(os.sep, "/").strip("/")
    # Most URLs end in the metadata file. A plugin or shared module that
    # arrived as another unit's dependency has its folder as its URL.
    folder = posixpath.dirname(remainder) if remainder.lower().endswith(".json") else remainder
    if not branch or not folder:
        return None
    return Location(repo, branch, folder)


def parameter_location(package, ref, repo):
    """Location of a parameter member's folder: parameters/<CF-id>/ on the
    package's home branch of repo (its home repository), or None before the
    package has one."""
    branch = package["local"].get("branch") or ""
    if repo is None or not branch:
        return None
    return Location(repo.cast(), branch, "{}/{}".format(store.PARAMETERS_DIR, ref.split("/", 1)[1]))


# ---------------------------------------------------------------------------
# Reading the global clone
# ---------------------------------------------------------------------------
def _clone(repo, clones):
    """repo's global clone, fetched unless that happened in the last
    FETCH_EVERY seconds. A failed fetch falls back to what the clone already
    holds. None when there is no clone and it cannot be made."""
    if repo.id in clones:
        return clones[repo.id]
    try:
        clone = repo.get_global_clone()
    except Exception:
        logger.exception("Could not clone %s to read READMEs", repo.label)
        clones[repo.id] = None
        return None
    try:
        age = time.time() - os.path.getmtime(os.path.join(clone.git_dir, "FETCH_HEAD"))
    except OSError:
        age = None
    if age is None or age > FETCH_EVERY:
        try:
            # get_branches fetches the global clone, as the branch pickers do.
            repo.get_branches()
        except Exception:
            logger.warning("Could not fetch %s; reading READMEs from the last fetch", repo.label)
    clones[repo.id] = clone
    return clone


def _rev(clone, branch):
    """The revision to read branch from: its remote-tracking branch, else a
    tag of that name. None when neither exists."""
    for rev in ("origin/{}".format(branch), "refs/tags/{}".format(branch)):
        try:
            clone.git.rev_parse("--verify", "--quiet", rev + "^{commit}")
        except Exception:
            continue
        return rev
    return None


def readmes(objects, clones=None, locations=None):
    """{global_id: README path} for every object whose folder holds a README
    on its own branch. One git call per repository and branch. clones caches
    the global clones by repository id across calls; locations
    ({global_id: Location}) gives the folder of objects with no Remote Source
    URL."""
    trees = OrderedDict()
    for obj in objects:
        loc = (locations or {}).get(obj.global_id) or location(obj)
        if loc is not None:
            trees.setdefault((loc.repo.id, loc.branch), []).append((obj.global_id, loc))

    found = {}
    clones = {} if clones is None else clones
    for (__, branch), items in trees.items():
        clone = _clone(items[0][1].repo, clones)
        rev = _rev(clone, branch) if clone is not None else None
        if rev is None:
            continue
        folders = sorted({loc.folder for __, loc in items})
        try:
            listed = clone.git.ls_tree("--name-only", rev, "--", *[f + "/" for f in folders]).splitlines()
        except Exception:
            logger.exception("Could not list READMEs on %s", rev)
            continue
        by_folder = {}
        for path in listed:
            folder, name = posixpath.split(path)
            if name.lower() in README_NAMES:
                by_folder.setdefault(folder, path)
        for global_id, loc in items:
            if loc.folder in by_folder:
                found[global_id] = by_folder[loc.folder]
    return found


def read(obj, loc=None):
    """obj's README as a dict for the dialog: rendered html, path, branch,
    repository and the file's URL on the Git host. loc overrides the folder
    obj's Remote Source URL names. Raises NoReadme."""
    loc = loc or location(obj)
    if loc is None:
        raise NoReadme("{} is not linked to a repository, so there is no README to show.".format(name_of(obj)))
    clones = {}
    path = readmes([obj], clones, {obj.global_id: loc}).get(obj.global_id)
    if path is None:
        raise NoReadme("{} has no README.md in {}/ on branch {} of {}.".format(
            name_of(obj), loc.folder, loc.branch, loc.repo.label
        ))
    clone = clones[loc.repo.id]
    try:
        text = clone.git.show("{}:{}".format(_rev(clone, loc.branch), path))
    except Exception as exc:
        logger.exception("Could not read %s", path)
        raise NoReadme("Could not read {} on branch {}: {}".format(path, loc.branch, exc))
    truncated = len(text) > MAX_BYTES
    if truncated:
        text = text[:MAX_BYTES]
    return {
        "html": render(text, loc, path),
        "path": path,
        "branch": loc.branch,
        "repo_label": loc.repo.label,
        "repo_id": loc.repo.id,
        "web_url": _file_url(loc, path),
        "service_name": getattr(loc.repo, "service_name", "") or "the repository",
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _file_url(loc, path):
    try:
        return loc.repo.get_repo_url_for_file(loc.branch, path)
    except Exception:
        return ""


def _link(href, loc, readme_path):
    """href as it should be inside CloudBolt: absolute and in-page links
    unchanged, repository paths pointed at the Git host, None for a path
    that leaves the repository."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path:
        return href
    base = "" if parts.path.startswith("/") else posixpath.dirname(readme_path)
    target = posixpath.normpath(posixpath.join(base, parts.path.lstrip("/")))
    if target == ".." or target.startswith("../"):
        return None
    url = _file_url(loc, target)
    if not url:
        return None
    return url + ("#" + parts.fragment if parts.fragment else "")


def render(text, loc, readme_path):
    """Markdown to safe HTML."""
    soup = BeautifulSoup(markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS), "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "object", "embed", "template"]):
        tag.decompose()
    for img in soup.find_all("img"):
        src = _link(img.get("src") or "", loc, readme_path)
        label = img.get("alt") or posixpath.basename(img.get("src") or "") or "image"
        if src:
            link = soup.new_tag("a", href=src)
            link.string = "[{}]".format(label)
            img.replace_with(link)
        else:
            img.replace_with("[{}]".format(label))
    for anchor in soup.find_all("a"):
        href = _link(anchor.get("href") or "", loc, readme_path)
        if href is None:
            anchor.unwrap()
            continue
        anchor["href"] = href
        if not href.startswith("#"):
            anchor["target"] = "_blank"
            anchor["rel"] = "noopener noreferrer"
    attrs = dict(settings.BLEACH_ALLOWED_ATTRS, a=LINK_ATTRS)
    # extract=False keeps the text of tags bleach strips (for example
    # <details>) instead of dropping it with the tag.
    return bleach_html(str(soup), allowed_attrs=attrs, extract=False)
