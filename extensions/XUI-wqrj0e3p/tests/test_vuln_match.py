"""
Checks for the vulnerability identity helper, the match hash and the totals of
the Vulnerabilities XUI, without CloudBolt (vuln_match is plain standard library):

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_match.py"

The package is loaded under a private name so that another test file's stub of
the same extension cannot get in the way.
"""
import importlib
import os
import random
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.join(os.path.dirname(HERE), "io_cloudbolt_vulnerabilities")
PKG = "vuln_s_match_pkg"

_pkg = types.ModuleType(PKG)
_pkg.__path__ = [PKG_DIR]
sys.modules[PKG] = _pkg
vuln_match = importlib.import_module(PKG + ".vuln_match")


def match(name, version, vuln_id, severity="High", cves=(), kev=False, fix="", path="", pkg_type="rpm"):
    """A MATCH dict the way parse_grype makes them; cves is a list of (id, exploited)."""
    return {
        "pkg_name": name, "pkg_version": version, "pkg_type": pkg_type, "path": path, "vuln_id": vuln_id,
        "severity": severity, "sev_rank": vuln_match.sev_rank(severity), "fix_state": "fixed" if fix else "unknown",
        "fix_version": fix, "kev": kev, "epss": None, "risk": None, "data_source": "",
        "cves": [{"cve": cve, "kev": bool(flag)} for cve, flag in cves],
    }


def reference_summarize(matches):
    """summarize as the first release wrote it (git show vulnerabilities-xui:...vuln_match.py), the
    reference the unchanged-numbers check compares with."""
    counts = {name.lower(): 0 for name in vuln_match.SEVERITIES}
    cves, exploited = set(), set()
    worst = {}
    for item in matches:
        counts[vuln_match.normalise_severity(item.get("severity")).lower()] += 1
        for entry in [c["cve"] for c in item.get("cves") or []] or [item.get("vuln_id")]:
            worst[entry] = max(worst.get(entry, 0), vuln_match.sev_rank(item.get("severity")))
        for link in item.get("cves") or []:
            cves.add(link["cve"])
            if link.get("kev"):
                exploited.add(link["cve"])
    counts["findings"] = len(matches)
    counts["kev"] = len(exploited)
    counts["cves"] = len(cves)
    counts["vulns"] = len(worst)
    for name in vuln_match.SEVERITIES:
        counts["vuln_" + name.lower()] = sum(1 for rank in worst.values() if rank == vuln_match._RANKS[name])
    return counts


FIXED = [
    match("bash", "5.1", "ALAS-1", "High", [("CVE-2024-0001", True), ("CVE-2024-0002", False)], True, "5.2"),
    match("bash", "5.1", "ALAS-2", "Critical", [("CVE-2024-0001", True)], True, "5.3"),
    match("openssl", "3.0", "GHSA-aaaa-bbbb", "Medium", [], False, "3.1", pkg_type="deb"),
    match("libfoo", "1.0", "CVE-2024-0009", "Negligible", [("CVE-2024-0009", False)], False, "", "/opt/a", "python"),
    match("libfoo", "1.0", "CVE-2024-0009", "Low", [("CVE-2024-0009", False)], False, "", "/opt/b", "python"),
    match("curl", "8.0", "ALAS-3", "weird", [("CVE-2024-0003", False)], False, ""),
]


class SummarizeTests(unittest.TestCase):
    def test_fixed_input_numbers(self):
        self.assertEqual(vuln_match.summarize(FIXED), {
            "critical": 1, "high": 1, "medium": 1, "low": 2, "unknown": 1,
            "findings": 6, "kev": 1, "cves": 4, "vulns": 5,
            "vuln_critical": 1, "vuln_high": 1, "vuln_medium": 1, "vuln_low": 1, "vuln_unknown": 1,
        })

    def test_rows_argument_changes_nothing(self):
        self.assertEqual(vuln_match.summarize(FIXED), vuln_match.summarize(FIXED, vuln_match.vuln_rows(FIXED)))

    def test_empty(self):
        self.assertEqual(vuln_match.summarize([])["vulns"], 0)
        self.assertEqual(vuln_match.vuln_rows([]), [])

    def test_equals_the_first_release_on_random_input(self):
        rng = random.Random(5)
        severities = ["Critical", "High", "Medium", "Low", "Negligible", "Unknown", "", None, "odd"]
        for _round in range(60):
            matches = []
            for n in range(rng.randint(0, 40)):
                ids = ["CVE-2024-%04d" % rng.randint(1, 15) for _ in range(rng.choice([0, 0, 1, 2, 3]))]
                matches.append(match(
                    "pkg%d" % rng.randint(1, 8), str(rng.randint(1, 3)), "ADV-%d" % rng.randint(1, 10),
                    rng.choice(severities), [(i, rng.random() < 0.3) for i in sorted(set(ids))],
                    rng.random() < 0.2, rng.choice(["", "1.2", "2.0"]), rng.choice(["", "/a", "/b"]),
                ))
            self.assertEqual(vuln_match.summarize(matches), reference_summarize(matches))


