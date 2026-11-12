"""
Where each StormForge optimization setting is set, and which level wins.

Pure Python with no CloudBolt or Kubernetes imports: views.py reads the
objects and hands them over as plain dicts, so this module can be tested on
its own.

A StormForge setting is one name (schedule, containers.cpu.requests.min, ...)
that can be given at several levels. The cluster agent applies the levels in
the order of the cascadingConfigs list in its own configuration (ConfigMap
stormforge-cluster-agent, key cluster-agent.yml, rendered by the Helm chart);
a later entry overrides an earlier one. Chart 3.5.0 renders, lowest first:

  1. api-cluster-optimization-configuration   rules kept in StormForge itself
  2. cluster-defaults                         ConfigMap cluster-defaults
  3. cluster-defaults-auto-deploy-exempt-namespaces   chart value, by namespace name
  4. clusteroptimizationconfigurations        ClusterOptimizationConfiguration objects
  5. namespace-annotations                    live.stormforge.io/* on the Namespace
  6. optimizationconfigurations               OptimizationConfiguration objects
  7. workload-annotations                     live.stormforge.io/* on the workload
  8. pod-annotations                          live.stormforge.io/* on the pod template

The list is read from the cluster, so a chart that reorders it is followed.
Verified on a 3.5.0 agent on 2026-11-10: with one setting given at several
levels, StormForge reported the value of the level latest in that list.

References:
  https://docs.stormforge.io/docs/configure/
  https://docs.stormforge.io/docs/configure/cluster-defaults/
  https://docs.stormforge.io/docs/configure/annotate-namespaces/
  https://docs.stormforge.io/docs/configure/annotate-workloads/
  https://docs.stormforge.io/docs/configure/optimization-configurations/
  https://docs.stormforge.io/docs/configure/settings/annotations/
  https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#label-selectors
"""

import re

PREFIX = "live.stormforge.io/"

LEVEL_LABELS = {
    "workload": "Workload",
    "namespace": "Namespace",
    "cluster": "Cluster",
    "platform": "StormForge",
    "conditional": "Depends on the workload",
}
LEVEL_ORDER = ("workload", "namespace", "cluster", "platform")

# cascadingConfigs entry kind -> (level, what the source is called)
KINDS = {
    "api": ("platform", "Organization settings in StormForge"),
    "file": ("cluster", "ConfigMap cluster-defaults"),
    "inline": ("cluster", "Agent chart setting"),
    "cluster_rules": ("cluster", "ClusterOptimizationConfiguration"),
    "namespace": ("namespace", "Namespace annotation"),
    "namespace_rules": ("namespace", "OptimizationConfiguration"),
    "workload": ("workload", "Workload annotation"),
    "pod": ("workload", "Pod template annotation"),
    "other": ("cluster", "Agent configuration"),
}

GROUPS = (
    ("goals", "Goals"),
    ("schedule", "Schedule"),
    ("apply", "Apply Settings"),
    ("reliability", "Reliability"),
    ("resources", "CPU and Memory"),
    ("hpa", "HPA"),
    ("runtime", "Runtime"),
    ("scheduling", "Pod Scheduling"),
    ("other", "Other"),
)


