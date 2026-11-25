"""
Checks for the decision functions of vuln_patch.sh without a server: the script is cut at its dispatch marker,
the part above it (functions only) is sourced in bash, and the commands it calls (rpm, dpkg-query, apt-get, dnf,
uname, stat, id) are shell functions defined by each test, so nothing runs on the machine the test runs on.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_patch_script.py"

What stays untested here: the real output of the package managers and of rpm/dpkg-query on a live server.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SH_PATH = os.path.join(os.path.dirname(HERE), "io_cloudbolt_vulnerabilities", "vuln_patch.sh")

# The packages installed on the Ubuntu 22.04 test server: the old 1035 set, the running and newest 1070 set,
# the metapackages and a few packages that are not kernel packages.
UBUNTU_OLD = ["linux-image-6.8.0-1035-aws", "linux-modules-6.8.0-1035-aws", "linux-headers-6.8.0-1035-aws",
              "linux-tools-6.8.0-1035-aws", "linux-aws-6.8-tools-6.8.0-1035", "linux-aws-6.8-headers-6.8.0-1035"]
UBUNTU_NEW = [name.replace("1035", "1070") for name in UBUNTU_OLD]
UBUNTU_OTHER = ["linux-image-aws", "linux-headers-aws", "linux-aws", "linux-libc-dev", "linux-base", "bash",
                "libc6", "linux-firmware"]


def shell(snippet):
    """Run snippet in bash after the script's functions are defined; returns stdout."""
    with open(SH_PATH, encoding="ascii") as handle:
        text = handle.read()
    head = text[:text.index("# ---- dispatch")]
    program = "MODE=poll\nRUN_ID=patch-script-test-0001\n" + head + "\n" + snippet + "\n"
    done = subprocess.run([shutil.which("bash")], input=program.encode("ascii"), capture_output=True, timeout=120)
    return done.stdout.decode(errors="replace")


def words(text):
    return text.split()


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name.replace("\\", "/")

    def write(self, name, lines):
        with open(os.path.join(self.tmp.name, name), "w", newline="\n") as handle:
            handle.write("".join(line + "\n" for line in lines))

    def read(self, name):
        path = os.path.join(self.tmp.name, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="ascii") as handle:
            return handle.read().split()


class SnapshotTests(Base):
    def test_ubuntu_counts_changed_and_new_packages(self):
        self.write("a", ["bash 5.1-6", "libc6 2.35-0", "openssl 3.0.2-0"])
        self.write("b", ["bash 5.1-6", "libc6 2.35-1", "openssl 3.0.2-0", "linux-image-x 1"])
        snippet = ("DISTRO=ubuntu\nRUN={r}\ndpkg-query() { cat \"$LISTFILE\"; }\n"
                   "LISTFILE={r}/a; snapshot {r}/before\nLISTFILE={r}/b; snapshot {r}/after\n"
                   "echo \"CHANGED|$(count_new {r}/before {r}/after)\"\n"
                   "echo \"SAME|$(count_new {r}/before {r}/before)\"\n"
                   "echo \"GONE|$(count_gone {r}/after {r}/before)\"\n")
        text = shell(snippet.replace("{r}", self.root))
        self.assertIn("CHANGED|2", words(text))
        self.assertIn("SAME|0", words(text))
        self.assertIn("GONE|2", words(text))

    def test_rpm_counts_by_name_and_epoch_version(self):
        self.write("a", ["bash 0:5.1-6.x86_64", "kernel-core 0:5.14.0-1.x86_64"])
        self.write("b", ["bash 0:5.1-6.x86_64", "kernel-core 0:5.14.0-1.x86_64", "kernel-core 0:5.14.0-2.x86_64"])
        snippet = ("DISTRO=rhel\nrpm() { cat \"$LISTFILE\"; }\n"
                   "LISTFILE={r}/a; snapshot {r}/before\nLISTFILE={r}/b; snapshot {r}/after\n"
                   "echo \"CHANGED|$(count_new {r}/before {r}/after)\"\n"
                   "echo \"SAME|$(count_new {r}/after {r}/after)\"\n").replace("{r}", self.root)
        text = words(shell(snippet))
        self.assertIn("CHANGED|1", text)
        self.assertIn("SAME|0", text)

    def test_the_snapshot_asks_rpm_for_the_epoch_and_dpkg_for_the_binary_package(self):
        text = shell("DISTRO=rhel\nrpm() { echo \"ARGS $*\"; }\nsnapshot {r}/x; cat {r}/x\n".replace("{r}", self.root))
        self.assertIn("%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}", text)
        text = shell("DISTRO=ubuntu\ndpkg-query() { echo \"ARGS $*\"; }\nsnapshot {r}/x; cat {r}/x\n".replace(
            "{r}", self.root))
        self.assertIn("${binary:Package} ${Version}", text)


