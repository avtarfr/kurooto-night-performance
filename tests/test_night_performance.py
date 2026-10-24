"""Night performance: it finds how to switch on each system (stand-ins only: PATH holds nothing but fakes, /sys is a
pretend tree, so this computer's power mode is never touched), switches while the night's work runs and puts back
exactly what was there, also after a crash; it doesn't switch on battery or while you're at the computer; your own
commands run without a shell, only after Allow, sudo only with -n; a failure is one morning line."""

from __future__ import annotations

import datetime as dt
import shutil
import sys
import time

import pytest

PPD = """#!/bin/sh
echo "$@" >> @LOG@
S=@DIR@/ppd.state
case "$1" in
  list) echo "  performance:"; echo "    CpuDriver: amd_pstate"; echo ""; echo "* balanced:"; echo ""
        echo "  power-saver:" ;;
  get) read -r v < "$S"; echo "$v" ;;
  set) echo "$2" > "$S" ;;
esac
exit 0
"""
TUNED = """#!/bin/sh
echo "$@" >> @LOG@
S=@DIR@/tuned.state
case "$1" in
  list) echo "Available profiles:"; echo "- balanced                    - General"
        echo "- powersave                   - Saves power"; echo "- throughput-performance      - Fast" ;;
  active) read -r v < "$S"; echo "Current active profile: $v" ;;
  profile) echo "$2" > "$S" ;;
esac
exit 0
"""
ASUS = """#!/bin/sh
echo "$@" >> @LOG@
S=@DIR@/asus.state
case "$2" in
  -p) read -r v < "$S"; echo "Active profile is $v" ;;
  -P) echo "$3" > "$S" ;;
esac
exit 0
"""
POWERCFG = """#!/bin/sh
echo "$@" >> @LOG@
S=@DIR@/plan.state
case "$1" in
  /list) echo "Existing Power Schemes (* Active)"; echo "-----------------------------------"
         echo "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced) *"
         [ -f @DIR@/no-high ] || echo "Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-cf3a6a3d1d6c  (High performance)" ;;
  /getactivescheme) read -r v < "$S"; echo "Power Scheme GUID: $v  (Something)" ;;
  /setactive|/overlaysetactive) echo "$2" > "$S" ;;
esac
exit 0
"""
PMSET = '#!/bin/sh\necho "$@" >> @LOG@\necho "System-wide power settings:"\necho " lowpowermode         1"\nexit 0\n'
SUDO = """#!/bin/sh
printf '%s\\n' "$*" >> @LOG@
[ "$1" = "-n" ] || { echo "sudo: a terminal is required to read the password" >&2; exit 1; }
shift
PATH=@DIR@:/usr/bin:/bin exec "$@"  # like sudo's own secure_path
"""
NOPE = '#!/bin/sh\necho "$@" >> @LOG@\nexit 0\n'


def fakes(d, progs: dict[str, str]):
    d.mkdir(exist_ok=True)
    for name, body in progs.items():
        f = d / name
        f.write_text(body.replace("@LOG@", str(d / f"{name}.log")).replace("@DIR@", str(d)), encoding="utf-8")
        f.chmod(0o755)


def log(d, name: str) -> str:
    f = d / f"{name}.log"
    return f.read_text() if f.exists() else ""


def wait_for(fn, s=5.0):
    end = time.time() + s
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


class Night:
    def __init__(self, app, m, mod, bin_, root):
        self.app, self.m, self.mod, self.bin, self.root = app, m, mod, bin_, root
        self.busy = False
        self.away = True
        self.ac = True

    def state(self, name: str) -> str:
        return (self.bin / f"{name}.state").read_text().strip()


@pytest.fixture
def night(env, load_addon, tmp_path, monkeypatch):
    load_addon("night-performance", env.app)
    a = env.app
    m = a.modules["night_performance"]
    mod = sys.modules[type(m).__module__]
    root = tmp_path / "root"
    (root / "sys").mkdir(parents=True)
    monkeypatch.setattr(mod, "SYS", root / "sys")  # a pretend /sys: never this computer's
    monkeypatch.setattr(mod, "HELPER", root / "usr-local-bin" / "kurooto-legion-night")
    monkeypatch.setenv("KUROOTO_LEGION_TEST_ROOT", str(root))
    bin_ = tmp_path / "fakebin"
    fakes(bin_, {})
    monkeypatch.setenv("PATH", str(bin_))  # nothing but stand-ins: this computer's own power tools are out of reach
    monkeypatch.setattr(mod, "platform", lambda: "linux")
    n = Night(a, m, mod, bin_, root)
    monkeypatch.setattr(m, "present", lambda: not n.away)
    monkeypatch.setattr(m, "busy", lambda: n.busy)
    now = dt.datetime.now()
    monkeypatch.setattr(m, "window", lambda: (now - dt.timedelta(hours=1), now + dt.timedelta(hours=5)))
    monkeypatch.setattr(a.power, "on_ac", lambda: n.ac)
    monkeypatch.setattr(a.power, "current", "normal")
    a.settings.set("night_performance.settle_min", 0)
    env.on("night_performance")
    time.sleep(0.2)  # its start's look (nothing to do: no work)
    yield n
    if a.is_on("night_performance"):
        a.disable("night_performance")