def _catalog():
    """(name, group, label, default, path in StormForge's workload configuration)."""
    rows = [
        ("cpu.optimization-goal", "goals", "CPU goal", "Balanced", "cpu.optimizationGoal"),
        ("memory.optimization-goal", "goals", "Memory goal", "Balanced", "memory.optimizationGoal"),
        ("schedule", "schedule", "Schedule", "@daily", "schedule.expression"),
        ("schedule.timezone", "schedule", "Time zone", "UTC", "schedule.timezone"),
        ("validity-period", "schedule", "Validity period", "", "validityPeriod"),
        ("auto-deploy", "apply", "Automatic deployment", "Disabled", "autoDeploy.enabled"),
        ("learning-period", "apply", "Learning period", "P7D", "learningPeriod"),
        ("auto-deploy.thresholds.cpu.percent", "apply", "Auto-deploy threshold, CPU percent", "",
         "autoDeploy.thresholds.cpu.percent"),
        ("auto-deploy.thresholds.memory.percent", "apply", "Auto-deploy threshold, memory percent", "",
         "autoDeploy.thresholds.memory.percent"),
        ("auto-deploy.thresholds.cpu.unit", "apply", "Auto-deploy threshold, CPU amount", "",
         "autoDeploy.thresholds.cpu.unit"),
        ("auto-deploy.thresholds.memory.unit", "apply", "Auto-deploy threshold, memory amount", "",
         "autoDeploy.thresholds.memory.unit"),
        ("apply.method", "apply", "Apply method", "PatchImmediateRollout", "apply.method"),
        ("apply.max-percent-increase", "apply", "Largest increase per apply, percent", "",
         "apply.maxPercentIncrease"),
        ("apply.max-percent-decrease", "apply", "Largest decrease per apply, percent", "",
         "apply.maxPercentDecrease"),
        ("pod-template.metadata.patch-path", "apply", "Pod template metadata patch path", "",
         "podTemplate.metadataPatchPath"),
        ("pod-template.metadata.patch-format", "apply", "Pod template metadata patch format", "",
         "podTemplate.metadataPatchFormat"),
    ]
    for prefix, title, path, unit_min, unit_max in (
        ("reliability.oom.memory-bump-up.", "OOM response", "reliability.oom.memoryBumpUp.", "0Mi", "2Gi"),
        ("reliability.cpu-pressure.cpu-bump-up.", "CPU pressure response", "reliability.cpuPressure.cpuBumpUp.",
         "0m", "500m"),
    ):
        rows += [
            (prefix + "enabled", "reliability", title, "False", path + "enabled"),
            (prefix + "period", "reliability", title + ", recovery period", "P4D", path + "period"),
            (prefix + "percent", "reliability", title + ", bump-up percent", "0", path + "percent"),
            (prefix + "min", "reliability", title + ", smallest bump-up", unit_min, path + "min"),
            (prefix + "max", "reliability", title + ", largest bump-up", unit_max, path + "max"),
            (prefix + "apply-immediately", "reliability", title + ", apply immediately", "Never",
             path + "applyImmediately"),
        ]
    for resource, title, floor in (("cpu", "CPU", "10m"), ("memory", "Memory", "32Mi")):
        prefix, path = f"containers.{resource}.", f"defaultContainer.{resource}."
        rows += [
            (prefix + "optimization-policy", "resources", title + " optimization policy",
             "RequestsRaiseLimitsIfNeeded", path + "optimizationPolicy"),
            (prefix + "limits.limit-request-ratio", "resources", title + " limit-to-request ratio", "2.0",
             path + "limitRequestRatio"),
            (prefix + "requests.min", "resources", title + " requests, minimum", floor, path + "requestsMin"),
            (prefix + "requests.max", "resources", title + " requests, maximum", "", path + "requestsMax"),
            (prefix + "limits.min", "resources", title + " limits, minimum", "", path + "limitsMin"),
            (prefix + "limits.max", "resources", title + " limits, maximum", "", path + "limitsMax"),
            (prefix + "requests.patch-path", "resources", title + " requests patch path", "",
             path + "requestsPatchPath"),
            (prefix + "limits.patch-path", "resources", title + " limits patch path", "", path + "limitsPatchPath"),
        ]
    for resource, title in (("cpu", "CPU"), ("memory", "Memory")):
        rows += [
            (f"hpa.{resource}.target-utilization.min", "hpa", f"HPA {title} target utilization, minimum", "50",
             f"hpa.{resource}.targetUtilizationMin"),
            (f"hpa.{resource}.target-utilization.max", "hpa", f"HPA {title} target utilization, maximum", "90",
             f"hpa.{resource}.targetUtilizationMax"),
        ]
    rows += [
        ("hpa.preserve-horizontal-scale-point", "hpa", "Preserve horizontal scale point", "False",
         "hpa.preserveHorizontalScalePoint"),
        ("hpa.ignore-horizontally-scaled-resources", "hpa", "Ignore horizontally scaled resources", "False",
         "hpa.ignoreHorizontallyScaledResources"),
        ("hpa.ignore", "hpa", "Ignore the HPA", "False", "hpa.ignore"),
        ("containers.java.max-heap.patch-path", "runtime", "Java max heap patch path", "-",
         "defaultContainer.java.maxHeapPatchPath"),
        ("containers.java.max-heap.patch-format", "runtime", "Java max heap patch format", "",
         "defaultContainer.java.maxHeapPatchFormat"),
        ("containers.java.max-heap.min", "runtime", "Java max heap, minimum", "512Mi",
         "defaultContainer.java.maxHeapMin"),
        ("containers.java.max-heap.max", "runtime", "Java max heap, maximum", "",
         "defaultContainer.java.maxHeapMax"),
        ("pod-scheduling.optimization-policy", "scheduling", "Scheduling optimization policy", "DoNotOptimize",
         "podScheduling.schedulingOptimizationPolicy"),
        ("pod-scheduling.instance-categories.node-affinity.type", "scheduling", "Node affinity type", "preferred",
         "podScheduling.instanceCategories.nodeAffinityType"),
        ("pod-scheduling.instance-categories.node-affinity.weight", "scheduling", "Node affinity weight", "70",
         "podScheduling.instanceCategories.nodeAffinityWeight"),
        ("pod-scheduling.instance-categories.node-affinity.patch-path", "scheduling", "Node affinity patch path",
         "", "podScheduling.instanceCategories.nodeAffinityPatchPath"),
        ("pod-scheduling.instance-categories.node-affinity.patch-format", "scheduling",
         "Node affinity patch format", "", "podScheduling.instanceCategories.nodeAffinityPatchFormat"),
    ]
    return rows


