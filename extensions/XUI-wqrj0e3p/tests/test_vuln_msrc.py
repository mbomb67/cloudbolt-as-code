"""
Checks for the Microsoft data of the Vulnerabilities XUI (vuln_msrc.py and the two calls added to vuln_tools.py)
without CloudBolt or the network: CloudBolt's modules are stubs, the web is a function that serves canned pages, and
the index lives in a temporary folder.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_msrc.py"

tests/fixtures/msrc_sample.json is cut from the real Security Update Guide API (affectedProduct) and the CISA KEV
feed, fetched on 2026-11-24. The build numbers in the tests are the real November 2026 ones for Windows Server 2025:
the standard update is 10.0.26100.33822 (KB5131080), the hotpatch 10.0.26100.33730 (KB5131077) and the October
baseline 10.0.26100.33650 (KB5126646).

The stubs are in place only while this file imports the extension's modules (and, for the download tests, while one
runs), so another test file's stubs are not disturbed.
"""
import contextlib
import copy
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.join(os.path.dirname(HERE), "io_cloudbolt_vulnerabilities")
PKG = "vuln_s_msrc_pkg"
TMP = tempfile.mkdtemp(prefix="vuln-msrc-test-")


class FakeLogger:
    def __init__(self, *a):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


class RequestException(Exception):
    pass


STUBS = {
    "django": _module("django"),
    "django.conf": _module("django.conf", settings=types.SimpleNamespace(
        PROSERV_DIR=TMP, CLOUDBOLT_OS_USER="u", CLOUDBOLT_OS_GROUP="g")),
    "utilities": _module("utilities"),
    "utilities.logger": _module("utilities.logger", ThreadLogger=FakeLogger),
    "utilities.helpers": _module("utilities.helpers", get_ssl_verification=lambda: True),
    "common": _module("common"),
    "common.methods": _module("common.methods", get_proxies=lambda url: {}),
}

_pkg = types.ModuleType(PKG)
_pkg.__path__ = [PKG_DIR]
sys.modules[PKG] = _pkg
with mock.patch.dict(sys.modules, STUBS):
    vuln_match = importlib.import_module(PKG + ".vuln_match")
    store = importlib.import_module(PKG + ".vuln_store")
    msrc = importlib.import_module(PKG + ".vuln_msrc")
    tools = importlib.import_module(PKG + ".vuln_tools")

with open(os.path.join(HERE, "fixtures", "msrc_sample.json"), encoding="utf-8") as _handle:
    SAMPLE = json.load(_handle)

BASE = "https://api.msrc.microsoft.com/sug/v2.0/en-US/"
# The November 2026 numbers of Windows Server 2025.
STD, HOT, OCT = "10.0.26100.33822", "10.0.26100.33730", "10.0.26100.33650"


def make_index(rows=None, exploited=None, kev=None, fetched_at="2026-11-24T00:00:00Z"):
    acc = msrc._new_acc()
    msrc.reduce_rows(SAMPLE["rows"] if rows is None else rows, acc)
    return msrc.build_index(
        acc, SAMPLE["exploited"] if exploited is None else exploited,
        [item["cveID"] for item in SAMPLE["kev"]["vulnerabilities"]] if kev is None else kev, fetched_at)


def facts(build=OCT, hotfixes=(), core=False, current=None):
    parts = build.split(".")
    return {"kind": "windows-build", "current_build": current or parts[2], "ubr": int(parts[3]), "build": build,
            "installation_type": "Server Core" if core else "Server", "edition_id": "ServerDatacenter",
            "display_version": "24H2", "product_name": "Windows Server 2025 Datacenter", "hotfixes": list(hotfixes)}


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def by_id(result):
    return {item["vuln_id"]: item for item in result["matches"]}


class Pure(unittest.TestCase):
    def test_build_tuple(self):
        self.assertEqual(msrc.build_tuple("10.0.26100.33650"), (10, 0, 26100, 33650))
        self.assertEqual(msrc.build_tuple(" 10.0.20348.5870 "), (10, 0, 20348, 5870))
        for bad in ("", None, "10.0.x.1", "10..1", "10.0.26100.", "KB5131080", "10.0.26100.-1", "1.2.3.4.5.6.7", "٣.٤"):
            self.assertIsNone(msrc.build_tuple(bad), bad)

    def test_builds_compare_numerically_with_four_and_five_digit_updates(self):
        self.assertLess(msrc.build_tuple("10.0.26100.9999"), msrc.build_tuple("10.0.26100.10000"))
        self.assertLess(msrc.build_tuple("10.0.26100.33650"), msrc.build_tuple("10.0.26100.33730"))
        self.assertLess(msrc.build_tuple("10.0.20348.987"), msrc.build_tuple("10.0.20348.5870"))
        self.assertNotEqual(sorted(["10.0.26100.9999", "10.0.26100.10000"]),
                            sorted(["10.0.26100.9999", "10.0.26100.10000"], key=msrc.build_tuple))

    def test_kb_numbers_are_normalised(self):
        for text in ("KB5131077", "kb5131077", "5131077", " KB5131077 "):
            self.assertEqual(msrc.normalise_kb(text), "KB5131077")
        for bad in ("", None, "KB", "KB12", "KB51310770000000", "update", "KB5131077x"):
            self.assertEqual(msrc.normalise_kb(bad), "")

    def test_severity_mapping(self):
        for rating, want in (("Critical", "Critical"), ("Important", "High"), ("Moderate", "Medium"), ("Low", "Low"),
                             ("critical", "Critical"), ("None", "Unknown"), ("", "Unknown"), (None, "Unknown"),
                             ("Negligible", "Unknown")):
            self.assertEqual(msrc.severity_for(rating), want, rating)

    def test_product_mapping(self):
        self.assertEqual(msrc.product_for(facts()), (12436, "Windows Server 2025"))
        self.assertEqual(msrc.product_for(facts(core=True)), (12437, "Windows Server 2025 (Server Core installation)"))
        self.assertEqual(msrc.product_for(facts("10.0.20348.5870")), (11923, "Windows Server 2022"))
        self.assertEqual(msrc.product_for(facts("10.0.20348.5870", core=True)),
                         (11924, "Windows Server 2022 (Server Core installation)"))
        odd = facts()
        odd["installation_type"] = " server core "
        self.assertEqual(msrc.product_for(odd)[0], 12437)
        other = facts()
        other["installation_type"] = "Client"
        self.assertEqual(msrc.product_for(other)[0], 12436)

    def test_unsupported_builds_have_no_product(self):
        for current in ("17763", "19045", "22631", "26200", "", None):
            self.assertIsNone(msrc.product_for(dict(facts(), current_build=current)), current)
        self.assertIsNone(msrc.product_for(None))
        self.assertIsNone(msrc.product_for("26100"))

    def test_products_table_has_the_four_ids_the_design_names(self):
        self.assertEqual(msrc.product_ids(), [11923, 11924, 12436, 12437])

    def test_hotpatch_sub_types(self):
        for name in ("Security Hotpatch Update", "SecurityHotpatchUpdate", "AzureHotpatch", "security hotpatch update"):
            self.assertTrue(msrc._is_hotpatch(name), name)
        for name in ("Security Update", "Standalone Security Update", "Security Only", "", None):
            self.assertFalse(msrc._is_hotpatch(name), name)