class NewestKernelTests(Base):
    def newest(self, lines, distro="rhel"):
        self.write("rpm", lines)
        snippet = "DISTRO={d}\nrpm() { cat {r}/rpm; }\necho \"NEWEST|$(newest_kernel)\"\n".replace(
            "{d}", distro).replace("{r}", self.root)
        for word in words(shell(snippet)):
            if word.startswith("NEWEST|"):
                return word.split("|", 1)[1]
        return None

    def test_red_hat_style_names(self):
        self.assertEqual(self.newest([
            "bash 5.1.8-6.el9.x86_64", "kernel 5.14.0-362.8.1.el9_3.x86_64", "kernel-core 5.14.0-362.8.1.el9_3.x86_64",
            "kernel-modules 5.14.0-362.8.1.el9_3.x86_64", "kernel-core 5.14.0-427.13.1.el9_4.x86_64",
            "kernel 5.14.0-427.13.1.el9_4.x86_64", "kernel-tools 5.14.0-999.el9.x86_64",
            "kernel-devel 5.14.0-999.el9.x86_64"]), "5.14.0-427.13.1.el9_4.x86_64")

    def test_oracle_linux_uek_names(self):
        self.assertEqual(self.newest([
            "kernel-uek 5.15.0-206.153.7.1.el9uek.x86_64", "kernel-uek-core 5.15.0-209.161.7.2.el9uek.x86_64",
            "kernel-uek-modules 5.15.0-999.el9uek.x86_64", "kernel-uek-devel 5.15.0-999.el9uek.x86_64"]),
            "5.15.0-209.161.7.2.el9uek.x86_64")

    def test_amazon_linux_2023_names_the_kernel_after_its_series(self):
        # kernel6.18 was observed on a test server, where "rpm -q kernel" says the package is not installed.
        self.assertEqual(self.newest([
            "kernel6.12 6.12.40-61.114.amzn2023.x86_64", "kernel6.18 6.18.62-71.115.amzn2023.x86_64",
            "kernel6.18-modules-core 6.18.62-71.115.amzn2023.x86_64", "kernel-headers 6.18.62-1.amzn2023.x86_64",
            "kernel-tools 6.18.62-1.amzn2023.x86_64", "kernel6.1 6.1.99-100.amzn2023.x86_64"], "amzn2023"),
            "6.18.62-71.115.amzn2023.x86_64")

    def test_no_kernel_package_is_an_empty_value_never_a_wrong_one(self):
        self.assertEqual(self.newest(["bash 5.1.8-6.el9.x86_64", "kernel-tools 5.14.0-1.x86_64"]), "")

    def test_ubuntu_takes_the_newest_installed_image_including_unsigned(self):
        self.write("dpkg", ["ii linux-image-6.8.0-1035-aws", "ii linux-image-unsigned-6.8.0-1070-aws",
                            "ii linux-image-aws", "rc linux-image-6.8.0-1100-aws", "ii bash"])
        snippet = "DISTRO=ubuntu\ndpkg-query() { cat {r}/dpkg; }\necho \"NEWEST|$(newest_kernel)\"\n".replace(
            "{r}", self.root)
        self.assertIn("NEWEST|6.8.0-1070-aws", words(shell(snippet)))