CATALOG = _catalog()
_BY_NAME = {row[0]: row for row in CATALOG}
_POSITION = {row[0]: index for index, row in enumerate(CATALOG)}


# ---------------------------------------------------------------------------
# The agent's order of precedence
# ---------------------------------------------------------------------------
def default_cascade():
    """Chart 3.5.0's cascadingConfigs, for a cluster whose agent configuration cannot be read."""
    def entry(name, kind, namespaces=(), annotations=None):
        return {"name": name, "kind": kind, "namespaces": list(namespaces), "annotations": dict(annotations or {})}
    return [
        entry("api-cluster-optimization-configuration", "api"),
        entry("cluster-defaults", "file"),
        entry("cluster-defaults-auto-deploy-exempt-namespaces", "inline", ("^kube-system$", "^openshift.*"),
              {PREFIX + "auto-deploy": "Disabled"}),
        entry("clusteroptimizationconfigurations", "cluster_rules"),
        entry("namespace-annotations", "namespace"),
        entry("optimizationconfigurations", "namespace_rules"),
        entry("workload-annotations", "workload"),
        entry("pod-annotations", "pod"),
    ]


def parse_cascade(document):
    """The cascadingConfigs of a parsed cluster-agent.yml, lowest precedence first; [] when absent."""
    entries = []
    for item in (document or {}).get("cascadingConfigs") or []:
        if not isinstance(item, dict):
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        selector = item.get("namespaceSelector") if isinstance(item.get("namespaceSelector"), dict) else {}
        entry = {
            "name": str(item.get("name") or ""),
            "kind": "other",
            "namespaces": [str(p) for p in selector.get("matchNamesRegex") or []],
            "annotations": {},
        }
        kind = config.get("optimizationConfigurationKind")
        if kind == "APIClusterOptimizationConfiguration":
            entry["kind"] = "api"
        elif kind == "ClusterOptimizationConfiguration":
            entry["kind"] = "cluster_rules"
        elif kind == "OptimizationConfiguration":
            entry["kind"] = "namespace_rules"
        elif config.get("annotationsFile"):
            entry["kind"] = "file"
        elif isinstance(config.get("annotationsInline"), dict):
            entry["kind"] = "inline"
            entry["annotations"] = {str(k): str(v) for k, v in config["annotationsInline"].items()}
        elif config.get("annotationsFrom") in ("namespace", "workload", "pod"):
            entry["kind"] = config["annotationsFrom"]
        entries.append(entry)
    return entries


def describe_cascade(cascade):
    """Rows for the order-of-precedence list, lowest first."""
    rows = []
    for entry in cascade:
        level, source = KINDS.get(entry["kind"], KINDS["other"])
        detail = ""
        if entry["namespaces"]:
            detail = "Namespaces named " + ", ".join(entry["namespaces"])
        if entry["annotations"]:
            pairs = ", ".join(f"{_strip(k)}={v}" for k, v in sorted(entry["annotations"].items()))
            detail = (detail + ": " if detail else "") + pairs
        rows.append({"name": entry["name"], "level": level, "level_label": LEVEL_LABELS[level],
                     "source": source, "detail": detail})
    return rows


