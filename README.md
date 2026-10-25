# Night performance, a Kurooto add-on

While Kurooto does its heavy night work (the concept graph reading your books and class files into your notes, the
nightly passes), this switches the computer to its highest performance power mode, which on most laptops also runs
the fans harder, so the long reading doesn't cook it. When the work is done it puts back exactly the mode it found.

- **When:** inside tonight's reading window (from the night's start until you usually wake: the alarm, or the night
  hours), while Kurooto's automatic AI work runs, on the charger, and only when you're away from the computer (no
  keyboard or mouse input for `night_performance.away_min` minutes, or the screen locked). It switches back when the
  work has been done for `night_performance.settle_min` minutes, when the window ends, when you come back to the
  computer, or when the laptop goes on battery.
- **How:** it finds the way by itself where it can (below). Where it can't, you enter two commands.
- **`/perf`**: what it found, the mode now, the reading window, the last problem, and a **Test** button that switches
  and puts back at once.

## Add it

```
kurooto addons add https://github.com/avtarfr/kurooto-night-performance --ref v1.0.0
```

or Dashboard → Features → **Add-ons by link**; read the card, Add, then turn on **Night performance** and send
`/perf`.

## Its card

| | |
|---|---|
| Adds | the feature `night_performance`; the command `/perf`; the job `night_performance:watch` (every 2 minutes) |
| May reach | nothing on the internet |
| May start | `powerprofilesctl`, `tuned-adm`, `asusctl`, `system76-power`, `sudo` (only with `-n`), `powercfg`, `pmset`: the tools below, and the commands you enter yourself, which may start only these |
| Runs code | yes: one Python file, run inside Kurooto (and `legion/`, two shell scripts you run yourself once, only on a Lenovo Legion or LOQ) |

## What it finds, per system

Your own commands, when you've entered them, always go first.

**Linux**, in this order:

1. **A Lenovo Legion or LOQ** (the `legion_laptop` driver from LenovoLegionLinux) with the helper installed: the
   `custom` profile (or `performance`) and the fans at full speed. See [below](#lenovo-legion-and-loq-on-linux).
2. **power-profiles-daemon** (GNOME, KDE and most desktops): `powerprofilesctl set performance`, when its driver
   offers a performance profile.
3. **tuned** (Fedora, RHEL): `tuned-adm profile throughput-performance` (or `latency-performance`).
4. **asusctl** (ASUS laptops): `asusctl profile -P Performance`.

None of these needs root. Considered and left to your own commands, because they need root (or a permission of their
own): **TLP** (`tlp ac`), **cpupower** (`cpupower frequency-set -g performance`), **auto-cpufreq**, **system76-power**
(`system76-power profile performance`, which works without root on some setups: it's on the card for your commands),
and the kernel's own **platform profile** (`/sys/firmware/acpi/platform_profile`, which power-profiles-daemon wraps:
`/perf` shows it when nothing else is there).

**Windows:** the **Ultimate Performance** power plan if this computer has it, else **High performance**
(`powercfg /setactive <its GUID>`); the plan that was active is put back by its GUID. Without either plan (many
Windows 11 laptops have only Balanced), the **Best performance** power mode (`powercfg /overlaysetactive`), with the
mode before it read from the registry and put back. No administrator rights needed.

**macOS:** Low Power Mode (and High Power Mode on some MacBook Pros) can only be changed as root, and Kurooto never
asks for a password, so on a Mac this works only with your own commands, for example with a sudo rule that lets
exactly these run without a password:

```
on_command:  sudo -n /usr/bin/pmset -a lowpowermode 0
off_command: sudo -n /usr/bin/pmset -a lowpowermode 1
```

## Putting it back

Before it changes a mode it writes down the one it found (in Kurooto's state folder), then puts exactly that one back.
If you changed the mode yourself in the meantime, it leaves yours alone. With commands (yours, or the Legion helper),
putting back is the off command and nothing else: the command remembers what was there.

Kurooto stopping (or this feature turned off) puts it back at once. After a crash, the next start does (unless the
night's work is still going, then it's put back when the work ends). On battery it never switches, and unplugging
puts it back.

When switching (or putting back) fails at night, Dreaming's morning message has one line about it under Loose ends;
`/perf` shows the reason. Nothing else is said.

## Your own commands

Dashboard → Features → Night performance → **Set up**, or the settings `night_performance.on_command`,
`night_performance.off_command` and, optionally, `night_performance.check_command` (prints the mode, for `/perf`).

- Each is **one program and its arguments**, run without a shell: `;`, `|`, `&&`, `>`, `$(...)` and backquotes are
  refused. Put anything longer in a script, and run the script through `sudo -n` (below).
- The program has to be **one on the card** (`powerprofilesctl`, `tuned-adm`, `asusctl`, `system76-power`, `sudo`,
  `powercfg`, `pmset`): an add-on may only start the programs its card names (Kurooto holds it to that), and the card
  is what you agreed to. For any other program, `sudo -n` it with a sudo rule for exactly that command.
- **sudo only ever runs with `-n`**: `sudo X` becomes `sudo -n X`, and `-S` / `-A` are refused, so nothing waits for a
  password at 2 am. A command that needs root without a rule fails at once, and `/perf` says it needs root.
- **They run only after your OK for exactly those lines**: saving them in Set up is that OK; set any other way
  (Settings, a request), `/perf` shows them with **Allow these commands and test**, and nothing runs at night until
  you tap it. Changing a line asks again.
- The off command has to put back what was there before: Kurooto runs it and nothing else.

A pattern for anything that needs root: a small script owned by root that does exactly one thing, and a sudo rule
that lets your account run exactly that script's lines without a password:

```
# /etc/sudoers.d/night-mode   (edit with: sudo visudo -f /etc/sudoers.d/night-mode)
yourname ALL=(root) NOPASSWD: /usr/local/bin/night-mode on, /usr/local/bin/night-mode off
```

```
on_command:  sudo -n /usr/local/bin/night-mode on
off_command: sudo -n /usr/local/bin/night-mode off
```

The script should remember the mode on "on" (in /run) and put it back on "off", as the Legion helper does.

## Lenovo Legion and LOQ on Linux

With the `legion_laptop` driver from [LenovoLegionLinux](https://github.com/johnfanv2/LenovoLegionLinux), these laptops
have a `custom` platform profile and a full-speed fan switch, both files only root may change. The add-on carries a
small helper for them in `legion/`:

- `kurooto-legion-night on custom|performance fan|nofan`: remembers the profile and the fan setting (in `/run`), then
  sets the profile and, with `fan`, the fans to full speed. A model without `custom` gets `performance`.
- `kurooto-legion-night off`: puts back exactly what `on` remembered.
- `kurooto-legion-night status`: prints both.

Install it once, from the add-on's folder (`/perf` prints the exact line for your computer):

```
sudo bash <the add-on's folder>/legion/install.sh
```

It copies the helper to `/usr/local/bin/kurooto-legion-night` (owned by root) and adds
`/etc/sudoers.d/kurooto-legion-night`, which lets only your account run only the helper's `on`, `off` and `status`
lines without a password (checked with `visudo -c` before it's put in place). It ends by running
`sudo -n /usr/local/bin/kurooto-legion-night status`. Undo:

```
sudo rm /usr/local/bin/kurooto-legion-night /etc/sudoers.d/kurooto-legion-night
```

From then on the add-on uses it by itself: no commands to enter. `night_performance.legion_profile` picks `custom`
(default) or `performance`; `night_performance.legion_max_fan` off leaves the fans to the firmware. Until the helper
is installed, power-profiles-daemon (if you have it) does the profile alone.

## Settings (Dashboard → Settings → night_performance)

| Setting | Default | Effect |
|---|---|---|
| `night_performance.on` | on | switch at night; off keeps `/perf` but never switches |
| `night_performance.on_command` | empty | your own command that switches to the performance mode |
| `night_performance.off_command` | empty | your own command that puts the mode back |
| `night_performance.check_command` | empty | your own command that prints the mode now (for `/perf`) |
| `night_performance.hold_window` | off | on: stays switched for the whole reading window while on the charger, whether you're at the computer or not and between pieces of work; back when the window ends or the charger comes out |
| `night_performance.away_min` | 10 | switch only after this many minutes without input (or with the screen locked) |
| `night_performance.settle_min` | 10 | minutes without night work before it switches back |
| `night_performance.legion_profile` | `custom` | Legion and LOQ with the helper: `custom` or `performance` |
| `night_performance.legion_max_fan` | on | Legion and LOQ with the helper: the fans at full speed too |

## Remove it

Turn it off (that puts the mode back), then `kurooto addons remove night-performance` (`kurooto addons undo
night-performance` puts it back). On a Legion or LOQ, also the Undo line above.

## Tests

Inside a Kurooto checkout, with this folder at `addons/night-performance/`: `uv run pytest addons/night-performance`.
Only stand-ins run: PATH holds nothing but fake `powerprofilesctl`, `tuned-adm`, `asusctl`, `powercfg`, `pmset` and
`sudo`, `/sys` is a pretend tree, and Kurooto's test guard refuses the real ones, so the computer's power mode is never
touched.