class UbuntuSelectionTests(Base):
    def select(self, running, installed):
        self.write("installed", installed)
        snippet = "cat {r}/installed | select_old_debs {run}\n".replace("{r}", self.root).replace("{run}", running)
        return sorted(words(shell(snippet)))

    def test_the_old_set_is_selected_and_nothing_else(self):
        chosen = self.select("6.8.0-1070-aws", UBUNTU_OLD + UBUNTU_NEW + UBUNTU_OTHER)
        self.assertEqual(chosen, sorted(UBUNTU_OLD))
        for name in UBUNTU_NEW + UBUNTU_OTHER:
            self.assertNotIn(name, chosen)

    def test_unsigned_extra_and_cloud_tools_forms_are_covered(self):
        old = ["linux-image-unsigned-6.8.0-1035-aws", "linux-modules-extra-6.8.0-1035-aws",
               "linux-cloud-tools-6.8.0-1035-aws", "linux-buildinfo-6.8.0-1035-aws"]
        chosen = self.select("6.8.0-1070-aws", old + UBUNTU_NEW + UBUNTU_OTHER)
        self.assertEqual(chosen, sorted(old))

    def test_a_longer_release_number_is_not_a_prefix_match(self):
        # -1035 must not select a package of release -10350.
        installed = ["linux-image-6.8.0-1035-aws", "linux-modules-6.8.0-10350-aws", "linux-image-6.8.0-1070-aws"]
        self.assertEqual(self.select("6.8.0-1070-aws", installed), ["linux-image-6.8.0-1035-aws"])

    def test_the_same_release_in_another_flavour_is_left_alone(self):
        installed = ["linux-image-6.8.0-1070-aws", "linux-image-6.8.0-1070-generic", "linux-modules-6.8.0-1070-generic"]
        self.assertEqual(self.select("6.8.0-1070-aws", installed), [])

    def test_nothing_old_installed_selects_nothing(self):
        self.assertEqual(self.select("6.8.0-1070-aws", UBUNTU_NEW + UBUNTU_OTHER), [])