# ---------------------------------------------------------------------------
# Label selectors and rule matching
# ---------------------------------------------------------------------------
_REQUIREMENT = re.compile(
    r"^(?P<not>!)?\s*(?P<key>[A-Za-z0-9_./-]+)\s*"
    r"(?:(?P<op>==|=|!=)\s*(?P<value>[A-Za-z0-9_.-]*)|\s+(?P<set>in|notin)\s*\((?P<values>[^)]*)\))?$"
)


def _requirements(selector):
    parts, depth, current = [], 0, ""
    for char in str(selector):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def selector_matches(selector, labels):
    """Whether a kubectl-style label selector matches ``labels``; None when it cannot be read."""
    labels = labels or {}
    for requirement in _requirements(selector):
        found = _REQUIREMENT.match(requirement)
        if not found:
            return None
        key, has = found.group("key"), found.group("key") in labels
        value = labels.get(key)
        if found.group("not"):
            if found.group("op") or found.group("set"):
                return None
            ok = not has
        elif found.group("set"):
            values = {v.strip() for v in found.group("values").split(",") if v.strip()}
            ok = (has and value in values) if found.group("set") == "in" else (not has or value not in values)
        elif found.group("op") == "!=":
            ok = not has or value != found.group("value")
        elif found.group("op"):
            ok = has and value == found.group("value")
        else:
            ok = has
        if not ok:
            return False
    return True


def _all(states):
    if any(state is False for state in states):
        return False
    return None if any(state is None for state in states) else True


def _term(term, context):
    """(states, conditions) of one match term's own criteria."""
    states, conditions = [], []
    for field, labels, words in (
        ("namespaceSelector", context.get("namespace_labels"), "namespaces labeled"),
        ("objectSelector", context.get("workload_labels"), "workloads labeled"),
    ):
        selector = term.get(field)
        if not selector:
            continue
        state = None if labels is None else selector_matches(selector, labels)
        states.append(state)
        if state is None:
            conditions.append(f"{words} {selector}")
    if term.get("expression"):
        # CEL is not evaluated here; StormForge's reported value settles it for a workload.
        states.append(None)
        conditions.append(f"the expression {term['expression']}")
    return states, conditions


def evaluate(match, context):
    """(True | False | None, [conditions]) for a rule's match; None means it depends."""
    match = match if isinstance(match, dict) else {}
    states, conditions = _term(match, context)
    if isinstance(match.get("anyOf"), list):
        outcomes, alternatives = [], []
        for term in match["anyOf"]:
            term = term if isinstance(term, dict) else {}
            term_states, term_conditions = _term(term, context)
            # A term with no criteria matches nothing.
            outcomes.append(_all(term_states) if term_states else False)
            if term_conditions:
                alternatives.append(" and ".join(term_conditions))
        if any(outcome is True for outcome in outcomes):
            states.append(True)
        elif any(outcome is None for outcome in outcomes):
            states.append(None)
            conditions.append("one of: " + "; or ".join(alternatives))
        else:
            states.append(False)
    state = _all(states)
    return state, (conditions if state is None else [])


def describe_match(match):
    """A rule's match in words."""
    match = match if isinstance(match, dict) else {}

    def words(term):
        parts = []
        if term.get("namespaceSelector"):
            parts.append(f"namespaces labeled {term['namespaceSelector']}")
        if term.get("objectSelector"):
            parts.append(f"workloads labeled {term['objectSelector']}")
        if term.get("expression"):
            parts.append(f"expression {term['expression']}")
        return " and ".join(parts)

    text = words(match)
    if isinstance(match.get("anyOf"), list):
        any_of = "; or ".join(filter(None, (words(t) for t in match["anyOf"] if isinstance(t, dict))))
        text = (text + " and " if text else "") + f"one of: {any_of or 'nothing'}"
    return text or "Every workload"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _strip(name):
    name = str(name)
    return name[len(PREFIX):] if name.startswith(PREFIX) else name


def _setting_value(setting):
    if setting.get("value") not in (None, ""):
        return str(setting["value"])
    if isinstance(setting.get("values"), list):
        return ", ".join(str(v) for v in setting["values"])
    return ""


def _annotations(annotations):
    """{setting name: value} of the live.stormforge.io/* annotations."""
    return {
        _strip(key): str(value) for key, value in (annotations or {}).items()
        if str(key).startswith(PREFIX)
    }