def with_ppd(n, mode="power-saver"):
    fakes(n.bin, {"powerprofilesctl": PPD})
    (n.bin / "ppd.state").write_text(mode + "\n")


# ------------------------------------------------------------------------------------------------ Linux
def test_power_profiles_daemon_switches_and_puts_back_exactly(night, env):
    with_ppd(night)
    assert "power-profiles-daemon (now: power-saver)" in env.say("/perf")
    ok, said = night.m.switch()
    assert ok and night.state("ppd") == "performance"
    assert night.m.restore()[0] and night.state("ppd") == "power-saver"  # what it was, not "balanced"


def test_the_night_switches_while_the_work_runs_and_not_after(night):
    with_ppd(night, "balanced")
    night.m.tick()
    assert night.state("ppd") == "balanced"  # no work: nothing
    night.busy = True
    night.m.tick()
    assert night.state("ppd") == "performance"
    night.busy = False
    night.m.tick()
    assert night.state("ppd") == "balanced"


def test_a_mode_you_changed_meanwhile_is_left_alone(night):
    with_ppd(night, "balanced")
    night.busy = True
    night.m.tick()
    (night.bin / "ppd.state").write_text("power-saver\n")  # you, at 3 am
    night.busy = False
    night.m.tick()
    assert night.state("ppd") == "power-saver" and not night.m._rec()["switched"]


def test_on_battery_or_at_the_computer_it_doesnt_switch_and_puts_back(night):
    with_ppd(night, "balanced")
    night.busy, night.ac = True, False
    night.m.tick()
    assert night.state("ppd") == "balanced"
    night.ac, night.away = True, False  # on the charger, but you're using it
    night.m.tick()
    assert night.state("ppd") == "balanced"
    night.away = True
    night.m.tick()
    assert night.state("ppd") == "performance"
    night.ac = False  # unplugged while switched
    night.m.tick()
    assert night.state("ppd") == "balanced"


def test_turned_off_in_settings_it_never_switches(night):
    with_ppd(night, "balanced")
    night.app.settings.set("night_performance.on", False)
    night.busy = True
    night.m.tick()
    assert night.state("ppd") == "balanced"


def test_a_crash_is_put_back_at_the_next_start_and_stopping_puts_back(night):
    with_ppd(night, "balanced")
    night.busy = True
    night.m.tick()
    assert night.state("ppd") == "performance"
    night.busy = False  # ... Kurooto died here; at the next start:
    night.m.start()
    assert wait_for(lambda: night.state("ppd") == "balanced")
    night.busy = True
    night.m.tick()
    assert night.state("ppd") == "performance"
    night.app.disable("night_performance")  # stopping (or turning it off) puts it back
    assert night.state("ppd") == "balanced"


def test_tuned_and_asusctl(night):
    fakes(night.bin, {"tuned-adm": TUNED})
    (night.bin / "tuned.state").write_text("balanced\n")
    assert night.m.switch()[0] and night.state("tuned") == "throughput-performance"
    assert night.m.restore()[0] and night.state("tuned") == "balanced"
    (night.bin / "tuned-adm").unlink()
    fakes(night.bin, {"asusctl": ASUS})
    (night.bin / "asus.state").write_text("Quiet\n")
    assert night.m.switch()[0] and night.state("asus") == "Performance"
    assert night.m.restore()[0] and night.state("asus") == "Quiet"


def test_nothing_found_says_what_to_do(night, env):
    (night.root / "sys" / "firmware" / "acpi").mkdir(parents=True)
    (night.root / "sys" / "firmware" / "acpi" / "platform_profile").write_text("balanced\n")
    (night.root / "sys" / "firmware" / "acpi" / "platform_profile_choices").write_text(
        "low-power balanced performance\n"
    )
    text = env.say("/perf")
    assert "platform profile is balanced" in text and "only root" in text
    night.busy = True
    night.m.tick()
    assert "Night performance couldn't switch" in night.m.dreaming_lines()[0]