class OldKernelsModeTests(Base):
    """mode_oldkernels with the package tools stubbed: the installed set lives in files the stubs change."""

    UBUNTU_STUBS = r'''
id() { echo 0; }
stat() { echo '0 700'; }
need_distro() { :; }
uname() { echo "$RUNNING"; }
dpkg-query() {
    case "$*" in
        *Status-Abbrev*) sed 's/^/ii /' "$T/installed" ;;
        *) sed 's/$/ 1.0/' "$T/installed" ;;
    esac
}
apt-get() {
    local a purge=0 simulate=0
    for a in "$@"; do
        [ "$a" = purge ] && purge=1
        [ "$a" = -s ] && simulate=1
    done
    [ "$purge" = 1 ] || return 0
    if [ "$simulate" = 1 ]; then
        for a in "$@"; do
            case "$a" in linux-*) echo "Remv $a [1]" ;; esac
        done
        [ -z "${EXTRA-}" ] || echo "Remv $EXTRA [1]"
        return 0
    fi
    echo "PURGED $*" >> "$T/calls"
    for a in "$@"; do
        case "$a" in linux-*) grep -vxF "$a" "$T/installed" > "$T/installed.new"; mv "$T/installed.new" "$T/installed" ;; esac
    done
    return 0
}
DISTRO=ubuntu
'''

    def run_mode(self, running, installed, extra=""):
        self.write("installed", installed)
        snippet = ("T={r}\nRUN={r}/run\nRUNNING={run}\nEXTRA={extra}\n" + self.UBUNTU_STUBS + "mode_oldkernels\n").replace(
            "{r}", self.root).replace("{run}", running).replace("{extra}", extra or "''")
        return shell(snippet)

    def test_the_old_set_is_purged_and_counted_from_the_snapshots(self):
        text = self.run_mode("6.8.0-1070-aws", UBUNTU_OLD + UBUNTU_NEW + UBUNTU_OTHER)
        self.assertIn("CBVULN|removed|6", text)
        self.assertIn("CBVULN|exit|0", text)
        self.assertIn("CBVULN|kernel_running|6.8.0-1070-aws", text)
        self.assertIn("CBVULN|kernel_newest|6.8.0-1070-aws", text)
        left = self.read("installed")
        self.assertEqual(sorted(left), sorted(UBUNTU_NEW + UBUNTU_OTHER))
        call = " ".join(self.read("calls"))
        self.assertIn("DPkg::Lock::Timeout=600", call)
        for name in UBUNTU_OLD:
            self.assertIn(name, call)
        for name in UBUNTU_NEW + ["linux-image-aws", "linux-headers-aws"]:
            self.assertNotIn(name + " ", call + " ")

    def test_a_server_not_running_its_newest_kernel_removes_nothing(self):
        installed = UBUNTU_OLD + UBUNTU_NEW + UBUNTU_OTHER
        text = self.run_mode("6.8.0-1035-aws", installed)
        self.assertIn("the server is not running its newest kernel", text)
        self.assertIn("CBVULN|removed|0", text)
        self.assertIn("CBVULN|exit|0", text)
        self.assertIn("CBVULN|kernel_running|6.8.0-1035-aws", text)
        self.assertIn("CBVULN|kernel_newest|6.8.0-1070-aws", text)
        self.assertEqual(self.read("calls"), [])
        self.assertEqual(sorted(self.read("installed")), sorted(installed))

    def test_a_simulation_that_removes_another_package_removes_nothing(self):
        installed = UBUNTU_OLD + UBUNTU_NEW + UBUNTU_OTHER
        text = self.run_mode("6.8.0-1070-aws", installed, extra="ubuntu-server")
        self.assertIn("purging them would also remove ubuntu-server", text)
        self.assertIn("CBVULN|removed|0", text)
        self.assertEqual(self.read("calls"), [])
        self.assertEqual(sorted(self.read("installed")), sorted(installed))

    def test_nothing_old_installed_reports_zero(self):
        text = self.run_mode("6.8.0-1070-aws", UBUNTU_NEW + UBUNTU_OTHER)
        self.assertNotIn("CBVULN|note|", text)
        self.assertIn("CBVULN|removed|0", text)
        self.assertIn("CBVULN|exit|0", text)
        self.assertEqual(self.read("calls"), [])

    def test_a_server_whose_newest_kernel_cannot_be_told_removes_nothing(self):
        text = self.run_mode("6.8.0-1070-aws", UBUNTU_OTHER)
        self.assertIn("could not be told", text)
        self.assertIn("CBVULN|removed|0", text)
        self.assertIn("CBVULN|kernel_newest|", text)