def _rule_contributions(configs, kind, context):
    """Contributions of ClusterOptimizationConfiguration or OptimizationConfiguration objects.

    Objects apply in spec.order, ties by name; rules within one in the order written.
    """
    out = []
    for config in sorted(configs or [], key=lambda c: (c.get("order") or 0, c.get("name") or "")):
        outer, outer_conditions = evaluate(config.get("match"), context)
        if outer is False:
            continue
        for rule in config.get("rules") or []:
            inner, inner_conditions = evaluate(rule.get("match"), context)
            state = _all([outer, inner])
            if state is False:
                continue
            source = f"{KINDS[kind][1]} {config.get('name') or ''}, rule {rule.get('name') or ''}".strip()
            for setting in rule.get("settings") or []:
                if not setting.get("name"):
                    continue
                out.append({
                    "name": _strip(setting["name"]), "value": _setting_value(setting), "kind": kind,
                    "source": source,
                    "when": "" if state else " and ".join(outer_conditions + inner_conditions),
                })
    return out


def contributions(layers, namespace="", workload=False):
    """Every value given for a setting, lowest precedence first.

    ``layers``: cascade, cluster_defaults {name: value}, cluster_rules,
    namespace {labels, annotations}, namespace_rules, workload {labels,
    annotations, pod_annotations}. ``namespace`` is '' for the cluster's own
    view; ``workload`` says whether one workload is being resolved.
    """
    namespace_doc = layers.get("namespace") or {}
    workload_doc = layers.get("workload") or {}
    context = {
        "namespace_labels": (namespace_doc.get("labels") or {}) if namespace else None,
        "workload_labels": (workload_doc.get("labels") or {}) if workload else None,
    }
    out = []
    for entry in layers.get("cascade") or default_cascade():
        kind = entry["kind"]
        when = ""
        if entry.get("namespaces"):
            if namespace:
                try:
                    if not any(re.search(pattern, namespace) for pattern in entry["namespaces"]):
                        continue
                except re.error:
                    when = "namespaces named " + ", ".join(entry["namespaces"])
            else:
                when = "namespaces named " + ", ".join(entry["namespaces"])
        if kind == "file":
            values = {_strip(k): str(v) for k, v in (layers.get("cluster_defaults") or {}).items()}
        elif kind == "inline":
            values = _annotations(entry.get("annotations"))
        elif kind == "namespace":
            values = _annotations(namespace_doc.get("annotations")) if namespace else {}
        elif kind == "workload":
            values = _annotations(workload_doc.get("annotations")) if workload else {}
        elif kind == "pod":
            values = _annotations(workload_doc.get("pod_annotations")) if workload else {}
        elif kind == "cluster_rules":
            for item in _rule_contributions(layers.get("cluster_rules"), kind, context):
                item["when"] = " and ".join(filter(None, (when, item["when"])))
                out.append(item)
            continue
        elif kind == "namespace_rules":
            if namespace:
                out.extend(_rule_contributions(layers.get("namespace_rules"), kind, context))
            continue
        else:
            continue
        source = KINDS[kind][1]
        if kind == "inline":
            source = f"{source} ({entry['name']})"
        for name, value in values.items():
            out.append({"name": name, "value": value, "kind": kind, "source": source, "when": when})
    return out


def _entry(item):
    level = KINDS[item["kind"]][0]
    return {"value": item["value"], "level": level, "level_label": LEVEL_LABELS[level],
            "source": item["source"], "when": item.get("when") or ""}


def _set_winner(row, entry):
    row.update(value=entry["value"], level=entry["level"], level_label=entry["level_label"],
               source=entry["source"])


def _blank_row(name):
    known = _BY_NAME.get(name)
    return {
        "name": name, "key": PREFIX + name,
        "group": known[1] if known else "other",
        "label": known[2] if known else name,
        "default": known[3] if known else "",
        "value": None, "level": "conditional", "level_label": LEVEL_LABELS["conditional"], "source": "",
        "overridden": [], "conditional": [], "confirmed": False, "reported": None,
    }


def resolve(layers, namespace="", workload=False):
    """One row per setting something sets: the value in effect, its level, and what it overrides."""
    rows = {}
    for item in contributions(layers, namespace, workload):
        row = rows.setdefault(item["name"], _blank_row(item["name"]))
        entry = _entry(item)
        if entry["when"]:
            row["conditional"].append(entry)
            continue
        if row["value"] is not None:
            row["overridden"].insert(0, {key: row[key] for key in ("value", "level", "level_label", "source")})
        _set_winner(row, entry)
    return sorted(rows.values(), key=lambda r: (_POSITION.get(r["name"], len(CATALOG)), r["name"]))