class IndexBuild(unittest.TestCase):
    def setUp(self):
        self.index = make_index()
        self.p25 = self.index["products"]["12436"]

    def test_real_november_row_has_standard_and_hotpatch_fixes(self):
        entry = self.p25["cves"]["CVE-2026-100223"]
        self.assertEqual(entry["sev"], "Important")
        self.assertEqual(entry["std"], [[STD, "KB5131080"]])
        self.assertEqual(entry["hp"], [[HOT, "KB5131077"]])
        self.assertIn("KB5131077", self.p25["hotpatch_kbs"])
        self.assertNotIn("KB5131080", self.p25["hotpatch_kbs"])

    def test_two_standard_builds_are_both_kept_in_order(self):
        std = self.p25["cves"]["CVE-2025-54100"]["std"]
        self.assertEqual([b for b, _ in std], ["10.0.26100.7392", "10.0.26100.7462"])

    def test_wrong_windows_version_build_is_ignored_and_the_cve_left_out(self):
        self.assertNotIn("CVE-2025-49735", self.p25["cves"])

    def test_advisories_without_a_fix_or_rating_are_left_out(self):
        self.assertNotIn("ADV260001", self.p25["cves"])  # "Not a vulnerability", rating None
        self.assertNotIn("ADV200013", self.p25["cves"])  # an advisory id with no fix

    def test_cves_without_a_fixing_update_are_left_out(self):
        for product in self.index["products"].values():
            self.assertNotIn("CVE-2013-3900", product["cves"])  # rated, but Microsoft publishes no update for it
            for entry in product["cves"].values():
                self.assertTrue(entry["std"] or entry["hp"])

    def test_core_and_desktop_are_separate_lists(self):
        core, desktop = self.index["products"]["12437"]["cves"], self.p25["cves"]
        self.assertIn("CVE-2026-69822", desktop)
        self.assertNotIn("CVE-2026-69822", core)
        self.assertNotEqual(set(core), set(desktop))
        self.assertEqual(set(self.index["products"]), {"11923", "11924", "12436", "12437"})

    def test_hotpatch_sub_type_spellings_go_to_the_hotpatch_list(self):
        def row(name, kb, build):
            return {"productId": 12436, "cveNumber": "CVE-2026-1001", "severity": "Low", "releaseDate": "2026-10-01T00:00:00Z",
                    "kbArticles": [{"articleName": kb, "downloadName": name, "fixedBuildNumber": build}]}

        acc = msrc._new_acc()
        msrc.reduce_rows([row("SecurityHotpatchUpdate", "5000001", "10.0.26100.100"),
                          row("AzureHotpatch", "5000002", "10.0.26100.90"),
                          row("Standalone Security Update", "5000003", "10.0.26100.120")], acc)
        index = msrc.build_index(acc, [], [], "x")
        entry = index["products"]["12436"]["cves"]["CVE-2026-1001"]
        self.assertEqual([kb for _b, kb in entry["hp"]], ["KB5000002", "KB5000001"])
        self.assertEqual([kb for _b, kb in entry["std"]], ["KB5000003"])

    def test_only_cve_ids_reach_the_index(self):
        def row(cve):
            return {"productId": 12436, "cveNumber": cve, "severity": "Low", "releaseDate": "2026-10-01T00:00:00Z",
                    "kbArticles": [{"articleName": "5000001", "downloadName": "Security Update",
                                    "fixedBuildNumber": "10.0.26100.100"}]}
        acc = msrc._new_acc()
        msrc.reduce_rows([row(cve) for cve in ("ADV200013", "CVE-2026-5001 ", "cve-2026-5002", "CVE-2026-5", "CVE-26-5003",
                                                "<script>", "CVE-2026-5004; drop", "CVE-2026-5005\n")], acc)
        self.assertEqual(sorted(acc[12436]), ["CVE-2026-5001", "CVE-2026-5002", "CVE-2026-5005"])

    def test_stamp_date(self):
        self.assertEqual(msrc.stamp_date("2026-11-10.26edfc27ff"), "2026-11-10")
        self.assertEqual(msrc.stamp_date(self.index["stamp"]), self.index["stamp"].split(".")[0])
        for bad in ("", None, "0000-00-00.abc", "garbage", "2026-11", "x2026-11-10.abc", 5):
            self.assertEqual(msrc.stamp_date(bad), "", bad)

    def test_junk_rows_are_skipped(self):
        acc = msrc._new_acc()
        msrc.reduce_rows([None, "x", {}, {"productId": "abc"}, {"productId": 99999, "cveNumber": "CVE-1-1"},
                          {"productId": 12436, "cveNumber": ""}, {"productId": 12436, "cveNumber": "CVE-2026-2002",
                           "severity": "Low", "kbArticles": ["junk", {"fixedBuildNumber": "10.0.26100.5", "articleName": "x"}]}],
                         acc)
        index = msrc.build_index(acc, [], [], "x")
        self.assertEqual(index["cve_count"], 0)  # the one real row has a fix build but no usable KB: unusable, left out

    def test_stamp_is_stable_and_follows_content(self):
        again = make_index(fetched_at="2027-01-01T00:00:00Z")
        self.assertEqual(again["stamp"], self.index["stamp"])
        self.assertRegex(self.index["stamp"], r"^2026-11-\d\d\.[0-9a-f]{10}$")
        changed_rows = copy.deepcopy(SAMPLE["rows"])
        for row in changed_rows:
            if row["cveNumber"] == "CVE-2026-100223" and row["productId"] == 12436:
                row["kbArticles"][0]["fixedBuildNumber"] = "10.0.26100.33823"
        self.assertNotEqual(make_index(rows=changed_rows)["stamp"], self.index["stamp"])
        self.assertNotEqual(make_index(exploited=["CVE-2026-96951"])["stamp"], self.index["stamp"])
        # A KEV entry for a CVE Windows Server has no row for does not change what any server's result can be.
        unrelated = [item["cveID"] for item in SAMPLE["kev"]["vulnerabilities"]] + ["CVE-2030-0001"]
        self.assertEqual(make_index(kev=unrelated)["stamp"], self.index["stamp"])
        relevant = [item["cveID"] for item in SAMPLE["kev"]["vulnerabilities"]] + ["CVE-2026-100223"]
        self.assertNotEqual(make_index(kev=relevant)["stamp"], self.index["stamp"])

    def test_index_round_trip(self):
        raw = msrc.dump_index(self.index)
        self.assertEqual(msrc.dump_index(self.index), raw)  # deterministic bytes
        self.assertEqual(msrc._inflate(raw), self.index)
        self.assertTrue(msrc._valid_index(self.index))
        self.assertLess(len(raw), 20000)

    def test_index_that_inflates_too_large_is_refused(self):
        raw = msrc.dump_index(self.index)
        with mock.patch.object(msrc, "MAX_INDEX_BYTES", 100):
            with self.assertRaises(ValueError):
                msrc._inflate(raw)