class DnfOldKernelsTests(Base):
    STUBS = r'''
id() { echo 0; }
stat() { echo '0 700'; }
need_distro() { :; }
uname() { echo "$RUNNING"; }
rpm() {
    case "$*" in
        *EPOCHNUM*) awk '{print $1, "0:" $2}' "$T/kernels" ;;
        *) cat "$T/kernels" ;;
    esac
}
dnf() {
    echo "DNF $*" >> "$T/calls"
    if [ "${DNF_RC:-0}" != 0 ]; then echo "$DNF_MSG"; return "$DNF_RC"; fi
    tail -n 1 "$T/kernels" > "$T/kernels.new"; mv "$T/kernels.new" "$T/kernels"
    return 0
}
DISTRO=rhel
'''

    def run_mode(self, running, kernels, rc=0, message=""):
        self.write("kernels", kernels)
        snippet = ("T={r}\nRUN={r}/run\nRUNNING={run}\nDNF_RC={rc}\nDNF_MSG='{msg}'\n" + self.STUBS
                   + "mode_oldkernels\n").replace(
            "{r}", self.root).replace("{run}", running).replace("{rc}", str(rc)).replace("{msg}", message)
        return shell(snippet)

    FOUR = ["kernel-core 5.14.0-1.el9.x86_64", "kernel-core 5.14.0-2.el9.x86_64", "kernel-core 5.14.0-3.el9.x86_64",
            "kernel-core 5.14.0-4.el9.x86_64"]

    def test_old_kernels_beyond_the_limit_are_removed_and_counted(self):
        text = self.run_mode("5.14.0-4.el9.x86_64", self.FOUR)
        self.assertEqual(self.read("calls"), ["DNF", "-y", "remove", "--oldinstallonly"])
        self.assertIn("CBVULN|removed|3", text)
        self.assertIn("CBVULN|exit|0", text)
        self.assertIn("CBVULN|kernel_newest|5.14.0-4.el9.x86_64", text)

    def test_two_kernels_with_the_running_one_newest_still_call_dnf(self):
        # dnf's remove --oldinstallonly does not depend on installonly_limit.
        text = self.run_mode("5.14.0-2.el9.x86_64", self.FOUR[:2])
        self.assertEqual(self.read("calls"), ["DNF", "-y", "remove", "--oldinstallonly"])
        self.assertIn("CBVULN|removed|1", text)

    def test_no_old_installonly_packages_is_removed_zero_and_success(self):
        text = self.run_mode("5.14.0-1.el9.x86_64", self.FOUR[:1], rc=1,
                             message="Error: No old installonly packages found for removal.")
        self.assertEqual(self.read("calls"), ["DNF", "-y", "remove", "--oldinstallonly"])
        self.assertNotIn("CBVULN|note|", text)
        self.assertIn("CBVULN|removed|0", text)
        self.assertIn("CBVULN|exit|0", text)
        self.assertNotIn("CBVULN|failure|", text)

    def test_a_server_not_running_its_newest_kernel_is_left_alone(self):
        text = self.run_mode("5.14.0-1.el9.x86_64", self.FOUR)
        self.assertEqual(self.read("calls"), [])
        self.assertIn("not running its newest kernel", text)
        self.assertIn("CBVULN|removed|0", text)

    def test_any_other_dnf_failure_is_a_note_with_the_exit_code_not_a_failure(self):
        text = self.run_mode("5.14.0-4.el9.x86_64", self.FOUR, rc=2, message="Error: boom")
        self.assertIn("dnf could not remove the old kernels (exit code 2)", text)
        self.assertIn("CBVULN|removed|0", text)
        self.assertIn("CBVULN|exit|0", text)
        self.assertNotIn("CBVULN|failure|", text)

    def test_amazon_linux_names_are_counted_as_kernels(self):
        kernels = ["kernel6.12 6.12.1-1.amzn2023.x86_64", "kernel6.12 6.12.2-1.amzn2023.x86_64",
                   "kernel6.18 6.18.1-1.amzn2023.x86_64", "kernel6.18 6.18.62-1.amzn2023.x86_64"]
        text = self.run_mode("6.18.62-1.amzn2023.x86_64", kernels)
        self.assertEqual(self.read("calls"), ["DNF", "-y", "remove", "--oldinstallonly"])
        self.assertIn("CBVULN|removed|3", text)