# ---------------------------------------------------------------------------------------- Legion and LOQ
def legion_tree(n, profile="balanced", fan="0"):
    d = n.root / "sys" / "bus" / "platform" / "drivers" / "legion" / "PNP0C09:00"
    d.mkdir(parents=True, exist_ok=True)
    (d / "fan_fullspeed").write_text(fan + "\n")
    acpi = n.root / "sys" / "firmware" / "acpi"
    acpi.mkdir(parents=True, exist_ok=True)
    (acpi / "platform_profile").write_text(profile + "\n")
    (acpi / "platform_profile_choices").write_text("low-power balanced performance custom\n")
    (n.root / "run").mkdir(exist_ok=True)
    return acpi / "platform_profile", d / "fan_fullspeed"


def install_helper(n):
    n.mod.HELPER.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(n.mod.HERE / "legion" / "kurooto-legion-night", n.mod.HELPER)
    n.mod.HELPER.chmod(0o755)
    fakes(n.bin, {"sudo": SUDO})


def test_legion_without_the_helper_says_how_to_install_it(night, env):
    legion_tree(night)
    text = env.say("/perf")
    assert "Lenovo Legion or LOQ" in text and "sudo bash " in text and "legion/install.sh" in text
    assert night.m.detect()[0] is None
    with_ppd(night, "balanced")  # power-profiles-daemon still does the profile meanwhile
    assert night.m.detect()[0].name == "power-profiles-daemon"


def test_legion_helper_custom_profile_and_max_fan_then_exactly_back(night, env):
    prof, fan = legion_tree(night, "balanced", "0")
    install_helper(night)
    with_ppd(night, "balanced")  # the helper goes first on a Legion
    assert "the Legion helper (custom profile, fans at full speed)" in env.say("/perf")
    night.busy = True
    night.m.tick()
    assert prof.read_text().strip() == "custom" and fan.read_text().strip() == "1"
    assert "-n" == log(night.bin, "sudo").split()[0] and "on custom fan" in log(night.bin, "sudo")
    night.busy = False
    night.m.tick()
    assert prof.read_text().strip() == "balanced" and fan.read_text().strip() == "0"
    assert not (night.root / "run" / "kurooto-legion-night.state").exists()
    assert night.state("ppd") == "balanced"  # restore was the helper's off, nothing else


def test_legion_performance_profile_without_the_fan(night):
    prof, fan = legion_tree(night, "low-power", "0")
    install_helper(night)
    night.app.settings.set("night_performance.legion_profile", "performance")
    night.app.settings.set("night_performance.legion_max_fan", False)
    assert night.m.switch()[0]
    assert prof.read_text().strip() == "performance" and fan.read_text().strip() == "0"
    assert night.m.restore()[0] and prof.read_text().strip() == "low-power"


def test_the_test_button_switches_and_puts_back(night, env):
    legion_tree(night, "balanced", "0")
    install_helper(night)
    said = env.press("nperf:test:go")
    assert said.startswith("Works:") and "night mode on" in said
    assert (night.root / "sys" / "firmware" / "acpi" / "platform_profile").read_text().strip() == "balanced"


# --------------------------------------------------------------------------------------- your own commands
def test_your_commands_run_without_a_shell_only_after_allow(night, env):
    fakes(night.bin, {"sudo": SUDO, "system76-power": NOPE})
    s = night.app.settings
    s.set("night_performance.on_command", "sudo system76-power profile performance")  # gets -n
    s.set("night_performance.off_command", "system76-power profile balanced")
    night.busy = True
    night.m.tick()
    assert log(night.bin, "system76-power") == ""  # not before your tap
    assert "wait for your OK" in night.m.dreaming_lines()[0]
    text = env.say("/perf")
    assert "sudo -n system76-power profile performance" in text and "tap Allow" in text
    said = env.press(f"nperf:allow:{night.m.digest()}")
    assert said.startswith("Works")
    assert log(night.bin, "sudo").splitlines()[0] == "-n system76-power profile performance"
    assert log(night.bin, "system76-power").splitlines() == ["profile performance", "profile balanced"]
    s.set("night_performance.off_command", "system76-power profile quiet")  # changed: asks again
    assert not night.m.approved()


def test_shell_tricks_and_programs_off_the_card_are_refused(night, tmp_path):
    marker = tmp_path / "pwned"
    for line in (
        f"powerprofilesctl set performance; touch {marker}",
        f"powerprofilesctl set performance && touch {marker}",
        f"powerprofilesctl set $(touch {marker})",
        f"powerprofilesctl set `touch {marker}`",
        f"touch {marker}",
        f"/bin/sh -c 'touch {marker}'",
        "sudo -S powerprofilesctl set performance",
    ):
        argv, why = night.m.parse(line)
        assert argv is None and why, line
    assert night.m.parse("sudo -n /usr/local/bin/helper on")[0] == ["sudo", "-n", "/usr/local/bin/helper", "on"]
    assert night.m.parse("sudo helper on")[0] == ["sudo", "-n", "helper", "on"]
    r = night.m.configure({"on_command": f"touch {marker}", "off_command": "x"})
    assert not r["ok"] and "card" in r["message"]
    assert not marker.exists()