# ---------------------------------------------------------------------------
# Checking against what StormForge reports for a workload
# ---------------------------------------------------------------------------
_QUANTITY = re.compile(r"^([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+))([a-zA-Z]*)$")
_SUFFIXES = {
    "": 1, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
    "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40,
}
_ALIASES = {
    "enabled": "true", "disabled": "false", "@daily": "p1d", "@hourly": "pt1h", "@weekly": "p7d",
    "preferred": "preferredduringschedulingignoredduringexecution",
    "required": "requiredduringschedulingignoredduringexecution",
}


def _lookup(document, path):
    for part in path.split("."):
        if not isinstance(document, dict):
            return None
        document = document.get(part)
    return document


def _unset(value):
    return value in (None, "", False, 0)


def _normal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value if value is not None else "").strip()
    found = _QUANTITY.match(text)
    if found and found.group(2) in _SUFFIXES:
        return float(found.group(1)) * _SUFFIXES[found.group(2)]
    text = text.lower()
    return _ALIASES.get(text, text)


def _same(left, right):
    left, right = _normal(left), _normal(right)
    if isinstance(left, float) and isinstance(right, float):
        return abs(left - right) <= 1e-9 * max(1.0, abs(left), abs(right))
    return left == right


def _default_part(name, value):
    """The all-containers part of a containers.* value ('16000m,jenkins=4000m' -> '16000m')."""
    if not name.startswith("containers."):
        return value
    first = str(value).split(",")[0].strip()
    return "" if "=" in first else first


def verify(rows, config, explicit):
    """Check ``rows`` against StormForge's configuration for the workload, in place; returns the rows.

    ``config`` is GET /management/workloads/{id}/config and ``explicit`` the
    same call with no_defaults=true, which leaves out built-in defaults. A
    value StormForge reports that a rule depending on the workload gives is
    credited to that rule; one that no Kubernetes object gives is shown as set
    in StormForge; a Kubernetes value StormForge does not report is flagged.
    """
    config, explicit = config or {}, explicit or {}
    by_name = {row["name"]: row for row in rows}
    for name, _group, _label, _default, path in CATALOG:
        if name.startswith("hpa.") and name != "hpa.ignore" and not _lookup(config, "hpa.enabled"):
            continue    # reported only for a workload with an HPA
        if name.startswith("containers.java.") and not config.get("hasJava"):
            continue
        effective, given = _lookup(config, path), _lookup(explicit, path)
        row = by_name.get(name)
        if row is None or row["value"] is None:
            if _unset(given):
                continue
            conditional = row["conditional"] if row else []
            hit = next((c for c in reversed(conditional) if _same(_default_part(name, c["value"]), given)), None)
            # StormForge also lists a few built-in defaults as given (the
            # optimization policy, once any container value is set).
            if hit is None and _same(given, _default):
                continue
            if row is None:
                row = by_name[name] = _blank_row(name)
                rows.append(row)
            if hit is not None:
                row["conditional"].remove(hit)
                _set_winner(row, hit)
            else:
                row.update(value=_text(given), level="platform", level_label=LEVEL_LABELS["platform"],
                           source="Set in StormForge, not by an object in this cluster")
            row["confirmed"] = True
            continue
        if _same(_default_part(name, row["value"]), effective):
            row["confirmed"] = True
            continue
        candidates = list(reversed(row["conditional"])) + row["overridden"]
        hit = next((c for c in candidates if _same(_default_part(name, c["value"]), effective)), None)
        if hit is not None:
            current = {key: row[key] for key in ("value", "level", "level_label", "source")}
            if hit in row["conditional"]:
                row["conditional"].remove(hit)
                row["overridden"].insert(0, current)
            else:
                row["overridden"].remove(hit)
                row["overridden"].insert(0, current)
            _set_winner(row, hit)
            row["confirmed"] = True
        else:
            row["reported"] = _text(effective) or "no value"
    rows.sort(key=lambda r: (_POSITION.get(r["name"], len(CATALOG)), r["name"]))
    return rows