class VulnRowsTests(unittest.TestCase):
    def rows(self, matches=FIXED):
        return {row["cve"]: row for row in vuln_match.vuln_rows(matches)}

    def test_identity_rule(self):
        rows = self.rows()
        # CVE ids, plus the advisory id of the match that carries none.
        self.assertEqual(sorted(rows), ["CVE-2024-0001", "CVE-2024-0002", "CVE-2024-0003", "CVE-2024-0009",
                                        "GHSA-aaaa-bbbb"])
        self.assertEqual(len(rows), vuln_match.summarize(FIXED)["vulns"])

    def test_sorted_by_identity(self):
        ids = [row["cve"] for row in vuln_match.vuln_rows(FIXED)]
        self.assertEqual(ids, sorted(ids))

    def test_shared_cve_takes_the_worst_severity_and_its_package(self):
        row = self.rows()["CVE-2024-0001"]
        self.assertEqual(row["sev_rank"], 4)  # ALAS-2 is Critical, ALAS-1 only High
        self.assertTrue(row["kev"])
        self.assertEqual((row["pkg_name"], row["pkg_version"], row["fix_version"]), ("bash", "5.1", "5.3"))
        self.assertEqual(row["pkg_count"], 1)  # the one package carries both advisories
        self.assertEqual(self.rows()["CVE-2024-0002"]["sev_rank"], 3)

    def test_advisory_without_cve(self):
        row = self.rows()["GHSA-aaaa-bbbb"]
        self.assertEqual((row["sev_rank"], row["kev"], row["pkg_name"], row["fix_version"]), (2, False, "openssl", "3.1"))

    def test_package_count_is_distinct_packages(self):
        row = self.rows()["CVE-2024-0009"]
        self.assertEqual(row["pkg_count"], 2)  # the same library at two paths
        self.assertEqual(row["sev_rank"], 1)  # Negligible folds into Low
        self.assertEqual(row["fix_version"], "")

    def test_representative_order_is_severity_then_exploited_then_name(self):
        cves = [("CVE-2024-1000", False)]
        flagged = [("CVE-2024-1000", True)]
        matches = [
            match("zeta", "1", "A1", "High", cves),
            match("alpha", "1", "A2", "Medium", flagged),
            match("mid", "1", "A3", "High", flagged),
            match("beta", "1", "A4", "High", flagged),
        ]
        row = self.rows(matches)["CVE-2024-1000"]
        # Highest severity wins over a lower exploited one; among High, the exploited ones, then the name.
        self.assertEqual((row["pkg_name"], row["sev_rank"], row["kev"], row["pkg_count"]), ("beta", 3, True, 4))
        # The same input in any order picks the same package.
        for seed in range(5):
            shuffled = list(matches)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(self.rows(shuffled)["CVE-2024-1000"]["pkg_name"], "beta")

    def test_identity_kev_is_any_entry(self):
        matches = [
            match("a", "1", "A1", "Critical", [("CVE-2024-2000", False)]),
            match("b", "1", "A2", "Low", [("CVE-2024-2000", True)]),
        ]
        row = self.rows(matches)["CVE-2024-2000"]
        self.assertTrue(row["kev"])
        self.assertEqual((row["sev_rank"], row["pkg_name"]), (4, "a"))  # severity decides the package, not the flag


class MatchHashTests(unittest.TestCase):
    def test_order_does_not_matter(self):
        shuffled = list(FIXED)
        random.Random(1).shuffle(shuffled)
        self.assertEqual(vuln_match.match_hash(FIXED), vuln_match.match_hash(shuffled))

    def test_cve_order_does_not_matter(self):
        a = match("p", "1", "A", "High", [("CVE-1", False), ("CVE-2", True)])
        b = match("p", "1", "A", "High", [("CVE-2", True), ("CVE-1", False)])
        self.assertEqual(vuln_match.match_hash([a]), vuln_match.match_hash([b]))

    def test_every_stored_field_counts(self):
        base = match("p", "1", "A", "High", [("CVE-1", False)], False, "2", "/x")
        digest = vuln_match.match_hash([base])
        changes = {
            "severity": "Critical", "sev_rank": 4, "fix_version": "3", "fix_state": "wont-fix", "kev": True,
            "path": "/y", "pkg_version": "2", "pkg_type": "deb", "vuln_id": "B", "pkg_name": "q",
            "epss": 0.5, "risk": 3.0, "data_source": "https://x.example/",
            "cves": [{"cve": "CVE-1", "kev": True}], }
        for key, value in changes.items():
            self.assertNotEqual(vuln_match.match_hash([dict(base, **{key: value})]), digest, key)
        self.assertNotEqual(vuln_match.match_hash([base, base]), digest)
        self.assertNotEqual(vuln_match.match_hash([]), digest)

    def test_stable(self):
        self.assertEqual(vuln_match.match_hash(FIXED), vuln_match.match_hash([dict(m) for m in FIXED]))
        self.assertEqual(len(vuln_match.match_hash([])), 64)


if __name__ == "__main__":
    unittest.main()