class WorkerEndToEndTests(Base):
    """The real worker (mode_worker, which runs run_update and writes the status file), read back through the same
    mode_poll the engine calls, with stub package managers whose upgrade changes the stubbed package list."""

    COMMON = r"""
T={r}
RUN=$T/run; mkdir -m 700 "$RUN"
id() { echo 0; }
stat() { echo '0 700'; }
uname() { echo 6.8.0-1035-aws; }
"""
    UBUNTU = r"""
detect() { DISTRO=ubuntu; OS_NAME=Ubuntu; return 0; }
printf 'libc6 1\nbash 1\nzlib 1\n' > "$T/pkgs"
dpkg-query() {
    case "$*" in
        *Status-Abbrev*) sed 's/ .*//; s/^/ii linux-image-/' "$T/pkgs" ;;
        *) cat "$T/pkgs" ;;
    esac
}
apt-get() {
    case "$*" in
        *dist-upgrade*)
            printf '%b
' "$UPGRADE_OUTPUT"
            [ "${APT_RC:-0}" = 0 ] || return "$APT_RC"
            printf 'libc6 2\nbash 2\nzlib 1\nnewpkg 1\n' > "$T/pkgs" ;;
    esac
    return 0
}
"""
    DNF = r"""
detect() { DISTRO=rhel; OS_NAME=RHEL; return 0; }
printf 'bash 0:1-1.x86_64\nlibc 0:1-1.x86_64\nkernel-core 0:5.14.0-1.x86_64\n' > "$T/pkgs"
rpm() {
    case "$*" in
        *EPOCHNUM*) cat "$T/pkgs" ;;
        *) awk '{sub(/^0:/, "", $2); print $1, $2}' "$T/pkgs" ;;
    esac
}
dnf() {
    case "$*" in
        *needs-restarting*) return 1 ;;
        *upgrade*)
            printf '%b\n' "$DNF_OUTPUT"
            [ "${DNF_RC:-0}" = 0 ] || return "$DNF_RC"
            printf 'bash 0:2-1.x86_64\nlibc 0:2-1.x86_64\nkernel-core 0:5.14.0-1.x86_64\nkernel-core 0:5.14.0-2.x86_64\n' > "$T/pkgs" ;;
    esac
    return 0
}
"""

    def run_worker(self, family, runs=1, env=""):
        stubs = self.UBUNTU if family == "ubuntu" else self.DNF
        calls = "".join("( mode_worker )\n" for _ in range(runs))
        snippet = (self.COMMON + env + stubs + calls + "mode_poll\n").replace("{r}", self.root)
        result = {"detail": []}
        for line in shell(snippet).splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[0] == "CBVULN":
                if parts[1] == "detail":
                    result["detail"].append(parts[2].strip())
                else:
                    result[parts[1]] = parts[2]
        return result

    def test_ubuntu_counts_the_packages_the_upgrade_changed(self):
        result = self.run_worker("ubuntu", env="UPGRADE_OUTPUT='Reading package lists'\n")
        self.assertEqual((result["state"], result["exit"], result["updated"]), ("done", "0", "3"))

    def test_ubuntu_with_nothing_to_upgrade_counts_zero(self):
        stubs = self.UBUNTU.replace("*dist-upgrade*)", "*never-matches*)")
        snippet = (self.COMMON + stubs + "( mode_worker )\nmode_poll\n").replace("{r}", self.root)
        self.assertIn("CBVULN|updated|0", shell(snippet))

    def test_the_dnf_family_counts_by_the_package_list_not_the_install_time(self):
        result = self.run_worker("rhel", env="DNF_OUTPUT='Complete!'\n")
        self.assertEqual((result["state"], result["exit"], result["updated"]), ("done", "0", "3"))
        self.setUp()
        stubs = self.DNF.replace("*upgrade*)", "*never-matches*)")
        snippet = (self.COMMON + stubs + "( mode_worker )\nmode_poll\n").replace("{r}", self.root)
        self.assertIn("CBVULN|updated|0", shell(snippet))

    def test_a_second_run_of_the_worker_still_counts_against_the_first_before_list(self):
        # A restart of the worker's service after the upgrade ran must not turn the count into 0.
        for family, env in (("ubuntu", "UPGRADE_OUTPUT=x\n"), ("rhel", "DNF_OUTPUT=x\n")):
            self.setUp()
            result = self.run_worker(family, runs=2, env=env)
            self.assertEqual(result["updated"], "3", family)

    def test_a_failed_dnf_run_sends_detail_lines_from_the_first_error(self):
        log = r"noise 1\nnoise 2\nError: Transaction test error:\n  file /usr/a conflicts with b-1\n  file /usr/c conflicts with d-2\n\n" + "".join(
            "  line {}\\n".format(i) for i in range(1, 15))
        result = self.run_worker("rhel", env="DNF_RC=1\nDNF_OUTPUT='" + log + "'\n")
        self.assertEqual(result["exit"], "1")
        self.assertTrue(result["failure"].startswith("Error: Transaction test error:"))
        self.assertEqual(len(result["detail"]), 12)
        self.assertEqual(result["detail"][-1], "line 14")
        self.assertNotIn("noise 1", result["detail"])

    def test_the_detail_starts_at_the_first_error_line_when_it_is_near_the_end(self):
        log = r"a\nb\nError: Transaction test error:\n  file /usr/a conflicts with b-1\n  file /usr/c conflicts with d-2\n"
        result = self.run_worker("rhel", env="DNF_RC=1\nDNF_OUTPUT='" + log + "'\n")
        self.assertEqual(result["detail"], ["Error: Transaction test error:", "file /usr/a conflicts with b-1",
                                            "file /usr/c conflicts with d-2"])

    def test_without_an_error_line_the_last_twelve_lines_are_sent_and_capped_at_300_characters(self):
        long_line = "x" * 400
        log = "".join("l{}\\n".format(i) for i in range(1, 21)) + long_line + "\\n"
        result = self.run_worker("ubuntu", env="APT_RC=100\nUPGRADE_OUTPUT='" + log + "'\n")
        self.assertEqual(result["exit"], "100")
        self.assertEqual(len(result["detail"]), 12)
        self.assertEqual(result["detail"][-1], "x" * 300)
        self.assertEqual(result["detail"][0], "l10")

    def test_a_successful_run_sends_no_detail(self):
        result = self.run_worker("rhel", env="DNF_OUTPUT='Complete!'\n")
        self.assertEqual(result["detail"], [])