class Match(unittest.TestCase):
    def setUp(self):
        self.index = make_index()

    def test_standard_track_with_the_october_baseline(self):
        result = msrc.match(facts(OCT), self.index)
        self.assertEqual(result["track"], "standard")
        self.assertEqual(result["product"], "Windows Server 2025")
        self.assertEqual(result["db_built"], self.index["stamp"])
        found = by_id(result)
        item = found["CVE-2026-100223"]
        self.assertEqual((item["fix_version"], item["path"], item["fix_state"]), (STD, "KB5131080", "fixed"))
        self.assertEqual(item["pkg_name"], "Windows Server 2025")
        self.assertEqual(item["pkg_type"], "windows")
        self.assertEqual(item["pkg_version"], OCT)
        self.assertEqual(item["severity"], "High")
        self.assertEqual(item["sev_rank"], vuln_match.sev_rank("High"))
        self.assertEqual(item["cves"], [{"cve": "CVE-2026-100223", "kev": False}])
        self.assertEqual(item["data_source"], "https://msrc.microsoft.com/update-guide/vulnerability/CVE-2026-100223")
        self.assertIsNone(item["epss"])
        self.assertIsNone(item["risk"])
        self.assertNotIn("CVE-2026-105016", found)  # fixed by the October baseline itself
        self.assertNotIn("CVE-2026-96951", found)

    def test_server_on_the_hotpatch_build_without_the_hotpatch_id_is_on_the_hotpatch_track(self):
        # Get-HotFix can omit the hotpatch update; the exact build is the second signal.
        result = msrc.match(facts(HOT), self.index)
        self.assertEqual(result["track"], "hotpatch")
        self.assertNotIn("CVE-2026-100223", by_id(result))
        self.assertEqual(msrc.match(facts(OCT), self.index)["track"], "standard")  # the baseline says nothing
        self.assertEqual(msrc.match(facts(STD), self.index)["track"], "standard")
        self.assertIn("10.0.26100.33730", self.index["products"]["12436"]["hotpatch_builds"])
        self.assertNotIn(OCT, self.index["products"]["12436"]["hotpatch_builds"])

    def test_tracks_at_one_build_do_not_share_findings_keys(self):
        standard = msrc.match(facts(OCT), self.index)
        hotpatch = msrc.match(facts(OCT, hotfixes=["KB5131077"]), self.index)
        self.assertEqual((standard["track"], hotpatch["track"]), ("standard", "hotpatch"))
        def keys(result):
            return {(m["pkg_name"], m["pkg_version"], m["pkg_type"], m["vuln_id"]) for m in result["matches"]}
        self.assertTrue(keys(standard) and keys(hotpatch))
        self.assertEqual(keys(standard) & keys(hotpatch), set())
        self.assertEqual({m["pkg_name"] for m in hotpatch["matches"]}, {"Windows Server 2025 (hotpatch)"})
        self.assertEqual({m["pkg_name"] for m in standard["matches"]}, {"Windows Server 2025"})
        core = msrc.match(facts(OCT, hotfixes=["KB5131077"], core=True), self.index)
        self.assertEqual({m["pkg_name"] for m in core["matches"]},
                         {"Windows Server 2025 (Server Core installation) (hotpatch)"})
        self.assertEqual(core["product"], "Windows Server 2025 (Server Core installation)")

    def test_real_builds_decide_the_track(self):
        self.assertEqual(msrc.match(facts("10.0.26100.33730"), self.index)["track"], "hotpatch")
        self.assertEqual(msrc.match(facts("10.0.26100.33650"), self.index)["track"], "standard")

    def test_a_build_that_a_standard_update_also_lists_is_not_a_hotpatch_build(self):
        def row(name, kb, build, cve):
            return {"productId": 12436, "cveNumber": cve, "severity": "Low", "releaseDate": "2026-10-01T00:00:00Z",
                    "kbArticles": [{"articleName": kb, "downloadName": name, "fixedBuildNumber": build}]}
        index = make_index(rows=[row("Security Hotpatch Update", "5000001", "10.0.26100.100", "CVE-2026-4001"),
                                 row("Security Update", "5000002", "10.0.26100.100", "CVE-2026-4002"),
                                 row("Security Hotpatch Update", "5000003", "10.0.26100.200", "CVE-2026-4003")])
        self.assertEqual(index["products"]["12436"]["hotpatch_builds"], ["10.0.26100.200"])

    def test_hotpatch_track_at_the_hotpatch_build_is_clean_of_november(self):
        result = msrc.match(facts(HOT, hotfixes=["KB5131077"]), self.index)
        self.assertEqual(result["track"], "hotpatch")
        found = by_id(result)
        self.assertNotIn("CVE-2026-100223", found)
        self.assertNotIn("CVE-2026-118148", found)

    def test_hotpatch_track_below_the_hotpatch_build_is_pointed_at_the_hotpatch_kb(self):
        found = by_id(msrc.match(facts(OCT, hotfixes=["KB5131077"]), self.index))
        item = found["CVE-2026-100223"]
        self.assertEqual((item["fix_version"], item["path"]), (HOT, "KB5131077"))

    def test_hotpatch_id_spellings(self):
        for spelling in ("KB5131077", "5131077", "kb5131077", " KB5131077 "):
            self.assertEqual(msrc.match(facts(HOT, hotfixes=[spelling]), self.index)["track"], "hotpatch", spelling)
        for other in (["KB5131080"], ["KB5126646"], [], ["junk", 5, None]):
            self.assertEqual(msrc.match(facts(OCT, hotfixes=other), self.index)["track"], "standard", other)

    def test_a_server_on_the_standard_november_update_has_nothing_left(self):
        found = by_id(msrc.match(facts(STD), self.index))
        self.assertEqual(found, {})

    def test_build_equal_to_the_fix_is_fixed_and_one_below_is_not(self):
        self.assertNotIn("CVE-2026-105016", by_id(msrc.match(facts(OCT), self.index)))
        below = by_id(msrc.match(facts("10.0.26100.33649"), self.index))
        self.assertEqual(below["CVE-2026-105016"]["fix_version"], OCT)

    def test_minimum_of_two_standard_builds_decides(self):
        # CVE-2025-54100 lists 26100.7392 and 26100.7462; a server on 7400 is fixed by the earlier one.
        self.assertNotIn("CVE-2025-54100", by_id(msrc.match(facts("10.0.26100.7400"), self.index)))
        item = by_id(msrc.match(facts("10.0.26100.7391"), self.index))["CVE-2025-54100"]
        self.assertEqual((item["fix_version"], item["path"]), ("10.0.26100.7392", "KB5074204"))

    def test_five_digit_update_numbers_compare_as_numbers(self):
        found = by_id(msrc.match(facts("10.0.26100.9999"), self.index))
        self.assertNotIn("CVE-2025-54100", found)  # 9999 is above 7392, not below as text order would say
        self.assertIn("CVE-2026-100223", found)

    def test_server_core_gets_its_own_list(self):
        desktop = by_id(msrc.match(facts("10.0.26100.1"), self.index))
        core = msrc.match(facts("10.0.26100.1", core=True), self.index)
        self.assertEqual(core["product"], "Windows Server 2025 (Server Core installation)")
        self.assertIn("CVE-2026-69822", desktop)
        self.assertNotIn("CVE-2026-69822", by_id(core))
        self.assertEqual({item["pkg_name"] for item in core["matches"]}, {core["product"]})
        self.assertLess(len(core["matches"]), len(desktop))

    def test_server_2022_uses_its_own_builds(self):
        found = by_id(msrc.match(facts("10.0.20348.5800"), self.index))
        self.assertEqual(found["CVE-2026-118148"]["fix_version"], "10.0.20348.5870")
        self.assertEqual(found["CVE-2026-118148"]["path"], "KB5131090")

    def test_known_exploited_is_the_union_of_microsoft_and_cisa(self):
        found = by_id(msrc.match(facts("10.0.26100.1"), self.index))
        self.assertTrue(found["CVE-2026-96951"]["kev"])  # Microsoft says exploited, CISA does not list it
        self.assertTrue(found["CVE-2026-111497"]["kev"])  # CISA lists it, Microsoft does not say so
        self.assertFalse(found["CVE-2026-105016"]["kev"])
        self.assertEqual(found["CVE-2026-111497"]["cves"], [{"cve": "CVE-2026-111497", "kev": True}])
        self.assertTrue(found["CVE-2026-68820"]["kev"])

    def test_unknown_rating_gets_unknown_severity(self):
        rows = copy.deepcopy(SAMPLE["rows"])
        for row in rows:
            if row["cveNumber"] == "CVE-2026-105016":
                row["severity"] = "Odd"
        found = by_id(msrc.match(facts("10.0.26100.1"), make_index(rows=rows)))
        self.assertEqual((found["CVE-2026-105016"]["severity"], found["CVE-2026-105016"]["sev_rank"]), ("Unknown", 0))

    def test_only_hotpatch_fix_listed_still_reaches_a_standard_server(self):
        rows = [{"productId": 12436, "cveNumber": "CVE-2026-3003", "severity": "Critical", "releaseDate": "2026-05-12T00:00:00Z",
                 "kbArticles": [{"articleName": "5000009", "downloadName": "Security Hotpatch Update",
                                 "fixedBuildNumber": "10.0.26100.500"}]}]
        index = make_index(rows=rows)
        item = by_id(msrc.match(facts("10.0.26100.400"), index))["CVE-2026-3003"]
        self.assertEqual(item["fix_version"], "10.0.26100.500")
        self.assertEqual(msrc.match(facts("10.0.26100.500"), index)["matches"], [])

    def test_result_is_deterministic(self):
        first = msrc.match(facts(OCT, hotfixes=["KB1000001", "KB5131077"]), self.index)
        second = msrc.match(facts(OCT, hotfixes=["KB5131077", "KB1000001"]), copy.deepcopy(self.index))
        self.assertEqual(first, second)
        ids = [item["vuln_id"] for item in first["matches"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(vuln_match.match_hash(first["matches"]), vuln_match.match_hash(second["matches"]))
        # The findings keep the shape of a Grype match, so the store and the totals take them unchanged.
        summary = vuln_match.summarize(first["matches"])
        self.assertEqual(summary["findings"], len(first["matches"]))

    def test_build_taken_from_current_build_and_ubr_when_the_string_is_missing_or_wrong(self):
        for text in (None, "", "garbage", "10.0.20348.5870", "10.0.26100"):
            broken = dict(facts(OCT), build=text)
            self.assertEqual(msrc.match(broken, self.index)["matches"][0]["pkg_version"], OCT, text)
        stringy = dict(facts(OCT), build=None, ubr="33650")
        self.assertEqual(msrc.match(stringy, self.index)["matches"][0]["pkg_version"], OCT)

    def test_errors(self):
        with self.assertRaises(msrc.MsrcError) as raised:
            msrc.match(facts("10.0.17763.5000"), self.index)
        self.assertEqual(raised.exception.reason, "unsupported_windows")
        self.assertTrue(raised.exception.public)
        with self.assertRaises(msrc.MsrcError) as raised:
            msrc.match(dict(facts(OCT), build=None, ubr=None), self.index)
        self.assertEqual(raised.exception.reason, "bad_facts")
        with self.assertRaises(msrc.MsrcError) as raised:
            msrc.match(facts(OCT), {"version": 1, "stamp": "s", "products": {}})
        self.assertEqual(raised.exception.reason, "not_ready")

    def test_no_index_means_not_ready(self):
        with mock.patch.object(msrc, "load_index", return_value=None):
            with self.assertRaises(msrc.MsrcError) as raised:
                msrc.match(facts(OCT))
        self.assertEqual(raised.exception.reason, "not_ready")
        self.assertIn("not on this appliance yet", raised.exception.public)


class Files(unittest.TestCase):
    """The index on disk: atomic writes, the cache, damaged files."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="msrc-", dir=TMP)
        patcher = mock.patch.object(msrc, "MSRC_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        msrc._cache.update(key=None, index=None)
        self.addCleanup(msrc._cache.update, key=None, index=None)

    def write_index(self, index=None):
        msrc._atomic_write(os.path.join(self.dir, msrc.INDEX_FILE), msrc.dump_index(index or make_index()))

    def test_absent_index(self):
        self.assertIsNone(msrc.load_index())
        self.assertFalse(msrc.ready())
        state = msrc.status()
        self.assertFalse(state["present"])
        self.assertEqual(set(state), {"present", "built", "checked", "cves", "bytes", "kev_entries", "last_error",
                                      "last_error_at"})

    def test_present_index_without_a_status_file_reports_itself(self):
        index = make_index()
        self.write_index(index)
        self.assertTrue(msrc.ready())
        state = msrc.status()
        self.assertTrue(state["present"])
        self.assertEqual(state["built"], index["stamp"])
        self.assertEqual(state["cves"], index["cve_count"])
        self.assertEqual(state["kev_entries"], len(index["kev"]))
        self.assertGreater(state["bytes"], 0)

    def test_ready_does_not_parse_the_index_when_the_status_says_it_was_written(self):
        self.write_index()
        msrc._update_status(built="s", checked="c", cves=1, bytes=1, kev_entries=1)
        with mock.patch.object(msrc, "load_index", side_effect=AssertionError("parsed")):
            self.assertTrue(msrc.ready())
        os.unlink(os.path.join(self.dir, msrc.INDEX_FILE))
        self.assertFalse(msrc.ready())

    def test_load_is_cached_until_the_file_changes(self):
        self.write_index()
        first = msrc.load_index()
        self.assertIs(msrc.load_index(), first)
        changed = make_index(exploited=[])
        self.write_index(changed)
        os.utime(os.path.join(self.dir, msrc.INDEX_FILE), (1, 1))
        self.assertEqual(msrc.load_index()["stamp"], changed["stamp"])

    def test_damaged_files_never_raise(self):
        path = os.path.join(self.dir, msrc.INDEX_FILE)
        for junk in (b"", b"not gzip", msrc.dump_index({"version": 1})[:20], msrc.dump_index({"version": 2, "stamp": "s", "products": {}}),
                     msrc.dump_index([1, 2])):
            with open(path, "wb") as handle:
                handle.write(junk)
            msrc._cache.update(key=None, index=None)
            self.assertIsNone(msrc.load_index())
            self.assertFalse(msrc.ready())
            self.assertFalse(msrc.status()["present"] and msrc.status()["built"] != "")

    def test_failed_write_leaves_the_old_file_and_no_temp_files(self):
        self.write_index()
        path = os.path.join(self.dir, msrc.INDEX_FILE)
        before = read_bytes(path)
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                msrc._atomic_write(path, b"new")
        self.assertEqual(read_bytes(path), before)
        self.assertEqual(os.listdir(self.dir), [msrc.INDEX_FILE])

    def test_status_reads_a_broken_status_file_as_empty(self):
        with open(os.path.join(self.dir, msrc.STATUS_FILE), "w") as handle:
            handle.write("{not json")
        self.assertEqual(msrc.status()["last_error"], "")


def page(rows, link=None):
    doc = {"value": rows}
    if link:
        doc["@odata.nextLink"] = link
    return json.dumps(doc).encode()


class FakeWeb:
    """Serves the sample like the real API: pages per product (two for Server 2025), the exploited list and the
    CISA feed. Records every URL. fail maps a substring of a URL to an exception or an (status) code."""

    def __init__(self, fail=None, kev_etag='"abc"', exploited=None):
        self.urls = []
        self.headers = []
        self.fail = fail or {}
        self.kev_etag = kev_etag
        self.exploited = SAMPLE["exploited"] if exploited is None else exploited
        self.calls = 0

    def __call__(self, url, headers=None, timeout=None, max_bytes=None):
        self.calls += 1
        self.urls.append(url)
        self.headers.append(headers or {})
        for needle, action in self.fail.items():
            if needle in url:
                if isinstance(action, Exception):
                    raise action
                raise msrc.MsrcError("The Microsoft security data service answered with HTTP {}.".format(action),
                                     "HTTP {}".format(action))
        if "cisa.gov" in url:
            if (headers or {}).get("If-None-Match") == self.kev_etag:
                return 304, b"", {}
            return 200, json.dumps(SAMPLE["kev"]).encode(), {"ETag": self.kev_etag}
        if "affectedProduct" in url:
            pid = int(url.split("productId%20eq%20")[1].split("&")[0])
            rows = [row for row in SAMPLE["rows"] if row["productId"] == pid]
            if "$skip=" in url:
                return 200, page(rows[len(rows) // 2:]), {}
            if pid == 12436:
                return 200, page(rows[:len(rows) // 2], url + "&$skip=1000"), {}
            return 200, page(rows), {}
        if "vulnerability" in url:
            return 200, page([{"cveNumber": cve} for cve in self.exploited]), {}
        raise AssertionError("unexpected url " + url)


class Update(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="msrc-", dir=TMP)
        for patcher in (mock.patch.object(msrc, "MSRC_DIR", self.dir), mock.patch.object(msrc.time, "sleep"),
                        mock.patch.object(store, "scanner_lock", side_effect=AssertionError("scanner lock taken"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        msrc._cache.update(key=None, index=None)
        self.addCleanup(msrc._cache.update, key=None, index=None)
        self.index_path = os.path.join(self.dir, msrc.INDEX_FILE)

    def run_update(self, web, progress=None):
        with mock.patch.object(msrc, "_get", web):
            return msrc.update(progress)

    def test_first_update_builds_the_index_and_follows_the_next_link(self):
        web = FakeWeb()
        said = []
        result = self.run_update(web, said.append)
        self.assertTrue(result["updated"])
        index = msrc.load_index()
        self.assertEqual(result["built"], index["stamp"])
        self.assertEqual(index["stamp"], make_index()["stamp"])  # two pages of 12436 add up to the whole sample
        self.assertIn("CVE-2026-118148", index["products"]["12436"]["cves"])
        self.assertIn("CVE-2026-68820", index["products"]["12436"]["cves"])
        self.assertEqual(len([u for u in web.urls if "affectedProduct" in u]), 5)  # 4 products + one next page
        self.assertEqual(len(web.urls), 7)
        self.assertTrue(said)
        state = msrc.status()
        self.assertEqual((state["built"], state["last_error"]), (index["stamp"], ""))
        self.assertEqual(state["cves"], index["cve_count"])
        self.assertTrue(state["checked"].endswith("Z"))
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".")], [])

    def test_urls_are_encoded_once(self):
        web = FakeWeb()
        self.run_update(web)
        first = web.urls[0]
        self.assertEqual(first, BASE + "affectedProduct?$filter=productId%20eq%2011923&$top=1000")
        for url in web.urls:
            self.assertNotIn(" ", url)
            self.assertNotIn("%25", url)
            self.assertNotIn("$select", url)
        self.assertIn(BASE + "vulnerability?$filter=exploited%20eq%20'Yes'&$top=1000", web.urls)

    def test_unchanged_data_does_not_change_the_stamp(self):
        first = self.run_update(FakeWeb())
        raw = read_bytes(self.index_path)
        second = self.run_update(FakeWeb())
        self.assertFalse(second["updated"])
        self.assertEqual(second["built"], first["built"])
        self.assertEqual(read_bytes(self.index_path), raw)

    def test_changed_data_changes_the_stamp(self):
        first = self.run_update(FakeWeb())
        second = self.run_update(FakeWeb(exploited=["CVE-2026-96951"]))
        self.assertTrue(second["updated"])
        self.assertNotEqual(second["built"], first["built"])
        self.assertEqual(msrc.load_index()["stamp"], second["built"])

    def test_kev_is_asked_conditionally_and_not_modified_reuses_the_list(self):
        self.run_update(FakeWeb())
        web = FakeWeb()
        self.run_update(web)
        self.assertEqual(web.headers[-1].get("If-None-Match"), '"abc"')
        self.assertEqual(len(msrc.load_index()["kev"]), len(SAMPLE["kev"]["vulnerabilities"]))

    def failing_update_leaves_the_index(self, web, wanted, **limits):
        self.run_update(FakeWeb())
        raw = read_bytes(self.index_path)
        stamp = msrc.load_index()["stamp"]
        with (mock.patch.multiple(msrc, **limits) if limits else contextlib.nullcontext()), self.assertRaises(msrc.MsrcError) as raised:
            self.run_update(web)
        self.assertEqual(read_bytes(self.index_path), raw)
        self.assertEqual(msrc.load_index()["stamp"], stamp)
        state = msrc.status()
        self.assertTrue(state["last_error"])
        self.assertIn(wanted, state["last_error"])
        self.assertTrue(state["last_error_at"].endswith("Z"))
        self.assertEqual(state["built"], stamp)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".")], [])
        return raised.exception

    def test_http_403_keeps_the_old_index_and_says_why(self):
        self.failing_update_leaves_the_index(FakeWeb(fail={"affectedProduct": 403}), "HTTP 403")

    def test_timeout_keeps_the_old_index(self):
        err = msrc.MsrcError("CloudBolt could not download the Microsoft security data. The appliance needs outbound access.",
                             "ReadTimeout: timed out")
        self.failing_update_leaves_the_index(FakeWeb(fail={"vulnerability": err}), "outbound access")

    def test_failure_on_the_second_page_is_a_partial_failure_and_keeps_the_old_index(self):
        self.failing_update_leaves_the_index(FakeWeb(fail={"$skip=": 500}), "HTTP 500")

    def test_failure_on_the_last_request_keeps_the_old_index(self):
        self.failing_update_leaves_the_index(FakeWeb(fail={"cisa.gov": 503}), "HTTP 503")

    def test_unexpected_exception_is_wrapped_and_recorded(self):
        error = self.failing_update_leaves_the_index(FakeWeb(fail={"affectedProduct": ValueError("boom")}), "unexpectedly")
        self.assertIn("ValueError", error.detail)

    def test_next_link_to_another_host_is_not_followed(self):
        # _get itself refuses the host; the fake bypasses _get, so check the real function directly.
        with self.assertRaises(msrc.MsrcError):
            msrc._get("https://example.com/steal")
        with self.assertRaises(msrc.MsrcError):
            msrc._get("http://api.msrc.microsoft.com/sug/v2.0/en-US/vulnerability")

    def test_a_truncated_listing_does_not_replace_the_index(self):
        class Cut(FakeWeb):
            def __call__(self, url, headers=None, timeout=None, max_bytes=None):
                if "affectedProduct" in url and "12436" in url and "$skip=" not in url:
                    rows = [row for row in SAMPLE["rows"] if row["productId"] == 12436]
                    return 200, page(rows[:len(rows) // 3]), {}  # the next link is lost too
                if "$skip=" in url:
                    raise AssertionError("not followed")
                return FakeWeb.__call__(self, url, headers, timeout, max_bytes)

        error = self.failing_update_leaves_the_index(Cut(), "incomplete")
        self.assertIn("Windows Server 2025", error.public)
        self.assertIn("12436", error.detail)

    def test_a_small_drop_is_accepted_and_no_current_index_means_no_comparison(self):
        first = make_index()
        small = copy.deepcopy(first)
        cves = small["products"]["12436"]["cves"]
        for cve in sorted(cves)[:max(1, len(cves) // 10)]:
            del cves[cve]
        msrc._refuse_truncated(first, small)  # about 10% fewer: fine
        msrc._refuse_truncated(None, small)
        big = copy.deepcopy(first)
        big["products"]["12436"]["cves"] = {}
        with self.assertRaises(msrc.MsrcError):
            msrc._refuse_truncated(first, big)

    def test_status_write_failing_after_the_replace_still_reports_the_real_stamp(self):
        with mock.patch.object(msrc, "_update_status", side_effect=OSError("disk")):
            result = self.run_update(FakeWeb())
        self.assertTrue(result["updated"])
        self.assertEqual(msrc.status()["built"], result["built"])
        self.assertTrue(msrc.ready())
        # An older status record is not believed either: the second update's status write fails too.
        self.run_update(FakeWeb())
        old = msrc.status()["built"]
        with mock.patch.object(msrc, "_update_status", side_effect=OSError("disk")):
            second = self.run_update(FakeWeb(exploited=["CVE-2026-96951"]))
        self.assertNotEqual(second["built"], old)
        state = msrc.status()
        self.assertEqual(state["built"], second["built"])
        self.assertTrue(state["present"])
        self.assertEqual(state["cves"], msrc.load_index()["cve_count"])

    def test_empty_listing_for_a_product_is_an_error(self):
        class Empty(FakeWeb):
            def __call__(self, url, headers=None, timeout=None, max_bytes=None):
                if "affectedProduct" in url and "12437" in url:
                    return 200, page([]), {}
                return FakeWeb.__call__(self, url, headers, timeout, max_bytes)

        self.failing_update_leaves_the_index(Empty(), "no updates for Windows Server 2025 (Server Core")

    def test_total_time_is_capped(self):
        self.failing_update_leaves_the_index(FakeWeb(), "longer than", TOTAL_TIMEOUT_S=-1)

    def test_page_count_is_capped(self):
        class Endless(FakeWeb):
            def __call__(self, url, headers=None, timeout=None, max_bytes=None):
                if "affectedProduct" in url:
                    return 200, page([], url + "&$skip=1"), {}
                return FakeWeb.__call__(self, url, headers, timeout, max_bytes)

        self.failing_update_leaves_the_index(Endless(), "more pages", MAX_PAGES=3)

    def test_total_size_is_capped(self):
        self.failing_update_leaves_the_index(FakeWeb(), "larger than expected", MAX_TOTAL_BYTES=10)

    def test_error_text_is_redacted(self):
        noisy = msrc.MsrcError("x", "y")
        self.assertEqual(msrc._tail("failed https://user:secret@proxy.example:3128/path now"), "failed <url> now")
        self.assertEqual(msrc._tail("token abcdefghijklmnopqrstu.abcdefghijklmnopqrstu end"), "token <token> end")
        self.assertNotIn("secret", msrc._tail("bad\x00 http://u:secret@h/ " + "z" * 5000))
        self.assertLessEqual(len(msrc._tail("a" * 9000)), msrc.DETAIL_TAIL)
        self.assertEqual(noisy.public, "x")

    def test_recorded_error_is_cleared_by_the_next_success(self):
        with self.assertRaises(msrc.MsrcError):
            self.run_update(FakeWeb(fail={"affectedProduct": 403}))
        self.assertIn("403", msrc.status()["last_error"])
        self.assertFalse(msrc.status()["present"])
        self.run_update(FakeWeb())
        self.assertEqual(msrc.status()["last_error"], "")


class FakeResponse:
    def __init__(self, code, body=b"", headers=None):
        self.status_code = code
        self._body = body
        self.headers = headers or {}

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), 7):
            yield self._body[start:start + 7]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Download(unittest.TestCase):
    """_get against a fake requests module."""

    def run_get(self, response, **kwargs):
        fake = _module("requests", RequestException=RequestException)
        calls = []

        def get(url, **options):
            calls.append((url, options))
            if isinstance(response, Exception):
                raise response
            return response

        fake.get = get
        with mock.patch.dict(sys.modules, dict(STUBS, requests=fake)):
            try:
                return msrc._get(BASE + "affectedProduct?$filter=productId%20eq%2012436&$top=1000", **kwargs), calls
            except msrc.MsrcError as exc:
                return exc, calls

    def test_ok(self):
        (code, body, headers), calls = self.run_get(FakeResponse(200, b'{"value": []}', {"ETag": "x"}))
        self.assertEqual((code, body, headers), (200, b'{"value": []}', {"ETag": "x"}))
        options = calls[0][1]
        self.assertEqual(options["headers"]["Accept"], "application/json")
        self.assertTrue(options["stream"])
        self.assertIn("timeout", options)
        self.assertIn("verify", options)

    def test_not_modified(self):
        (code, body, _h), _calls = self.run_get(FakeResponse(304), headers={"If-None-Match": '"a"'})
        self.assertEqual((code, body), (304, b""))

    def test_403_is_explained(self):
        error, _calls = self.run_get(FakeResponse(403, b"<html>Access Denied</html>"))
        self.assertIsInstance(error, msrc.MsrcError)
        self.assertIn("403", error.public)
        self.assertIn("firewall", error.public)
        self.assertNotIn("http", error.public.lower().replace("http 403", ""))

    def test_other_status(self):
        error, _calls = self.run_get(FakeResponse(500, b"oops"))
        self.assertIn("HTTP 500", error.public)

    def test_network_error_names_the_hosts_and_hides_the_url(self):
        error, _calls = self.run_get(RequestException("timed out connecting to https://user:pw@proxy:3128/x"))
        self.assertIn("api.msrc.microsoft.com", error.public)
        self.assertIn("www.cisa.gov", error.public)
        self.assertNotIn("pw", error.detail)

    def test_oversized_answer_is_refused(self):
        error, _calls = self.run_get(FakeResponse(200, b"x" * 100), max_bytes=50)
        self.assertIsInstance(error, msrc.MsrcError)
        self.assertIn("larger than expected", error.public)

    def test_json_checks(self):
        with self.assertRaises(msrc.MsrcError):
            msrc._json(b"<html>", "x")
        with self.assertRaises(msrc.MsrcError):
            msrc._json(b"[1]", "x")
        self.assertEqual(msrc._json(b'{"a": 1}', "x"), {"a": 1})

    def test_a_redirect_is_an_error_and_is_never_followed(self):
        response = FakeResponse(302, b"", {"Location": "https://example.com/steal"})
        error, calls = self.run_get(response)
        self.assertIsInstance(error, msrc.MsrcError)
        self.assertIn("redirect", error.public)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][1]["allow_redirects"], False)
        self.assertIn("HTTP 302", error.detail)

    def test_next_link_encoding_keeps_existing_escapes_and_encodes_raw_spaces(self):
        mixed = BASE + "affectedProduct?$filter=productId eq 12436 and name eq 'a%20b'&$skip=1000"
        self.assertEqual(msrc._next_link(mixed),
                         BASE + "affectedProduct?$filter=productId%20eq%2012436%20and%20name%20eq%20'a%20b'&$skip=1000")
        self.assertNotIn("%25", msrc._next_link(mixed))

    def test_next_link_encoding(self):
        self.assertEqual(msrc._next_link(None), "")
        already = BASE + "affectedProduct?$filter=productId%20eq%2012436&$skip=1000"
        self.assertEqual(msrc._next_link(already), already)
        self.assertEqual(msrc._next_link(BASE + "affectedProduct?$filter=productId eq 12436&$skip=1000"),
                         BASE + "affectedProduct?$filter=productId%20eq%2012436&$skip=1000")


class SetupContract(unittest.TestCase):
    """setup() runs the Microsoft update even when the scanner part fails, and carries its result."""

    def run_setup(self, grype_error=None, msrc_result=None):
        error = msrc_result if isinstance(msrc_result, Exception) else None
        with mock.patch.object(tools, "_setup_grype", side_effect=grype_error,
                               return_value={"updated": True, "built": "d"}),                 mock.patch.object(tools.vuln_msrc, "update", side_effect=error,
                                  return_value=None if error else (msrc_result or {"updated": True, "built": "m"})) as update:
            try:
                return tools.setup(), update
            except Exception as exc:
                return exc, update

    def test_both_succeed(self):
        result, update = self.run_setup()
        self.assertEqual(result["db"], {"updated": True, "built": "d"})
        self.assertEqual(result["msrc"], {"updated": True, "built": "m"})
        self.assertEqual(update.call_count, 1)

    def test_scanner_failure_still_updates_microsoft_and_carries_the_result(self):
        failure = tools.ToolError("Could not reach github.com.", "detail")
        raised, update = self.run_setup(grype_error=failure)
        self.assertIs(raised, failure)
        self.assertEqual(update.call_count, 1)
        self.assertEqual(raised.msrc, {"updated": True, "built": "m"})

    def test_both_fail(self):
        failure = tools.ToolError("Could not reach github.com.")
        raised, _update = self.run_setup(grype_error=failure, msrc_result=msrc.MsrcError("Blocked.", "HTTP 403"))
        self.assertIs(raised, failure)
        self.assertEqual(raised.msrc, {"updated": False, "built": "", "error": "Blocked."})

    def test_unexpected_scanner_exception_also_carries_msrc(self):
        raised, _update = self.run_setup(grype_error=RuntimeError("boom"))
        self.assertIsInstance(raised, RuntimeError)
        self.assertEqual(raised.msrc["built"], "m")

    def test_an_unexpected_microsoft_exception_is_reported_not_raised(self):
        result, _update = self.run_setup(msrc_result=ValueError("boom"))
        self.assertEqual(result["msrc"]["updated"], False)
        self.assertTrue(result["msrc"]["error"])

    def test_tool_error_has_msrc_none_by_default(self):
        self.assertIsNone(tools.ToolError("x").msrc)


class ToolsWiring(unittest.TestCase):
    """vuln_tools: status() carries the Microsoft data, update_microsoft wraps update, setup keeps going."""

    tools = tools

    def test_status_has_microsoft_fields_and_keeps_ready_for_grype(self):
        with mock.patch.object(self.tools.vuln_msrc, "status", return_value={"present": True, "built": "s"}), \
                mock.patch.object(self.tools.vuln_msrc, "ready", return_value=True):
            state = self.tools.status()
        self.assertTrue(state["ready_windows"])
        self.assertEqual(state["msrc"], {"present": True, "built": "s"})
        self.assertFalse(state["ready"])  # no Grype in the test folder: "ready" still means Grype only

    def test_update_microsoft_wraps_update_and_takes_no_scanner_lock(self):
        seen = []
        with mock.patch.object(self.tools.vuln_msrc, "update", side_effect=lambda progress=None: seen.append(progress) or
                               {"updated": True, "built": "b"}), \
                mock.patch.object(store, "scanner_lock", side_effect=AssertionError("scanner lock")):
            self.assertEqual(self.tools.update_microsoft("cb"), {"updated": True, "built": "b"})
        self.assertEqual(seen, ["cb"])

    def test_microsoft_failure_does_not_fail_the_linux_setup(self):
        tools = self.tools
        locked = []

        @contextlib.contextmanager
        def lock(exclusive):
            locked.append(exclusive)
            yield

        error = msrc.MsrcError("Blocked.", "HTTP 403")
        with mock.patch.object(tools, "_locked", lock), mock.patch.object(tools, "_install_grype", return_value=False), \
                mock.patch.object(tools, "_install_syft"), mock.patch.object(tools, "_update_status"), \
                mock.patch.object(tools, "db_update", return_value={"updated": False, "built": "d"}), \
                mock.patch.object(store, "ensure_dir"), \
                mock.patch.object(tools.vuln_msrc, "update", side_effect=error):
            result = tools.setup()
        self.assertEqual(result["db"], {"updated": False, "built": "d"})
        self.assertEqual(result["msrc"], {"updated": False, "built": "", "error": "Blocked."})
        self.assertEqual(locked, [True])  # the scanner lock was taken once, for the Linux part only


if __name__ == "__main__":
    unittest.main()