def test_with_your_commands_putting_back_is_your_off_command_only(night):
    with_ppd(night, "balanced")
    r = night.m.configure(
        {"on_command": "powerprofilesctl set performance", "off_command": "powerprofilesctl set balanced"}
    )
    assert r["ok"]  # typed in Set up: your OK
    (night.bin / "ppd.state").write_text("power-saver\n")
    night.busy = True
    night.m.tick()
    assert night.state("ppd") == "performance"
    night.busy = False
    night.m.tick()
    assert night.state("ppd") == "balanced"  # what your off command says, not what Kurooto saw before
    assert "get" not in log(night.bin, "powerprofilesctl")


def test_a_failing_command_that_needs_root_says_so(night):
    fakes(night.bin, {"system76-power": '#!/bin/sh\necho "Permission denied" >&2\nexit 1\n'})
    night.m.configure({"on_command": "system76-power profile performance", "off_command": "system76-power profile b"})
    ok, why = night.m.switch()
    assert not ok and "needs root" in why and "password" in why
    assert not night.m._rec()["switched"]


# --------------------------------------------------------------------------------------- Windows and macOS
def test_windows_high_performance_plan_and_back_by_guid(night, monkeypatch):
    fakes(night.bin, {"powercfg": POWERCFG})
    (night.bin / "plan.state").write_text("381b4222-f694-41f0-9685-ff5bb260df2e\n")
    monkeypatch.setattr(night.mod, "platform", lambda: "win32")
    way, _ = night.m.detect()
    assert way.name == "windows-plan" and "High performance" in way.title
    assert night.m.switch()[0] and night.state("plan") == "8c5e7fda-e8bf-4a96-9a85-cf3a6a3d1d6c"
    assert night.m.restore()[0] and night.state("plan") == "381b4222-f694-41f0-9685-ff5bb260df2e"


def test_windows_without_the_plan_uses_the_best_performance_overlay(night, monkeypatch):
    fakes(night.bin, {"powercfg": POWERCFG})
    (night.bin / "no-high").write_text("")
    balanced_overlay = "00000000-0000-0000-0000-000000000000"
    (night.bin / "plan.state").write_text(balanced_overlay + "\n")
    monkeypatch.setattr(night.mod, "platform", lambda: "win32")
    monkeypatch.setattr(night.m, "win_overlay", lambda: night.state("plan"))
    assert night.m.switch()[0] and night.state("plan") == night.mod.WIN_BEST
    assert night.m.restore()[0] and night.state("plan") == balanced_overlay


def test_macos_says_it_needs_root(night, env, monkeypatch):
    fakes(night.bin, {"pmset": PMSET})
    monkeypatch.setattr(night.mod, "platform", lambda: "darwin")
    way, hints = night.m.detect()
    assert way is None and "only as root" in hints[0] and "Low Power Mode is on now" in hints[0]


# ----------------------------------------------------------------------------------------------- the card
def test_the_morning_message_has_one_line_only_when_switching_failed(night, env):
    env.on("dreaming")
    d = night.app.modules["dreaming"]
    night.app.events.emit("integrate_batch", book="x/y", batch="1-12", new=1, links=1)  # something to tell
    assert "Night performance" not in d.report(send=False)
    night.busy = True
    night.m.tick()  # nothing to switch with here
    assert "Night performance couldn't switch" in d.report(send=False)


def test_it_may_start_its_card_programs_and_nothing_else(night, monkeypatch):
    with_ppd(night)
    assert night.m.run(["powerprofilesctl", "get"]).ok
    real = night.mod.proc.run
    monkeypatch.setattr(night.mod.proc, "run", lambda args, **kw: real(["/bin/sh", "-c", "echo hi"], **kw))
    r = night.m.run(["powerprofilesctl", "get"])  # a shell instead: core/fence.py stops it
    assert not r.ok and "only start the programs on its card" in r.err


def test_the_legion_helper_takes_only_its_own_lines(night):
    import subprocess

    legion_tree(night)
    helper = night.mod.HERE / "legion" / "kurooto-legion-night"
    for args in (["on"], ["on", "custom"], ["on", "custom", "fan", "x"], ["on", "low-power", "fan"], ["rm", "-rf"]):
        r = subprocess.run(["/bin/bash", str(helper), *args], capture_output=True, text=True, timeout=20)
        assert r.returncode == 2 and "usage" in r.stderr, args
    r = subprocess.run(["/bin/bash", str(helper), "status"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout.strip() == "profile balanced, max fan 0"