def _text(value):
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def grouped(rows):
    """[{title, rows}] in StormForge's settings order, empty groups left out."""
    out = []
    for key, title in GROUPS:
        members = [row for row in rows if row["group"] == key]
        if members:
            out.append({"key": key, "title": title, "rows": members})
    return out


def counts(rows):
    """[{level, label, count}] of settings in effect per level, most specific first."""
    out = []
    for level in LEVEL_ORDER:
        count = sum(1 for row in rows if row["value"] is not None and row["level"] == level)
        if count:
            out.append({"level": level, "label": LEVEL_LABELS[level], "count": count})
    return out


def default_count(rows):
    """How many catalog settings nothing sets, so StormForge's default applies."""
    given = {row["name"] for row in rows if row["value"] is not None}
    return sum(1 for row in CATALOG if row[0] not in given)


def rule_objects(configs):
    """Rows describing rule objects: name, order, who they match, and each rule's settings."""
    out = []
    for config in sorted(configs or [], key=lambda c: (c.get("order") or 0, c.get("name") or "")):
        out.append({
            "name": config.get("name") or "",
            "order": config.get("order") or 0,
            "match": describe_match(config.get("match")),
            "rules": [
                {"name": rule.get("name") or "", "match": describe_match(rule.get("match")),
                 "settings": [(_strip(s.get("name") or ""), _setting_value(s)) for s in rule.get("settings") or []]}
                for rule in config.get("rules") or []
            ],
        })
    return out


def merged(base, override):
    """``override`` laid over ``base``, dict by dict (Helm's values merge)."""
    out = dict(base or {})
    for key, value in (override or {}).items():
        out[key] = merged(out.get(key), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _names(values):
    return ", ".join(str(v) for v in values) if isinstance(values, list) and values else ""


def behavior(values):
    """Agent chart values that decide what StormForge watches and how it applies: [(label, value, note)].

    https://docs.stormforge.io/docs/installation/agent-config/
    https://docs.stormforge.io/docs/installation/applier-config/
    https://docs.stormforge.io/docs/installation/webhook-config/
    """
    values = values if isinstance(values, dict) else {}
    agent = merged(values.get("agent") if isinstance(values.get("agent"), dict) else {},
                   values.get("clusterAgent") if isinstance(values.get("clusterAgent"), dict) else {})
    applier = values.get("applier") if isinstance(values.get("applier"), dict) else {}
    webhook = values.get("webhook") if isinstance(values.get("webhook"), dict) else {}
    handler = _lookup(webhook, "handlers.podContainerResources")
    handler = handler if isinstance(handler, dict) else webhook
    allow, deny = _names(agent.get("allowNamespaces")), _names(agent.get("denyNamespaces"))
    rows = [(
        "Namespaces StormForge watches",
        f"Only {allow}" if allow else (f"All except {deny}" if deny else "All namespaces"),
        "clusterAgent.allowNamespaces, clusterAgent.denyNamespaces",
    )]
    applier_on = applier.get("enabled") is not False
    rows.append((
        "Applier", "Installed" if applier_on else "Not installed",
        "applier.enabled" + ("" if applier_on else ". StormForge cannot apply recommendations itself, so "
                             "automatic deployment has no effect."),
    ))
    if applier_on:
        mode = _lookup(applier, "reconciliation.mode") or "EventBased"
        rows.append((
            "Reconciliation", mode,
            "applier.reconciliation.mode. " + (
                "The Applier keeps recommended values in place when something else changes them."
                if mode == "Continuous" else "The Applier changes requests and limits only on StormForge events."),
        ))
        if applier.get("suspend"):
            rows.append(("Applier suspended", str(applier["suspend"]), "applier.suspend"))
    webhook_on = webhook.get("enabled") is not False and handler.get("enabled") is not False
    rows.append((
        "Admission webhook", "Enabled" if webhook_on else "Disabled",
        "webhook.enabled" + ("" if webhook_on else ". The Webhook apply methods do not work without it."),
    ))
    if webhook_on:
        allow, deny = _names(handler.get("allowNamespaces")), _names(handler.get("denyNamespaces"))
        rows.append((
            "Webhook namespaces",
            f"Only {allow}" if allow else (f"All except {deny}" if deny else "All namespaces"),
            "Namespaces where the Webhook apply methods can change pods",
        ))
        rows.append(("Webhook failure policy", str(handler.get("failurePolicy") or "Ignore"),
                     "Ignore admits pods unchanged when the webhook is down; Fail rejects them"))
    return rows
