"""Night performance: while Kurooto does its heavy night work (the concept graph reading books and class files, the
nightly passes), this computer runs in its highest performance power mode, which on most laptops also runs the fans
harder; when the work ends, or the night's reading window does, it goes back to exactly the mode it was in.

    when      inside tonight's reading window (core/readbudget.py: from the night's start until you wake), while
              Kurooto's automatic AI work runs (the "bg" and "chk" lanes of core/jobs.py), on the charger, and only
              when you're away from the computer (no input for night_performance.away_min, or the screen locked).
              Back when none of that holds any more (after night_performance.settle_min without work, so a pause
              between two batches doesn't flip it).
    how       found by itself, without root:
                Linux    a Lenovo Legion or LOQ with the helper in legion/ installed (custom profile, fans at full
                         speed); power-profiles-daemon (powerprofilesctl); tuned (tuned-adm); asusctl (ASUS laptops)
                Windows  the Ultimate Performance or High performance power plan (powercfg), else the "Best
                         performance" power mode overlay
                macOS    nothing without root: your own commands
              or your own commands (night_performance.on_command / off_command), which go first when set.
    restore   a mode it found is remembered (in its state file) before it's changed, and put back exactly, unless you
              changed it yourself meanwhile; with commands (yours, or the Legion helper), the off command puts it back.
              Kurooto stopping puts it back; after a crash, the next start does (unless the night's work is still on).
    /perf     what it found, the mode now, and a Test button (switch and put back)

It only starts the programs on its card. Your own commands run without a shell, may start only those programs, and
run only after you've tapped Allow for exactly those lines; sudo only ever runs with -n (never asks for a password).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shlex
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from kurooto.core import events, proc
from kurooto.core.config import register_defaults
from kurooto.core.state import jload, jsave
from kurooto.modules.base import Answer, Command, Incoming, Module

register_defaults(
    "night_performance",
    {
        "on": True,  # switch at night; False keeps the feature (and /perf) but never switches
        "on_command": "",  # your own command that switches to the performance mode (one program, no shell)
        "off_command": "",  # your own command that puts the mode back as it was (it has to remember it itself)
        "check_command": "",  # optional: your own command that prints the mode now (shown by /perf)
        "away_min": 10,  # switch only after this many minutes without keyboard or mouse input (or a locked screen)
        "settle_min": 10,  # minutes without night work before it switches back
        "legion_profile": "custom",  # Lenovo Legion and LOQ with the helper: "custom" or "performance"
        "legion_max_fan": True,  # Lenovo Legion and LOQ with the helper: the fans at full speed too
    },
)

SYS = Path("/sys")  # tests point these at a pretend tree
HELPER = Path("/usr/local/bin/kurooto-legion-night")
HERE = Path(__file__).resolve().parent.parent  # the add-on's folder (legion/ is in it)
PROGRAMS = ("powerprofilesctl", "tuned-adm", "asusctl", "system76-power", "sudo", "powercfg", "pmset")  # its card
LANES = ("bg", "chk")  # core/jobs.py: automatic work (the concept graph's batches and second looks, ...)
TELL_S = 20 * 3600  # a failure is a morning line for this long
RETRY_S = 30 * 60  # after a failed switch, the next try waits this long
TUNED = ("throughput-performance", "latency-performance", "accelerator-performance")
WIN_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
WIN_HIGH = "8c5e7fda-e8bf-4a96-9a85-cf3a6a3d1d6c"
WIN_BEST = "ded574b5-45a0-4f42-8737-46345c09c238"  # the "Best performance" power mode overlay (Windows 10 1709+)
GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
SHELLISH = re.compile(r"[;|&<>`]|\$\(")  # a shell's, in a command that runs without one
ROOT_WORDS = ("password is required", "permission denied", "not permitted", "access denied", "authoriz", "root")
README = "the add-on's README"


def platform() -> str:
    """This computer's system (sys.platform; tests stand in another)."""
    return sys.platform