class NeedrestartAndSecondStartTests(Base):
    """The needrestart exclusion around apt, and the guards for a worker started a second time. The stubs are the
    ones of WorkerEndToEndTests; apt-get is wrapped to note whether the exclusion file exists at each call."""

    COMMON = r"""
T={r}
RUN=$T/run
NR_CONF_DIR=$T/conf.d
id() { echo 0; }
stat() { echo '0 700'; }
uname() { echo 6.8.0-1035-aws; }
"""
    WRAP = r"""
eval "real_$(declare -f apt-get)"
apt-get() {
    if [ -f "$NR_CONF_DIR/zz-cloudbolt-updates.conf" ]; then
        echo "present $*" >> "$T/seen"
        cat "$NR_CONF_DIR/zz-cloudbolt-updates.conf" >> "$T/content"
    else
        echo "absent $*" >> "$T/seen"
    fi
    echo "apt $*" >> "$T/aptcalls"
    real_apt-get "$@"
}
"""

    def go(self, body, family="ubuntu", env="", conf_dir=True, needrestart=True, wrap=True):
        stubs = WorkerEndToEndTests.UBUNTU if family == "ubuntu" else WorkerEndToEndTests.DNF
        snippet = self.COMMON + ("mkdir \"$NR_CONF_DIR\"\n" if conf_dir else "") + env
        snippet += ("needrestart() { :; }\n" if needrestart else "") + stubs + (self.WRAP if wrap else "") + body
        text = shell(snippet.replace("{r}", self.root))
        return text

    def slurp(self, *parts):
        with open(os.path.join(self.tmp.name, *parts), encoding="ascii") as handle:
            return handle.read()

    def seen(self):
        return self.slurp("seen").splitlines() if os.path.exists(os.path.join(self.tmp.name, "seen")) else []

    def conf_files(self):
        folder = os.path.join(self.tmp.name, "conf.d")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else None

    FRESH = "mkdir -m 700 \"$RUN\"\n( mode_worker )\nmode_poll\n"

    def test_the_exclusion_exists_for_every_apt_call_and_is_gone_after_success(self):
        text = self.go(self.FRESH, env="UPGRADE_OUTPUT=x\n")
        self.assertIn("CBVULN|state|done", text)
        seen = self.seen()
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(line.startswith("present ") for line in seen), seen)
        self.assertIn("dist-upgrade", seen[1])
        content = self.slurp("content")
        self.assertIn("push(@{ $nrconf{blacklist_rc} }, qr(^cbvuln-patch-));", content)
        self.assertEqual(self.conf_files(), [])

    def test_the_exclusion_is_gone_after_an_apt_failure(self):
        text = self.go(self.FRESH, env="UPGRADE_OUTPUT=x\nAPT_RC=100\n")
        self.assertIn("CBVULN|exit|100", text)
        self.assertTrue(all(line.startswith("present ") for line in self.seen()))
        self.assertEqual(self.conf_files(), [])

    def test_a_stale_file_from_a_killed_run_is_replaced_and_removed(self):
        env = ("UPGRADE_OUTPUT=x\nprintf 'stale from a killed run\\n' > \"$NR_CONF_DIR/zz-cloudbolt-updates.conf\"\n")
        self.go(self.FRESH, env=env)
        content = self.slurp("content")
        self.assertNotIn("stale", content)
        self.assertIn("cbvuln-patch-", content)
        self.assertEqual(self.conf_files(), [])

    def test_the_cleanup_mode_removes_a_stale_file_when_no_update_is_running(self):
        env = "printf 'stale\\n' > \"$NR_CONF_DIR/zz-cloudbolt-updates.conf\"\n"
        self.go("mode_cleanup\n", env=env, wrap=False)
        self.assertEqual(self.conf_files(), [])

    def test_nothing_is_written_when_needrestart_is_absent_or_has_no_conf_d(self):
        self.go(self.FRESH, env="UPGRADE_OUTPUT=x\n", needrestart=False)
        self.assertTrue(all(line.startswith("absent ") for line in self.seen()))
        self.assertEqual(self.conf_files(), [])
        self.setUp()
        self.go(self.FRESH, env="UPGRADE_OUTPUT=x\n", conf_dir=False)
        self.assertTrue(all(line.startswith("absent ") for line in self.seen()))
        self.assertIsNone(self.conf_files())

    def test_nothing_is_written_on_the_dnf_family(self):
        text = self.go(self.FRESH, family="rhel", env="DNF_OUTPUT=x\n", wrap=False)
        self.assertIn("CBVULN|updated|3", text)
        self.assertEqual(self.conf_files(), [])

    def test_a_second_worker_start_leaves_a_finished_run_alone(self):
        env = "UPGRADE_OUTPUT=x\n"
        self.go(self.FRESH, env=env)
        before = len(self.slurp("aptcalls").splitlines())
        status_before = self.slurp("run", "status")
        # Run the worker again over the same folder, with the packages already upgraded.
        text = self.go2("( mode_worker )\nmode_poll\n")
        self.assertEqual(len(self.slurp("aptcalls").splitlines()), before)
        self.assertEqual(self.slurp("run", "status"), status_before)
        self.assertIn("CBVULN|updated|3", text)

    def go2(self, body, pre=""):
        """The worker's stubs over the run folder an earlier call left (the package list is not reset)."""
        snippet = self.COMMON + "needrestart() { :; }\n" + WorkerEndToEndTests.UBUNTU.replace(
            "printf 'libc6 1\\nbash 1\\nzlib 1\\n' > \"$T/pkgs\"\n", "") + self.WRAP + pre + body
        return shell(snippet.replace("{r}", self.root))

    def test_an_interrupted_run_is_reported_as_a_failure_without_a_second_upgrade(self):
        os.makedirs(os.path.join(self.tmp.name, "run"))
        self.write("run/status", ["CBVULN|state|running", "CBVULN|phase|Installing updates"])
        self.write("run/before.pkgs", ["bash 1", "libc6 1", "zlib 1"])
        self.write("pkgs", ["bash 2", "libc6 2", "zlib 1"])
        text = self.go2("( mode_worker )\nmode_poll\n")
        self.assertIn("CBVULN|state|done", text)
        self.assertIn("CBVULN|exit|1", text)
        self.assertIn("CBVULN|failure|the update was interrupted", text)
        self.assertIn("CBVULN|updated|2", text)
        self.assertEqual(self.read("aptcalls"), [])

    def test_a_second_worker_while_the_first_is_alive_changes_nothing(self):
        os.makedirs(os.path.join(self.tmp.name, "run"))
        self.write("run/status", ["CBVULN|state|running"])
        self.write("pkgs", ["bash 1"])
        text = self.go2("( mode_worker )\ncat \"$RUN/status\"\n", pre="pid_alive() { return 0; }\n")
        self.assertEqual(text.split(), ["CBVULN|state|running"])
        self.assertEqual(self.read("aptcalls"), [])


if __name__ == "__main__":
    unittest.main()