class Way:
    """One way to switch. A "mode" way reads and sets a named mode (Kurooto remembers the one before); a "commands"
    way runs an on and an off command (the off command puts back what was there)."""

    def __init__(
        self,
        name: str,
        title: str,
        *,
        target: str = "",
        get: Callable[[], str | None] | None = None,
        put: Callable[[str], tuple[bool, str]] | None = None,
        on: list[str] | None = None,
        off: list[str] | None = None,
        check: list[str] | None = None,
        user: bool = False,
    ):
        self.name, self.title, self.target = name, title, target
        self.get, self.put = get, put
        self.on, self.off, self.check = on, off, check
        self.user = user  # commands you entered (they run only after Allow)

    @property
    def kind(self) -> str:
        return "commands" if self.on is not None else "mode"


def first_line(text: str) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")


class NightPerformance(Module):
    id = "night_performance"
    title = "Night performance"
    description = "Switches this computer to its highest performance mode during Kurooto's heavy night work."
    privacy = "Changes this computer's power mode at night and puts it back; nothing leaves the computer."

    def __init__(self, app):
        super().__init__(app)
        self.state_file = Path(app.paths.state) / "night-performance.json"
        self._lock = threading.RLock()
        self._off = None
        self._probe = None
        self._busy_at = 0.0
        self._starting = False

    # ------------------------------------------------------------------------------------------- running things
    def programs(self) -> tuple[str, ...]:
        """The programs it may start: its card's (core/fence.py holds it to them)."""
        card = getattr(self, "linked_card", None) or {}
        names = tuple(str(p.get("name")).lower() for p in card.get("programs") or [] if p.get("name"))
        return names or PROGRAMS

    def run(self, argv: list[str], timeout: float = 30) -> proc.Result:
        exe = argv[0] if ("/" in argv[0] or "\\" in argv[0]) else proc.which(argv[0])
        if not exe or not Path(exe).exists():
            return proc.Result(127, "", f"{argv[0]} isn't installed")
        try:
            return proc.run([exe, *argv[1:]], timeout=timeout)
        except PermissionError as e:  # core/fence.py: not on its card
            return proc.Result(126, "", str(e))
        except OSError as e:
            return proc.Result(126, "", f"{argv[0]} didn't start: {e}")

    def why(self, r: proc.Result, argv: list[str]) -> str:
        text = first_line(r.err) or first_line(r.out) or f"it ended with code {r.code}"
        if r.code is None:
            return f"{' '.join(argv[:3])} didn't finish in time"
        low = text.lower()
        if any(w in low for w in ROOT_WORDS):
            return (
                f"{text[:150]} (this needs root: Kurooto never asks for a password, so let this one command run "
                f"without one, as {README} shows, or use a tool that doesn't need root)"
            )
        return text[:200]

    # ------------------------------------------------------------------------------------------ your commands
    def parse(self, line: str) -> tuple[list[str] | None, str]:
        """A command you entered, as the program and its arguments (no shell ever runs it), or (None, why not)."""
        line = str(line or "").strip()
        if not line:
            return None, "it's empty"
        try:
            argv = shlex.split(line, posix=platform() != "win32")
        except ValueError as e:
            return None, f"Kurooto can't read it ({e})"
        if not argv:
            return None, "it's empty"
        if any(SHELLISH.search(a) for a in argv):
            return (
                None,
                "it runs one program without a shell, so ;, |, &&, > and $(...) don't work: put them in a script",
            )
        prog = Path(argv[0]).name.lower().removesuffix(".exe")
        if prog not in self.programs():
            return None, (
                f"{prog} isn't on this add-on's card, so it may not start it. It may start "
                f"{', '.join(self.programs())}; for anything else, run it through sudo -n (a sudo rule for exactly "
                f"that command, as {README} shows)"
            )
        if prog == "sudo":
            opts = []
            for a in argv[1:]:
                if not a.startswith("-"):
                    break
                opts.append(a)
            if any(o in ("-S", "--stdin", "-A", "--askpass") for o in opts):
                return None, "sudo here never takes a password: drop -S / -A"
            if len(argv) == 1 + len(opts):
                return None, "sudo needs the command it runs"
            if "-n" not in opts and "--non-interactive" not in opts:
                argv = [argv[0], "-n", *argv[1:]]  # never waits for a password
        return argv, ""

    def user_lines(self) -> list[str]:
        return [str(self.setting(k) or "").strip() for k in ("on_command", "off_command", "check_command")]

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.user_lines()).encode()).hexdigest()[:12]

    def approved(self) -> bool:
        return self._rec().get("allowed") == self.digest()

    def allow(self) -> None:
        self._save(allowed=self.digest(), failed=None)

    def user_way(self) -> tuple[Way | None, str]:
        on_l, off_l, check_l = self.user_lines()
        if not on_l and not off_l:
            return None, ""
        if not on_l or not off_l:
            return None, "set both night_performance.on_command and off_command (the off one puts the mode back)"
        on, why = self.parse(on_l)
        if on is None:
            return None, f"on_command: {why}"
        off, why = self.parse(off_l)
        if off is None:
            return None, f"off_command: {why}"
        check = None
        if check_l:
            check, why = self.parse(check_l)
            if check is None:
                return None, f"check_command: {why}"
        return Way("commands", "your own commands", on=on, off=off, check=check, user=True), ""

    # ----------------------------------------------------------------------------------------------- finding
    def detect(self) -> tuple[Way | None, list[str]]:
        """(how to switch on this computer, or None; notes for /perf). Your own commands go first."""
        way, why = self.user_way()
        if way is not None:
            return way, []
        hints = [f"Your commands can't run: {why}."] if why else []
        if platform() == "win32":
            way, more = self._windows()
        elif platform() == "darwin":
            way, more = self._mac()
        elif platform().startswith("linux"):
            way, more = self._linux()
        else:
            way, more = None, [f"Kurooto doesn't know how to switch modes on {platform()}: use your own commands."]
        return way, hints + more

    def way_named(self, name: str) -> Way | None:
        """A mode way by its name, to put back what it changed (without looking for a target)."""
        if name == "power-profiles-daemon":
            return self._ppd_way("performance")
        if name == "tuned":
            return self._tuned_way("")
        if name == "asusctl":
            return self._asus_way()
        if name == "windows-plan":
            return self._win_plan_way("", "")
        if name == "windows-overlay":
            return self._win_overlay_way()
        return None

    # Linux
    def legion(self) -> bool:
        return (SYS / "bus" / "platform" / "drivers" / "legion").is_dir()

    def legion_way(self) -> Way:
        prof = str(self.setting("legion_profile") or "custom").strip().lower()
        prof = prof if prof in ("custom", "performance") else "custom"
        fan = "fan" if self.setting("legion_max_fan") else "nofan"
        h = str(HELPER)
        return Way(
            "legion",
            f"the Legion helper ({prof} profile{', fans at full speed' if fan == 'fan' else ''})",
            target=prof,
            on=["sudo", "-n", h, "on", prof, fan],
            off=["sudo", "-n", h, "off"],
            check=["sudo", "-n", h, "status"],
        )

    def legion_install(self) -> str:
        return f"sudo bash {HERE / 'legion' / 'install.sh'}"

    def _linux(self) -> tuple[Way | None, list[str]]:
        hints: list[str] = []
        if self.legion():
            if HELPER.is_file():
                w = self.legion_way()
                r = self.run(w.check or [], timeout=15)
                if r.ok:
                    return w, []
                hints.append(
                    f"The Legion helper is installed, but `sudo -n {HELPER} status` didn't work: "
                    f"{self.why(r, w.check or [])}. Running the installer again sets its sudo rule up: "
                    f"{self.legion_install()}"
                )
            else:
                hints.append(
                    "This is a Lenovo Legion or LOQ (the legion_laptop driver). Its custom profile and full-speed "
                    "fans are files only root may change, so Kurooto uses a small helper you install once: "
                    f"{self.legion_install()} (it lets only that helper's on, off and status run without a password; "
                    f"{README}: Lenovo Legion and LOQ on Linux)."
                )
        for find in (self._ppd, self._tuned, self._asus):
            w, note = find()
            if note:
                hints.append(note)
            if w is not None:
                return w, hints
        prof = SYS / "firmware" / "acpi" / "platform_profile"
        if prof.is_file():
            try:
                cur = prof.read_text().strip()
                choices = (prof.parent / "platform_profile_choices").read_text().split()
            except OSError:
                cur, choices = "?", []
            hints.append(
                f"The kernel's platform profile is {cur} ({', '.join(choices) or 'no choices listed'}); only root may "
                f"change it, and no tool here does it for you. Enter your own commands ({README}: Your own "
                "commands), or install power-profiles-daemon."
            )
        for tool in ("tlp", "cpupower", "auto-cpufreq", "system76-power"):
            if proc.which(tool):
                hints.append(
                    f"{tool} is installed: it switches with root (or its own permission), so enter its commands "
                    f"yourself, with sudo -n and a sudo rule where it needs root ({README})."
                )
        if not hints:
            hints.append(
                "No power tool here switches modes without root (power-profiles-daemon, tuned, asusctl): enter your "
                f"own commands ({README})."
            )
        return None, hints

    def _ppd_way(self, target: str) -> Way:
        def get():
            r = self.run(["powerprofilesctl", "get"], 15)
            return first_line(r.out) or None if r.ok else None

        def put(v: str):
            r = self.run(["powerprofilesctl", "set", v], 15)
            return r.ok, "" if r.ok else self.why(r, ["powerprofilesctl", "set"])

        return Way("power-profiles-daemon", "power-profiles-daemon", target=target, get=get, put=put)

    def _ppd(self) -> tuple[Way | None, str]:
        if not proc.which("powerprofilesctl"):
            return None, ""
        r = self.run(["powerprofilesctl", "list"], 15)
        if not r.ok:
            return None, f"powerprofilesctl didn't answer: {self.why(r, ['powerprofilesctl'])}"
        names = re.findall(r"^\s*\*?\s*([a-z][a-z-]*):\s*$", r.out or "", re.M)
        if "performance" not in names:
            return None, "power-profiles-daemon offers no performance profile on this computer (its driver has none)"
        return self._ppd_way("performance"), ""

    def _tuned_way(self, target: str) -> Way:
        def get():
            r = self.run(["tuned-adm", "active"], 15)
            m = re.search(r"Current active profile:\s*(\S+)", r.out or "")
            return m[1] if r.ok and m else None

        def put(v: str):
            r = self.run(["tuned-adm", "profile", v], 60)
            return r.ok, "" if r.ok else self.why(r, ["tuned-adm", "profile"])

        return Way("tuned", "tuned", target=target, get=get, put=put)

    def _tuned(self) -> tuple[Way | None, str]:
        if not proc.which("tuned-adm"):
            return None, ""
        r = self.run(["tuned-adm", "list"], 15)
        have = set(re.findall(r"^-\s*(\S+)", r.out or "", re.M)) if r.ok else set()
        target = next((t for t in TUNED if t in have), "")
        if not target:
            return None, "tuned has no performance profile here (throughput-performance)"
        return self._tuned_way(target), ""

    def _asus_way(self) -> Way:
        def get():
            r = self.run(["asusctl", "profile", "-p"], 15)
            m = re.search(r"\b(Quiet|Balanced|Performance|LowPower)\b", r.out or "")
            return m[1] if r.ok and m else None

        def put(v: str):
            r = self.run(["asusctl", "profile", "-P", v], 15)
            return r.ok, "" if r.ok else self.why(r, ["asusctl", "profile"])

        return Way("asusctl", "asusctl (ASUS)", target="Performance", get=get, put=put)

    def _asus(self) -> tuple[Way | None, str]:
        if not proc.which("asusctl"):
            return None, ""
        w = self._asus_way()
        if w.get is None or w.get() is None:
            return None, "asusctl is installed but didn't say the profile (is asusd running?)"
        return w, ""

    # Windows
    def _win_plan_way(self, target: str, title: str) -> Way:
        def get():
            r = self.run(["powercfg", "/getactivescheme"], 15)
            m = GUID.search(r.out or "")
            return m[0].lower() if r.ok and m else None

        def put(v: str):
            r = self.run(["powercfg", "/setactive", v], 15)
            return r.ok, "" if r.ok else self.why(r, ["powercfg", "/setactive"])

        return Way("windows-plan", f"the {title or 'power'} plan (powercfg)", target=target, get=get, put=put)

    def win_overlay(self) -> str | None:
        """The power mode overlay now (Settings → Power: Best performance, ...), from the registry; None when Windows
        has none (or it can't be read)."""
        try:
            import winreg  # type: ignore[import-not-found]

            key = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                v, _ = winreg.QueryValueEx(k, "ActiveOverlayAcPowerScheme")
        except (ImportError, OSError):
            return None
        return str(v).lower() if GUID.fullmatch(str(v)) else None

    def _win_overlay_way(self) -> Way:
        def put(v: str):
            r = self.run(["powercfg", "/overlaysetactive", v], 15)
            return r.ok, "" if r.ok else self.why(r, ["powercfg", "/overlaysetactive"])

        return Way("windows-overlay", "the Best performance power mode", target=WIN_BEST, get=self.win_overlay, put=put)

    def _windows(self) -> tuple[Way | None, list[str]]:
        r = self.run(["powercfg", "/list"], 15)
        if not r.ok:
            return None, [f"powercfg didn't answer: {self.why(r, ['powercfg'])}"]
        plans = {g.lower(): n.strip() for g, n in re.findall(rf"({GUID.pattern})\s+\(([^)]*)\)", r.out or "")}
        pick = next((g for g, n in plans.items() if g == WIN_ULTIMATE or "ultimate" in n.lower()), "")
        pick = pick or next((g for g, n in plans.items() if g == WIN_HIGH or n.lower() == "high performance"), "")
        if pick:
            return self._win_plan_way(pick, plans[pick]), []
        if self.win_overlay() is not None:
            return self._win_overlay_way(), []
        return None, [
            "Windows here has no High or Ultimate Performance plan and no power mode setting Kurooto can read. "
            f"Enter your own commands ({README})."
        ]

    # macOS
    def _mac(self) -> tuple[Way | None, list[str]]:
        r = self.run(["pmset", "-g"], 15)
        m = re.search(r"lowpowermode\s+(\d)", r.out or "") if r.ok else None
        now = " Low Power Mode is on now." if m and m[1] == "1" else ""
        return None, [
            "macOS changes Low Power Mode (and High Power Mode on some MacBook Pros) only as root, and Kurooto never "
            f"asks for a password.{now} Enter your own commands with sudo -n and a sudo rule for exactly them "
            f"({README}: macOS)."
        ]

    # ----------------------------------------------------------------------------------------------- the night
    def window(self) -> tuple[dt.datetime, dt.datetime]:
        """Tonight's reading window (core/readbudget.py): the concept graph's own, or worked out the same way."""
        ig = self.app.modules.get("integrate")
        budget = getattr(ig, "budget", None)
        if budget is None:
            from kurooto.core import readbudget

            budget = readbudget.Budget(self.app, lambda: "")
        return budget.window()

    def present(self) -> bool:
        """Are you at this computer (input in the last away_min, screen not locked)? Unknown counts as away."""
        from kurooto.core.idle import IdleProbe

        if self._probe is None:
            self._probe = IdleProbe(self.app.paths.state)
        idle = self._probe.idle_seconds()
        if idle is None:
            return False
        try:
            if self._probe.locked():
                return False
        except Exception:
            pass
        return idle < self.minutes("away_min", 10) * 60

    def minutes(self, key: str, default: float) -> float:
        v = self.setting(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default

    def busy(self) -> bool:
        """Is Kurooto's automatic work going (a job of the bg or chk lane running, or queued and due)?"""
        t = time.time()
        for j in self.app.jobs.active():
            if (j.lane or "you") in LANES and (j.status == "running" or float(j.not_before or 0) <= t):
                return True
        return False

    def on_charger(self) -> bool:
        pw = self.app.power
        return pw.current == "normal" and bool(pw.on_ac())

    def wanted(self) -> tuple[bool, str]:
        if not self.setting("on"):
            return False, "night_performance.on is off"
        if not self.on_charger():
            return False, "on battery (or paused): the heavy work waits for the charger"
        start, end = self.window()
        if not start <= dt.datetime.now() < end:
            return False, "outside tonight's reading window"
        if self.present():
            return False, "you're using the computer"
        if self.busy():
            self._busy_at = time.time()
            return True, "the night's work is going"
        if self._busy_at and time.time() - self._busy_at < self.minutes("settle_min", 10) * 60:
            return True, "between two pieces of the night's work"
        return False, "the night's work is done"

    def tick(self, payload: dict | None = None, why: str = "") -> str:
        """Switch or put back, as the night asks. What it did, or ""."""
        with self._lock:
            want, reason = self.wanted()
            switched = bool(self._rec().get("switched"))
            if switched and not want:
                return self.restore(reason)[1]
            if want and not switched:
                f = self._rec().get("failed") or {}
                if f and time.time() - float(f.get("at") or 0) < RETRY_S:
                    return ""  # it failed a moment ago: tried again in a while, not every 2 minutes
                return self.switch(reason)[1]
            return ""

    # --------------------------------------------------------------------------------------- switching
    def _rec(self) -> dict:
        return jload(self.state_file, {}) or {}

    def _save(self, **kw) -> dict:
        with self._lock:
            rec = {**self._rec(), **kw}
            jsave(self.state_file, rec)
            return rec

    def _failed(self, why: str, tell: bool = True) -> tuple[bool, str]:
        if tell:
            f = self._rec().get("failed") or {}
            if f.get("why") != why or time.time() - float(f.get("at") or 0) > RETRY_S:  # not every 2 minutes
                self._save(failed={"at": time.time(), "why": why})
                self.app.events.emit("night_performance_failed", why=why[:200])
        return False, why

    def read(self, way: Way) -> str:
        """The mode now, in words ("" when it can't say)."""
        if way.kind == "mode":
            return (way.get() if way.get else None) or ""
        if way.check:
            r = self.run(way.check, 15)
            return first_line(r.out)[:120] if r.ok else ""
        return ""

    def switch(self, why: str = "night", tell: bool = True) -> tuple[bool, str]:
        with self._lock:
            way, hints = self.detect()
            if way is None:
                return self._failed(hints[0] if hints else "no way to switch was found", tell)
            if way.kind == "commands":
                if way.user and not self.approved():
                    return self._failed("your commands wait for your OK (Allow under /perf)", tell)
                self._save(switched=True, way=way.name, off=way.off, before="", set=way.target, at=time.time())
                r = self.run(way.on or [], 60)
                if not r.ok:
                    self.run(way.off or [], 60)  # undo whatever part of it happened
                    self._save(switched=False, off=None)
                    return self._failed(self.why(r, way.on or []), tell)
                before = ""
            else:
                before = way.get() if way.get else None
                if not before:
                    return self._failed(f"{way.title} didn't say which mode it's in", tell)
                if before == way.target:
                    return True, f"It's in {way.target} already."
                self._save(switched=True, way=way.name, off=None, before=before, set=way.target, at=time.time())
                ok, err = way.put(way.target) if way.put else (False, "")
                if not ok:
                    self._save(switched=False)
                    return self._failed(err or f"{way.title} didn't switch", tell)
            self._save(failed=None, ok_at=time.time())
            self.app.events.emit("night_performance_on", way=way.name, before=before, why=why)
            return True, f"Switched to {way.target or 'the performance mode'} with {way.title}."

    def restore(self, why: str = "", tell: bool = True) -> tuple[bool, str]:
        """Put back what switch() changed: the off command, or the mode it found (unless you changed it since)."""
        with self._lock:
            rec = self._rec()
            if not rec.get("switched"):
                return True, ""
            if rec.get("off"):
                r = self.run(list(rec["off"]), 60)
                if not r.ok:
                    return self._failed(f"putting it back didn't work: {self.why(r, rec['off'])}", tell)
                said = "Put the power mode back."
            else:
                way = self.way_named(str(rec.get("way") or ""))
                before = str(rec.get("before") or "")
                if way is None or not before:
                    self._save(switched=False)
                    return self._failed("it doesn't know how to put the mode back any more", tell)
                now = way.get() if way.get else None
                if now and now != rec.get("set"):
                    self._save(switched=False, left=now)
                    self.app.events.emit("night_performance_left", now=now)
                    return True, f"Left it at {now}: it was changed after Kurooto switched it."
                ok, err = way.put(before) if way.put else (False, "")
                if not ok:
                    return self._failed(f"putting {before} back didn't work: {err}", tell)
                said = f"Back to {before}."
            self._save(switched=False, restored=time.time(), off=None, failed=None)
            self.app.events.emit("night_performance_off", way=rec.get("way"), why=why)
            return True, said

    # -------------------------------------------------------------------------------------------- lifecycle
    def start(self):
        self.every("watch", "every 2m", lambda p=None: self.tick())
        if self._off is None:
            self._off = events.subscribe(self._event)
        # left switched by a crash: put back now, unless the night's work is still going
        threading.Thread(target=self._safe_tick, name="night-performance", daemon=True).start()

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception as e:
            self.app.events.emit("module_failed", module=self.id, error=f"tick: {e}"[:300])

    def _event(self, rec: dict) -> None:
        k = rec.get("kind")
        if k == "power_mode" or (k == "job_queued" and rec.get("lane") in LANES):
            threading.Thread(target=self._safe_tick, name="night-performance", daemon=True).start()

    def stop(self):
        self.shutdown()
        super().stop()

    def shutdown(self):  # Kurooto is stopping (or this was turned off): stop listening, put the mode back
        if self._off is not None:
            self._off()
            self._off = None
        if self._rec().get("switched"):
            self.restore("Kurooto stopped")

    # ------------------------------------------------------------------------------------------- telling
    def dreaming_lines(self) -> list[str]:
        """The morning message's line (modules/dreaming.py), only when switching failed last night."""
        f = self._rec().get("failed") or {}
        if f and time.time() - float(f.get("at") or 0) < TELL_S:
            return [f"Night performance couldn't switch the power mode: {f.get('why', '')[:200]} (/perf)."]
        return []

    def health(self):
        way, hints = self.detect()
        if way is None:
            return False, (hints[0] if hints else "no way to switch found")[:300]
        if way.user and not self.approved():
            return False, "your commands wait for your OK (Allow under /perf)"
        return True, way.title

    def status_lines(self) -> list[str]:
        rec = self._rec()
        return [f"Night performance: {'switched for the night' if rec.get('switched') else 'not switched'}"]

    def setup_form(self) -> dict:
        return {
            "intro": "Kurooto finds how to switch by itself where it can (power-profiles-daemon, tuned, asusctl, a "
            "Lenovo Legion or LOQ with its helper, Windows' power plans). Where it can't, enter the commands: one "
            "program each, no shell; sudo only with -n (a sudo rule for exactly that command). Saving here is your "
            "OK for exactly these lines.",
            "fields": [
                {"name": "on_command", "label": "Command that switches to performance", "type": "text"},
                {"name": "off_command", "label": "Command that puts it back", "type": "text"},
                {"name": "check_command", "label": "Command that prints the mode (optional)", "type": "text"},
            ],
        }

    def configure(self, values: dict) -> dict:
        lines = {k: str(values.get(k) or "").strip() for k in ("on_command", "off_command", "check_command")}
        for k, line in lines.items():
            if line:
                argv, why = self.parse(line)
                if argv is None:
                    return {"ok": False, "message": f"{k}: {why}."}
        if bool(lines["on_command"]) != bool(lines["off_command"]):
            return {"ok": False, "message": "Enter both: the command that switches and the one that puts it back."}
        for k, line in lines.items():
            self.app.settings.set(f"night_performance.{k}", line)
        if lines["on_command"]:
            self.allow()  # you typed them here: that's your OK for exactly these
        return {"ok": True, "message": "Saved. /perf tests a switch and back."}

    # ----------------------------------------------------------------------------------------------- /perf
    def commands(self):
        return [Command("perf", "night performance: what it found, the mode now, a test", self._cmd, "[test]")]

    def callbacks(self):
        return {"nperf": self._button}

    def test(self) -> str:
        """Switch and put back now, saying what happened (Test under /perf)."""
        with self._lock:
            if self._rec().get("switched"):
                return "It's switched for the night now; it goes back when the work is done."
            way, hints = self.detect()
            if way is None:
                return hints[0] if hints else "No way to switch was found."
            before = self.read(way)
            ok, said = self.switch("test", tell=False)
            if not ok:
                return f"Couldn't switch: {said}"
            during = self.read(way)
            back_ok, back = self.restore("test", tell=False)
            after = self.read(way)
            if not back_ok:
                return f"Switched ({during or way.target}), but putting it back didn't work: {back}"
            return f"Works: {before or '?'} → {during or way.target} → {after or '?'} ({way.title})."

    def _button(self, ref: str, act: str) -> str:
        if ref == "test":
            return self.test()[:190]
        if ref == "allow":
            if act != self.digest():
                return "Those commands changed since; /perf again."
            self.allow()
            return self.test()[:190]
        return "Nothing to do."

    def _cmd(self, msg: Incoming, arg: str) -> str:
        a = arg.strip().lower()
        if a == "test":
            way, _ = self.detect()
            if way is not None and way.user and not self.approved():
                return Answer(self.status_text(), self.buttons(way))
            return self.test()
        return Answer(self.status_text(), self.buttons() or None)

    def buttons(self, way: Way | None = None) -> list:
        if way is None:
            way, _ = self.detect()
        if way is None or self._rec().get("switched"):
            return []
        if way.user and not self.approved():
            return [[("Allow these commands and test", f"nperf:allow:{self.digest()}")]]
        return [[("Test: switch and back", "nperf:test:go")]]

    def status_text(self) -> str:
        way, hints = self.detect()
        rec = self._rec()
        lines = []
        if way is None:
            lines.append("Night performance: no way to switch found yet.")
        else:
            now = self.read(way)
            lines.append(f"Night performance: {way.title}" + (f" (now: {now})" if now else "") + ".")
            if way.kind == "commands":
                shown = [("on", way.on), ("off", way.off)] + ([("check", way.check)] if way.check else [])
                lines += [f"  {k}: {shlex.join(v or [])}" for k, v in shown]
                if way.user and not self.approved():
                    lines.append("Your commands run only after you tap Allow below (they start programs you typed).")
        lines += hints
        if rec.get("switched"):
            since = dt.datetime.fromtimestamp(float(rec.get("at") or 0)).strftime("%H:%M")
            was = f" (was {rec['before']})" if rec.get("before") else ""
            lines.append(f"Switched for the night since {since}{was}.")
        try:
            start, end = self.window()
            lines.append(f"Tonight's reading window: {start:%H:%M}-{end:%H:%M}.")
        except Exception:
            pass
        if not self.setting("on"):
            lines.append("It doesn't switch: night_performance.on is off.")
        elif not self.on_charger():
            lines.append("On battery (or paused): it doesn't switch until the charger is in.")
        f = rec.get("failed") or {}
        if f:
            when = dt.datetime.fromtimestamp(float(f.get("at") or 0)).strftime("%a %H:%M")
            lines.append(f"Last problem ({when}): {f.get('why', '')}")
        return "\n".join(lines)
