"""One server: its driver, settings, history and control loop. And the registry of all of them."""

import json
import logging
import os
import random
import re
import threading
import time
from collections import deque

from .alerts import alert_config, notify
from .config import (DATA_DIR, HISTORY_SECONDS, INTERVAL, LONG_BUCKET, LONG_SECONDS, SAVE_EVERY, STALL_SECONDS, read_json,
                     write_json)
from .control import (DEFAULT_SETTINGS, aggregate, apply_schedule, curve_speed, decide, failed_fans, learned_curve,
                      quiet_cap, ramped, smart_step, speed_text, validate_learned, validate_settings)
from .drivers import DRIVERS, HOST_RE, DemoDriver, DriverError, RedfishDriver

log = logging.getLogger("fanctl.server")
LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}

# ---------------------------------------------------------------- one server

class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.id, self.name, self.host = cfg["id"], cfg["name"], cfg.get("host", "")
        self.driver = DRIVERS[cfg["driver"]](cfg.get("host", ""), cfg.get("username", ""), cfg.get("password", ""),
                                             cfg.get("verify_tls", False), str(DATA_DIR))
        self.driver.pin = True  # remember the BMC's certificate (see drivers.PinnedHTTPSHandler)
        # the single-server setup of 1.0 keeps its settings file
        self.settings_file = DATA_DIR / ("settings.json" if cfg.get("legacy") else f"settings-{self.id}.json")
        self.history_file = DATA_DIR / f"history-{self.id}.json"
        self.smart_file = DATA_DIR / f"smart-{self.id}.json"  # what smart mode learned about this server
        self.calib_file = DATA_DIR / f"calibration-{self.id}.json"  # the last fan calibration's results
        self.handed_file = DATA_DIR / f"handover-{self.id}.json"  # fans left to the BMC for a while
        self.changes_file = DATA_DIR / f"changes-{self.id}.json"  # the last settings saved, to look back on
        self.lock = threading.RLock()  # re-entrant: log() takes it, and is called with it held
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.history = deque(maxlen=HISTORY_SECONDS // INTERVAL)
        self.long = deque(maxlen=LONG_SECONDS // LONG_BUCKET)  # 5-minute averages for 7 days
        self.bucket = []
        self.events = deque(maxlen=100)
        self.window = deque()  # (time, target) pairs for the ramp-down delay
        self.smart = {}        # smart mode controller memory
        self.learned = self.load_learned()  # smart mode: heat load -> speed that held the target
        self.learned_saved = dict(self.learned)
        self.calib = self.load_calibration()  # see start_calibration
        self.handed_until = self.load_handover()  # see hand_over
        self.logged = None     # last speed written to the event log
        self.hot = False       # above the "running hot" alert threshold
        self.inlet_hot = False
        self.failsafe_since = None  # start of the current failsafe, for the "failsafe for N min" alert
        self.failsafe_told = False
        self.fan_strikes = {}  # fan name -> readings in a row it looked failed
        self.fans_failed = set()
        self.spun = set()          # fans seen turning since the start: only those can fail (see check_fans)
        self.idle_told = set()
        self.last_manual = None  # (speed, rpm) of the previous cycle, for the command check
        self.probe = None        # a speed change whose effect on the RPM is being checked
        self.ignored = False     # the BMC did not follow the last checked change
        self.ignored_at = 0
        self.known_temps = set()   # every temperature sensor seen since the loop started
        self.missing = {}          # known sensor -> readings in a row it has been absent
        self.blind_tried = 0       # last attempt to hand the fans back while the BMC could not be read
        self.misses = 0            # readings failed in a row (see READ_MISSES)
        self.told_thresholds = False
        self.rpm_floor = 0         # % below which a fan read under its BMC minimum (see check_fan_floors)
        self.retakes = []          # times to take the fans back from the BMC again (see command)
        self.retook = 0
        self.rpm_at = {}           # speed -> RPM seen while the fans followed (see check_follow)
        self.follow_speed, self.follow_since, self.follow_strikes = None, 0, 0
        self.not_following, self.follow_ok_at = False, 0
        self.resume_until = 0      # easing out of a failsafe until then
        self.saved = time.time()
        self.bad_settings = None  # last validation error of the settings file, logged once
        self.pcie_applied = None
        self.dry_released = False
        self.thread = None
        self.tick = time.time()  # last turn of the control loop, for the watchdog
        # since the process started, for Prometheus counters
        self.counters = {"bmc_errors": 0, "fan_commands": 0, "fan_commands_refused": 0, "failsafe_trips": 0}
        self.cmd_lock = threading.Lock()  # fan commands vs. release(): never both at once
        self.state = {"sensors": None, "cpu_temp": None, "effective": None, "applied_speed": None,
                      "target_speed": None, "reason": "", "failsafe": False, "error": None,
                      "updated": None, "power": None, "model": None, "dry_run": False, "smart": None,
                      "smart_map": [], "following": None}

    @property
    def control(self):
        return self.driver.control

    # ---- settings and persistence

    def settings(self):
        """The saved settings, validated. When the file is corrupt or holds something invalid the
        safe answer is the BMC's own fan control, never a curve nobody checked."""
        saved = read_json(self.settings_file, {}, f"settings of {self.name}", corrupt=False)
        if saved is False:  # corrupt, now kept aside: start again from automatic, and say so
            s = {**DEFAULT_SETTINGS, "mode": "auto"}
            write_json(self.settings_file, s)
            self.log("The settings file was corrupt and was kept aside; the BMC controls the fans until "
                     "settings are applied again", "error")
            return s
        if not isinstance(saved, dict):
            saved = {}
        if saved.get("mode") == "dell":  # 1.x name of automatic mode
            saved["mode"] = "auto"
        # 2.3 lowered the failsafe's ceiling: older files are brought inside it rather than refused
        if type(saved.get("failsafe_temp")) in (int, float) and saved["failsafe_temp"] > 90:
            saved["failsafe_temp"] = 90
        try:
            s = validate_settings(saved, dict(DEFAULT_SETTINGS))
            self.bad_settings = None
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            if self.bad_settings != str(e):
                self.bad_settings = str(e)
                self.log(f"Saved settings are invalid ({e}); the BMC controls the fans", "error")
            s = {**DEFAULT_SETTINGS, "mode": "auto"}
        return s

    CHANGES_KEPT = 20  # settings versions kept per server

    def save_settings(self, s, who=""):
        write_json(self.settings_file, s)
        rows = self.changes()
        rows.append({"t": round(time.time()), "who": who, "settings": s})
        try:
            write_json(self.changes_file, rows[-self.CHANGES_KEPT:])
        except OSError as e:  # the settings themselves are saved; only the record of them is not
            log.error("could not keep the settings history: %s", e, extra={"server": self.id})
        with self.lock:
            self.window.clear()  # a new setting applies now, not after the ramp-down delay
        self.log(f"Settings saved{' by ' + who if who else ''}: mode {s['mode']}")
        notify(self, "settings_changed", mode=s["mode"], failsafe=s["failsafe_temp"])
        self.wake.set()

    def changes(self):
        """The settings saved before, oldest first: {t, who, settings}."""
        rows = read_json(self.changes_file, [], f"settings history of {self.name}")
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("settings"), dict)] if isinstance(rows, list) else []

    def load_history(self):
        try:
            data = json.loads(self.history_file.read_text())
        except (OSError, ValueError):
            return False
        cutoff = time.time() - HISTORY_SECONDS
        self.history.extend(p for p in data.get("history", []) if p.get("t", 0) > cutoff)
        self.long.extend(p for p in data.get("long", []) if p.get("t", 0) > time.time() - LONG_SECONDS)
        self.events.extend(e for e in data.get("events", []) if isinstance(e, dict))
        rpm_at = data.get("rpm_at")
        if isinstance(rpm_at, dict):
            self.rpm_at = {int(k): v for k, v in rpm_at.items() if str(k).isdigit() and type(v) in (int, float)}
        return bool(self.history)

    def save_history(self):
        with self.lock:
            data = {"history": list(self.history), "long": list(self.long), "events": list(self.events),
                    "rpm_at": dict(self.rpm_at)}
            learned = dict(self.learned)
        try:
            write_json(self.history_file, data)
            if learned != self.learned_saved:
                write_json(self.smart_file, learned)
                self.learned_saved = learned
        except OSError as e:
            log.error("could not save history: %s", e, extra={"server": self.id})
        self.saved = time.time()

    def load_learned(self):
        try:
            return validate_learned(json.loads(self.smart_file.read_text()))
        except (OSError, ValueError):
            return {}

    def forget_learned(self, who=""):
        with self.lock:
            self.learned.clear()
            self.learned_saved = {}
            self.state["smart_map"] = []
        try:
            self.smart_file.unlink()
        except OSError:
            pass
        self.log(f"Smart mode starts learning afresh{' (' + who + ')' if who else ''}")

    # ---- hand-over: the fans left to the BMC for a while (maintenance, a stress test), then taken back

    def load_handover(self):
        h = read_json(self.handed_file, None, f"hand-over of {self.name}")
        until = h.get("until") if isinstance(h, dict) else None
        return until if type(until) in (int, float) and until > time.time() else None

    def hand_over(self, minutes, who=""):
        """Leave the fans to the BMC for this many minutes; 0 takes them back now. Kept on disk, so a
        restart in the middle neither cuts it short nor makes it last for ever."""
        until = round(time.time() + minutes * 60) if minutes else None
        with self.lock:
            was, self.handed_until = self.handed_until, until
        by = f" by {who}" if who else ""
        try:
            if until:
                write_json(self.handed_file, {"until": until, "by": who})
            else:
                self.handed_file.unlink(missing_ok=True)
        except OSError as e:
            log.error("could not save the hand-over: %s", e, extra={"server": self.id})
        if until:
            self.log(f"Fans handed to the BMC until {time.strftime('%H:%M', time.localtime(until))}{by}")
        elif was:
            self.log(f"Fans taken back from the BMC{by}")
        self.wake.set()

    # ---- fan calibration
    #
    # Runs the fans through CALIB_STEPS and measures what each speed gives: the RPM curve, the speed
    # below which the fans turn no slower, and the lowest speed at which every fan spins; what the
    # minimum speed is best set from. Every check stays on while it runs: the failsafe, the BMC's
    # thresholds, failed fans, a BMC ignoring commands. Any of them, or the CPU warming by
    # CALIB_WARMING, stops it, and normal control resumes. Fastest first: when a fan stops at a low
    # step and the failsafe takes over, every step above it is already measured.

    CALIB_STEPS = (100, 80, 60, 50, 40, 30, 25, 20, 15, 10, 5, 0)
    CALIB_SETTLE = 15    # s for the fans to reach a new speed before it is measured
    CALIB_SAMPLES = 3    # readings averaged at each step
    CALIB_EVERY = 5      # s between readings while it runs
    CALIB_WARMING = 10   # °C the CPU may warm before it stops
    CALIB_STOPPED = 300  # RPM under which a fan counts as stopped (as failed_fans has it)
    CALIB_MARGIN = 1.15  # no slower step once a fan is within 15 % of its BMC minimum

    def load_calibration(self):
        c = read_json(self.calib_file, None, f"fan calibration of {self.name}")
        if not isinstance(c, dict) or not isinstance(c.get("results"), list):
            return None
        if c.get("status") == "running":  # the process stopped in the middle
            c.update(status="stopped", reason="Fan Control restarted", samples=[])
        return c

    def calibrating(self):
        return bool(self.calib) and self.calib.get("status") == "running"

    def start_calibration(self, who=""):
        """Start a calibration. Returns why it cannot start, or None."""
        settings = self.settings()
        with self.lock:
            st = self.state
            fans = (st["sensors"] or {}).get("fans", [])
            why = ("this server is monitored only" if not self.control
                   else "a calibration is already running" if self.calibrating()
                   else "the fans are handed to the BMC; take them back first" if self.handed_until
                   else "the BMC is not answering" if st["error"] or not st["sensors"]
                   else "the server is powered off" if st["power"] == "off"
                   else "dry run is on; turn it off first" if settings["dry_run"]
                   else "the failsafe is in control" if st["failsafe"]
                   else "no fan reports its RPM" if not any(f["rpm"] for f in fans)
                   else "there is no CPU reading" if st["cpu_temp"] is None
                   else f"the CPU is at {st['cpu_temp']:.0f}°C; calibrate when it is 10°C or more below the failsafe"
                   if st["cpu_temp"] > settings["failsafe_temp"] - 10 else None)
            if why:
                return why
            now = time.time()
            floor = getattr(self.driver, "min_speed", 0)
            self.calib = {"status": "running", "started": now, "by": who, "cpu": st["cpu_temp"],
                          "steps": [p for p in self.CALIB_STEPS if p >= floor], "i": 0, "since": now,
                          "samples": [], "results": [], "reason": "", "floor": None, "suggested": None}
            self.smart = {}
            self.retakes = [now]  # measured fans must be Fan Control's: take hold properly first
            minutes = round(len(self.calib["steps"]) * (self.CALIB_SETTLE + self.CALIB_SAMPLES * self.CALIB_EVERY) / 60)
        self.log(f"Fan calibration started{' by ' + who if who else ''}: about {minutes} min")
        self.wake.set()
        return None

    def calibration_step(self, failsafe, why_failsafe, cpu, sensors, power, dry, now):
        """While a calibration runs: (speed, reason line) for this cycle. It measures the step the fans
        are on, moves to the next one, and finishes or stops the calibration. None otherwise."""
        with self.lock:
            c = self.calib
            if not self.calibrating():
                return None
            rpms = {f["name"]: [f["rpm"], f.get("ok", True)] for f in sensors["fans"] if f["rpm"] is not None}
            why = (f"the failsafe took over ({why_failsafe})" if failsafe
                   else "the fans were handed to the BMC" if self.handed_until
                   else "the server powered off" if power == "off"
                   else "dry run was turned on" if dry
                   else "the CPU reading was lost" if cpu is None
                   else f"the CPU warmed by {cpu - c['cpu']:.0f}°C" if cpu - c["cpu"] >= self.CALIB_WARMING
                   else "the fans stopped reporting their RPM" if not rpms else None)
            if why:
                self.finish_calibration("stopped", why)
                return None
            # under a fan's lower threshold the BMC logs an event, lights the amber LED and may run every
            # fan at full speed: never go there, and never one step closer than CALIB_MARGIN above it
            floors = {f["name"]: f["floor"] for f in sensors["fans"] if f.get("floor")}
            under = next((n for n, (rpm, _) in rpms.items() if n in floors and rpm < floors[n]), None)
            if under:
                self.finish_calibration("done" if c["results"] else "stopped",
                                        f"{under} went under its BMC minimum of {floors[under]:.0f} RPM")
                return None
            if now - c["since"] >= self.CALIB_SETTLE:
                c["samples"].append({"rpm": rpms, "watts": sensors["watts"]})
                if len(c["samples"]) >= self.CALIB_SAMPLES:
                    seen = [s["rpm"] for s in c["samples"]]
                    fans = {n: round(sum(s[n][0] for s in seen if n in s) / sum(n in s for s in seen))
                            for n in sorted({n for s in seen for n in s})}
                    ok = all(s[n][1] for s in seen for n in s) and min(fans.values()) >= self.CALIB_STOPPED
                    watts = [s["watts"] for s in c["samples"] if s["watts"] is not None]
                    c["results"].append({"pct": c["steps"][c["i"]], "rpm": round(sum(fans.values()) / len(fans)),
                                         "fans": fans, "ok": ok,
                                         "watts": round(sum(watts) / len(watts)) if watts else None})
                    c["i"], c["since"], c["samples"] = c["i"] + 1, now, []
                    near = next((n for n, rpm in fans.items() if n in floors and rpm < floors[n] * self.CALIB_MARGIN), None)
                    if near:
                        self.finish_calibration("done", f"{near} turned {fans[near]} RPM, close to its BMC minimum of "
                                                        f"{floors[near]:.0f} RPM")
                        return None
                    if c["i"] == len(c["steps"]):
                        self.finish_calibration("done")
                        return None
            pct = c["steps"][c["i"]]
            return pct, f"fan calibration, step {c['i'] + 1} of {len(c['steps'])}: {pct}%"

    def finish_calibration(self, status, reason=""):
        """End the calibration ("done" or "stopped"), work out what it found and keep it on disk.
        Called with self.lock held."""
        c = self.calib
        at = c["steps"][min(c["i"], len(c["steps"]) - 1)]
        c.update(status=status, reason=reason, ended=time.time(), samples=[], at=at)
        res = c["results"]
        spinning = None
        for r in res:  # fastest first: the lowest speed down to which every step kept every fan turning
            if not r["ok"]:
                break
            spinning = r["pct"]
        if status == "done":
            if reason:  # ended before 0 %: the steps measured until then stand
                self.log(f"Fan calibration went no slower: {reason}")
            slowest = res[-1]["rpm"]
            c["floor"] = max(r["pct"] for r in res if r["rpm"] <= slowest * 1.05)  # below it, no slower
            c["suggested"] = None if spinning is None else max(c["floor"], spinning)
            # what the fans themselves draw at full speed over the slowest: the server's power, steady load
            if res[0]["watts"] is not None and res[-1]["watts"] is not None:
                c["fan_watts"] = res[0]["watts"] - res[-1]["watts"]
            self.log(f"Fan calibration finished: {res[0]['rpm']} RPM at {res[0]['pct']}%, {slowest} RPM at "
                     f"{res[-1]['pct']}%" + ("; no speed kept every fan turning" if spinning is None else
                                             f"; suggested minimum speed {c['suggested']}%"),
                     "warn" if spinning is None else "info")
        elif reason.startswith("the failsafe took over") and spinning is not None:
            # a fan stopped at this step, or the BMC took over: the steps above it still tell
            c["suggested"] = spinning
            self.log(f"Fan calibration stopped at {at}%: {reason}; every fan kept turning down to {spinning}%", "warn")
        else:
            self.log(f"Fan calibration stopped at {at}%: {reason}", "warn")
        self.wake.set()
        try:
            write_json(self.calib_file, c)
        except OSError as e:
            log.error("could not save the fan calibration: %s", e, extra={"server": self.id})

    def log(self, msg, level="info"):
        with self.lock:  # HTTP threads log too, while reports iterate over the events
            self.events.appendleft({"t": time.time(), "level": level, "msg": msg})
        log.log(LEVELS.get(level, logging.INFO), "%s", msg, extra={"server": self.id})

    def demo_backfill(self):
        now = time.time()
        for i in range(self.long.maxlen, HISTORY_SECONDS // LONG_BUCKET, -1):
            t = now - i * LONG_BUCKET
            day = 6 * abs(((t / 43200) % 2) - 1)  # warmer by day
            cpu = DemoDriver.cpu_at(t) + day
            speed = None if i % 97 < 3 else curve_speed(DEFAULT_SETTINGS["curve"], cpu)
            self.long.append({"t": round(t), "cpu": round(cpu, 1), "cpu_max": round(cpu + 2, 1), "speed": speed,
                              "inlet": round(DemoDriver.inlet_at(t) + day / 3, 1), "exhaust": round(cpu - 12, 1),
                              "rpm": 1800 + (speed or 60) * 120, "fanpct": None,
                              "watts": round(100 + cpu + random.uniform(-2, 2))})
        for i in range(self.history.maxlen, 0, -1):
            t = now - i * INTERVAL
            cpu = DemoDriver.cpu_at(t)
            speed = None if 1500 < i * INTERVAL < 1900 else curve_speed(DEFAULT_SETTINGS["curve"], cpu)
            self.history.append({"t": round(t), "cpu": round(cpu, 1), "speed": speed,
                                 "inlet": round(DemoDriver.inlet_at(t), 1), "exhaust": round(cpu - 12, 1),
                                 "rpm": 1800 + (speed or 60) * 120, "fanpct": None,
                                 "watts": round(100 + cpu + random.uniform(-2, 2))})

    # ---- control loop
    #
    # One cycle: read the BMC, work out what could be wrong (protect), decide a speed, send it, then
    # report. The rule throughout: when the controller does not know, the BMC decides. A missing CPU
    # reading, a sensor that stops reporting or reports a fault, a failed fan, a BMC that ignores
    # fan commands, an unreadable BMC: all of them hand the fans back.

    FAILSAFE_HOLD = 300   # s the BMC keeps the fans once a limit tripped, however fast things cool
    RESUME_SPEED = 50     # % manual control eases down from after a failsafe, instead of dropping at once
    MISSING_AFTER = 2     # readings in a row a known sensor may be absent before it counts as lost
    IGNORED_RETRY = 600   # s before manual control is tried again on a BMC that ignored it
    BLIND_RETRY = 60      # s between attempts to hand the fans back while the BMC cannot be read
    READ_MISSES = 2       # readings in a row that must fail before the BMC counts as unreadable: one failed
                          # reading (a busy iDRAC drops some) made the fans jump and sent two alerts

    def cycle(self):
        now = time.time()
        local = time.localtime(now)
        settings, (profile_cap, profile) = apply_schedule(self.settings(), local)
        quiet = quiet_cap(settings["quiet"], local)
        # the lowest cap in force, and what to call it in the reason line
        cap, cap_name = min(((c, n) for c, n in ((quiet, "quiet hours"), (profile_cap, profile)) if c is not None),
                            default=(None, None))
        dry = self.control and settings["dry_run"]
        sensors = self.read_sensors(now, dry)
        if sensors is None:
            return
        power = sensors.pop("power") or self.state["power"]  # not known this time: as it was
        if power == "on" and self.state["power"] == "off":
            # a starting host can hand the fans back to the BMC, more than once (seen on iDRAC 7): take
            # them back now, and again when the start-up is likely over
            self.retakes = [now, now + 180, now + 480]
            self.log("Host powered on: taking the fans back from the BMC while it starts")

        # only a CPU sensor counts as the CPU: standing in the inlet or a DIMM would run the fans on the
        # floor while the CPU cooks, so without one the BMC decides
        cpus = [t["value"] for t in sensors["temps"] if t["cpu"]]
        cpu = max(cpus) if cpus else None
        sensors["faults"] = self.faults(sensors, power, now)
        was_failsafe = self.state["failsafe"]
        effective, target, reason, failsafe = self.protect(settings, cpu, sensors, power, was_failsafe, now)

        smart = None
        calib = self.calibration_step(failsafe, reason, cpu, sensors, power, dry, now)
        if calib:  # the step's speed as it is, past quiet hours and ramps: someone started this by hand
            (speed, reason), effective = calib, "manual"
            target = speed
            self.smart = {}
        else:
            if effective == "manual" and settings["mode"] == "smart":
                with self.lock:  # forget_learned() clears the map from the HTTP thread
                    # while the BMC has the fans, what held the target was not this speed: learn nothing
                    learned = dict(self.learned) if self.not_following else self.learned
                    target, smart = smart_step(self.smart, learned, settings, cpu, sensors, now, cap)
                smart["reason"] = smart["reason"].replace(", quiet hours cap", f", {cap_name} cap")
                reason = smart["reason"]
                if target is None:
                    effective, reason, smart = "auto", "smart mode has no temperature to aim at", None
            else:
                self.smart = {}  # start afresh next time smart mode takes over; what it learned stays
            speed, reason = self.choose_speed(settings, effective, target, reason, smart, cap, cap_name, was_failsafe,
                                              failsafe, now)
        effective, speed, reason, error = self.command(settings, effective, speed, reason, dry)
        if effective is None:  # stopped while reading: release() owns the fans now
            return
        self.report(settings, sensors, power, cpu, effective, target, speed, reason, failsafe, was_failsafe,
                    error, dry, smart, now)

    def read_sensors(self, now, dry):
        """The BMC's readings, or None when it cannot be read. Then the last manual speed would stay
        with nobody watching, so the fans are handed back whenever the BMC still takes a command."""
        try:
            sensors = self.driver.read()
        except DriverError as e:
            self.counters["bmc_errors"] += 1
            self.misses += 1
            if self.misses < self.READ_MISSES:
                return None  # a busy BMC drops a reading now and then: the last decision holds one cycle longer
            with self.lock:
                if self.state["error"] != str(e):
                    self.log(f"Cannot read the BMC: {e}", "error")
                    if self.state["error"] is None:
                        notify(self, "unreachable", error=str(e))
                self.state.update(error=str(e), updated=now)
            if self.control and not dry and now - self.blind_tried > self.BLIND_RETRY:
                self.blind_tried = now
                with self.cmd_lock:
                    if not self.stop.is_set():
                        try:
                            self.driver.set_auto()
                            with self.lock:
                                if self.state["effective"] != "auto":
                                    self.log("Fans handed back to the BMC while it cannot be read", "warn")
                                self.state.update(effective="auto", applied_speed=None,
                                                  reason="the BMC cannot be read; it controls the fans")
                        except DriverError:
                            pass  # it doesn't answer at all; the error above says so already
            return None
        self.blind_tried, self.misses = 0, 0
        self.state["model"] = self.driver.model or self.state["model"]
        if getattr(self.driver, "thresholds", None) == {} and not self.told_thresholds:
            self.told_thresholds = True
            why = getattr(self.driver, "thresholds_error", "")
            self.log(f"The BMC's warning thresholds could not be read{f' ({why})' if why else ''}; protection follows "
                     "the CPU and exhaust limits until they can (retried every 10 min)", "warn")
        elif getattr(self.driver, "thresholds", None):
            self.told_thresholds = False
        return sensors

    def faults(self, sensors, power, now):
        """What makes the readings untrustworthy, each as a reason to hand the fans back: a sensor
        that was there and is gone, one the BMC reports as faulty, a failed fan, a BMC that ignores
        fan commands. Sensors are learned while the loop runs; a restart forgets removed hardware."""
        present = {t["name"] for t in sensors["temps"]}
        if power == "off":
            self.missing = {}  # an idle host drops its CPU sensors; that is not a fault
        else:
            self.missing = {n: self.missing.get(n, 0) + 1 for n in self.known_temps - present}
        self.known_temps |= present
        out = [f"{n} stopped reporting" for n, c in sorted(self.missing.items()) if c >= self.MISSING_AFTER]
        out += [f"{t['name']} reports a fault" for t in sensors["temps"] if t.get("ok") is False]
        out += [f"fan {n} failed" for n in sorted(self.fans_failed)]
        if self.ignored:
            if now - self.ignored_at < self.IGNORED_RETRY:
                out.append("the BMC ignored the last fan command")
            else:
                self.ignored = False
                self.log("Trying manual fan control again")
        return out

    def protect(self, settings, cpu, sensors, power, was_failsafe, now):
        """(effective, target, reason, failsafe). A failsafe holds for FAILSAFE_HOLD seconds even
        when things cool at once, so the fans don't flap between the BMC and the controller."""
        if not self.control:
            return "monitor", None, "monitoring only", False
        effective, target, reason, failsafe = decide(settings, cpu, was_failsafe, sensors)
        if power == "off":
            return "auto", None, "server is powered off", failsafe
        if was_failsafe and not failsafe and self.failsafe_since and now - self.failsafe_since < self.FAILSAFE_HOLD:
            left = self.FAILSAFE_HOLD - (now - self.failsafe_since)
            return "auto", None, f"failsafe held for {left / 60:.0f} more min", True
        if self.handed_until:
            if now < self.handed_until:
                return "auto", None, f"handed to the BMC until {time.strftime('%H:%M', time.localtime(self.handed_until))}", failsafe
            self.handed_until = None
            self.log("The hand-over to the BMC ended; control resumes")
        return effective, target, reason, failsafe

    def choose_speed(self, settings, effective, target, reason, smart, cap, cap_name, was_failsafe, failsafe, now):
        """The speed to send in manual control, after ramp-down, caps, the driver's own floor and a
        gentle way out of a failsafe."""
        if effective != "manual":
            with self.lock:
                self.window.clear()
            return None, reason
        if was_failsafe and not failsafe:
            self.resume_until = now + max(60, settings["ramp_down_seconds"])
        with self.lock:  # save_settings() clears the window from the HTTP thread
            if smart:
                speed = target  # smart mode holds, slows down and keeps the caps on its own
            else:
                easing = now < self.resume_until
                speed = ramped(self.window, now, max(target, self.RESUME_SPEED) if easing else target,
                               settings["ramp_down_seconds"])
        if speed > target and not smart:
            reason += f", holding {speed}% for ramp-down"
        if cap is not None and not smart and speed > cap:
            speed = max(cap, settings["min_speed"])
            reason += f", {cap_name} cap {speed}%"
        if smart and now < self.resume_until and speed < self.RESUME_SPEED:
            speed = self.RESUME_SPEED
            self.smart["last"] = float(speed)  # smart mode eases down from here at its own pace
            reason += ", easing out of the failsafe"
        floor = getattr(self.driver, "min_speed", 0)
        if speed < floor:  # below this some BMCs see a stalled fan and go to full speed
            speed = floor
            reason += f", {self.driver.vendor} floor {floor}%"
        if speed < self.rpm_floor:
            speed = self.rpm_floor
            reason += f", BMC fan minimum {speed}%"
        return speed, reason

    def command(self, settings, effective, speed, reason, dry):
        """Send the decision. Returns (effective, speed, reason, error); effective None means the loop
        was stopped while it worked and must not touch the fans."""
        error = None
        with self.cmd_lock:
            if self.stop.is_set():
                return None, None, reason, None
            if dry:
                # decide as usual, but leave the fans to the BMC: hand them over once, then send nothing
                if not self.dry_released:
                    try:
                        self.driver.set_auto()
                    except DriverError as e:
                        self.log(f"Could not hand the fans to the BMC for the dry run: {e}", "error")
                    self.dry_released = True
            elif self.control:
                # Taking the fans from the BMC, whatever had them: the start, the end of a failsafe, a
                # hand-over or a dry run, a mode change from automatic, a refused command. Every one of
                # these lets go and takes hold again (driver.retake), since an iDRAC can ignore the bare
                # manual-mode command (issue #3); so do the times scheduled after a host power-on.
                taking = self.dry_released or self.state["effective"] != "manual"
                self.dry_released = False
                try:
                    if effective == "auto":
                        self.driver.set_auto()
                    elif taking or (self.retakes and time.time() >= self.retakes[0]):
                        self.retakes = [t for t in self.retakes if t > time.time()]
                        self.retook = time.time()
                        self.driver.retake(speed)
                    else:
                        self.driver.set_speed(speed)  # re-sent every cycle: a BMC reset silently returns to auto
                    self.counters["fan_commands"] += 1
                except DriverError as e:
                    self.counters["fan_commands_refused"] += 1
                    error = f"fan command refused: {e}"
                    effective, speed, reason = "auto", None, "the BMC refused the fan command"
            pcie = settings["pcie_cooling"]
            if self.control and not dry and self.driver.pcie and pcie is not None and pcie != self.pcie_applied:
                try:
                    self.driver.set_pcie(pcie)
                    self.log(f"Third-party PCIe cooling response {'enabled' if pcie else 'disabled'}",
                             "info" if pcie else "warn")
                except DriverError as e:
                    self.log(f"Third-party PCIe cooling command refused: {e}", "error")
                self.pcie_applied = pcie  # tried once per change, not every cycle
        notice, self.driver.notice = self.driver.notice, None
        if notice:
            self.log(notice, "warn")
        return effective, speed, reason, error

    def report(self, settings, sensors, power, cpu, effective, target, speed, reason, failsafe, was_failsafe,
               error, dry, smart, now):
        """Events, alerts, state and history for one cycle."""
        vals = {"cpu": "—" if cpu is None else f"{cpu:.0f}", "reason": reason, "error": error or "",
                "speed": speed_text(effective, speed)}
        with self.lock:
            prev = self.state
            # every change of who is in control, but speed changes only from 5 % on: a slow
            # ramp moves 1 % at a time and would push everything else out of the event log. A
            # calibration's steps are its own start and end lines.
            if effective != "monitor" and not self.calibrating() and (effective != prev["effective"] or (
                    speed is not None and (self.logged is None or abs(speed - self.logged) >= 5))):
                self.log(("Dry run: would set fans to " if dry else "Fans → ")
                         + f"{'automatic' if effective == 'auto' else f'{speed}%'} ({reason})",
                         "warn" if failsafe or error else "info")
                self.logged = speed
            if prev["error"] and not error:
                self.log("BMC responding again", "info")
                notify(self, "recovered", **vals)
                self.retakes = [now]  # a BMC that was reset or rebooted is back on its own control
            if error and error != prev["error"]:
                self.log(error, "error")
                notify(self, "refused", **vals)
            if failsafe and not was_failsafe:
                self.counters["failsafe_trips"] += 1
                notify(self, "failsafe", **vals)
            if was_failsafe and not failsafe:
                self.log(f"Failsafe cleared at CPU {cpu:.0f}°C" if cpu is not None else "Failsafe cleared")
                notify(self, "failsafe_cleared", **vals)
            self.check_limits(sensors, cpu, failsafe, vals, now)
            self.check_fans(sensors["fans"], vals)
            if prev["effective"] == "manual" and not dry and not self.calibrating():
                self.check_fan_floors(sensors["fans"], prev["applied_speed"])
            self.check_response(effective if not dry else "dry", speed, sensors["fans"], vals)
            manual = prev["effective"] == "manual" and not prev["dry_run"] and not self.calibrating()
            self.check_follow(prev["applied_speed"] if manual else None, sensors["fans"], now, vals)
            following = False if self.not_following else True if now - self.follow_ok_at < 600 else None
            self.state.update(sensors=sensors, cpu_temp=cpu, effective=effective, applied_speed=speed,
                              target_speed=target, reason=reason, failsafe=failsafe, error=error,
                              updated=now, power=power, dry_run=dry, smart=smart, following=following,
                              smart_map=learned_curve(self.learned, settings, sensors["inlet"]))
            self.record({"t": round(now), "cpu": cpu, "speed": speed,
                         "inlet": sensors["inlet"], "exhaust": sensors["exhaust"],
                         "rpm": fan_avg(sensors["fans"], "rpm"), "fanpct": fan_avg(sensors["fans"], "pct"),
                         "watts": sensors["watts"],
                         "target": settings["smart_target"] if smart else None})

    def check_limits(self, sensors, cpu, failsafe, vals, now):
        """The temperature alerts: running hot, room running hot, failsafe lasting. Called with self.lock held."""
        cfg = alert_config()
        threshold = cfg["hot_threshold"]
        if cpu is not None and cpu >= threshold and not self.hot:
            self.hot = True
            notify(self, "hot", **vals)
        elif cpu is None or cpu < threshold - 3:
            self.hot = False
        inlet = sensors["inlet"]
        if inlet is not None and inlet >= cfg["inlet_threshold"] and not self.inlet_hot:
            self.inlet_hot = True
            self.log(f"Inlet air at {inlet:.0f}°C: the room is running hot", "warn")
            notify(self, "inlet_hot", **vals, inlet=f"{inlet:.0f}")
        elif inlet is None or inlet < cfg["inlet_threshold"] - 2:
            self.inlet_hot = False
        if failsafe:
            self.failsafe_since = self.failsafe_since or now
            minutes = (now - self.failsafe_since) / 60
            if minutes >= cfg["failsafe_minutes"] and not self.failsafe_told:
                self.failsafe_told = True
                self.log(f"Failsafe for {minutes:.0f} min", "error")
                notify(self, "failsafe_long", **vals, minutes=f"{minutes:.0f}")
        else:
            self.failsafe_since, self.failsafe_told = None, False

    FOLLOW_TOLERANCE = 0.25  # RPM this far from what the speed gave before: the fans are not following it
    FOLLOW_AFTER = 4         # readings in a row before that is believed
    FOLLOW_STEADY = 60       # s a speed must have held before its RPM is judged
    RETAKE_EVERY = 300       # s between attempts to take the fans back while they do not follow

    def expected_rpm(self, speed):
        """What this speed made the fans turn before: the calibration's measurements, else what was seen
        while they followed. Between two known speeds, in proportion; outside them, None."""
        cal = [(r["pct"], r["rpm"]) for r in (self.calib or {}).get("results", []) if r.get("rpm")]
        pts = sorted(cal) if len(cal) >= 2 else sorted(self.rpm_at.items())
        gap = 100 if len(cal) >= 2 else 10  # a measured curve holds between its steps; odd points seen, only close by
        if speed in dict(pts):
            return dict(pts)[speed]
        for (p0, r0), (p1, r1) in zip(pts, pts[1:], strict=False):
            if p0 < speed < p1 and p1 - p0 <= gap:
                return r0 + (r1 - r0) * (speed - p0) / (p1 - p0)
        return None

    def check_follow(self, speed, fans, now, vals):
        """Fans that do not turn as fast as this speed made them turn before, at a steady speed: the BMC has
        taken them back (some iDRACs do while the host starts) or ignores the commands sent every cycle.
        Then the fans are taken back every RETAKE_EVERY seconds until they follow. The BMC has them in
        the meantime, which is safe, so this is no failsafe. speed: the one the reading answers to, or
        None. Called with self.lock held."""
        rpm = fan_avg(fans, "rpm", whole=False)
        if speed is None or not rpm or not self.driver.exact_speed:
            self.follow_speed = None
            return
        if speed != self.follow_speed:
            self.follow_speed, self.follow_since, self.follow_strikes = speed, now, 0
            return
        if now - self.follow_since < self.FOLLOW_STEADY:
            return
        expected = self.expected_rpm(speed)
        if expected is None:
            if not self.not_following:  # the first time at this speed: what it gives
                self.rpm_at[speed] = round(rpm)
            return
        if abs(rpm - expected) <= expected * self.FOLLOW_TOLERANCE:
            self.follow_strikes, self.follow_ok_at = 0, now
            if speed in self.rpm_at:  # fans age; follow them slowly
                self.rpm_at[speed] = round(0.9 * self.rpm_at[speed] + 0.1 * rpm)
            if self.not_following:
                self.not_following = False
                self.log("The fans follow Fan Control again")
                notify(self, "recovered", **vals)
            return
        self.follow_strikes += 1
        if self.follow_strikes >= self.FOLLOW_AFTER and not self.not_following:
            self.not_following = True
            self.log(f"Fans at {rpm:.0f} RPM where {speed}% gave {expected:.0f} RPM before: the BMC seems to have "
                     "taken them back; taking them again", "error")
            notify(self, "ignored", **vals)
        if self.not_following and now - self.retook >= self.RETAKE_EVERY and not self.retakes:
            self.retakes = [now]

    def check_fan_floors(self, fans, speed):
        """A fan under its BMC minimum at the speed last sent: the BMC logs it, lights the amber LED and
        may run every fan at full speed. From then on this server's fans stay 5 % above that speed,
        until Fan Control restarts. Called with self.lock held."""
        low = next((f for f in fans if f.get("floor") and f["rpm"] is not None and f["rpm"] < f["floor"]), None)
        if low is None or speed is None or speed + 5 <= self.rpm_floor:
            return
        self.rpm_floor = min(60, speed + 5)
        self.log(f"{low['name']} turned {low['rpm']} RPM at {speed}%, under its BMC minimum of {low['floor']:.0f} RPM; "
                 f"the fans stay at {self.rpm_floor}% or more", "warn")

    def check_fans(self, fans, vals):
        """A fan that stops while the others spin. Two readings in a row, so one odd value is ignored.
        Called with self.lock held."""
        self.spun |= {f["name"] for f in fans if (f["rpm"] or 0) >= 300 or (f["pct"] or 0) > 0}
        # A fan at 0 that has not turned since the start is an empty bay or a blank, which some BMCs list:
        # never a failsafe for good. One the BMC itself reports as failed still counts.
        flagged = {f["name"] for f in fans if f.get("ok", True) is False}
        stopped = set(failed_fans(fans))
        now_failed = {n for n in stopped if n in self.spun or n in flagged}
        for name in sorted(stopped - now_failed - self.idle_told):
            self.idle_told.add(name)
            self.log(f"Fan {name} reads 0 and has not turned since Fan Control started: taken as an empty bay", "warn")
        self.fan_strikes = {n: self.fan_strikes.get(n, 0) + 1 for n in now_failed}
        for f in fans:
            if self.fan_strikes.get(f["name"], 0) >= 2 and f["name"] not in self.fans_failed:
                self.fans_failed.add(f["name"])
                reading = f"{f['rpm']} rpm" if f["rpm"] is not None else f"{f['pct']}%" if f["pct"] is not None else "a fault"
                self.log(f"Fan {f['name']} failed: {reading}", "error")
                notify(self, "fan_failed", **vals, fan=f["name"], rpm=reading)
        for name in self.fans_failed - now_failed:
            self.fans_failed.discard(name)
            self.log(f"Fan {name} is spinning again")

    def check_response(self, effective, speed, fans, vals):
        """A BMC that accepts fan commands but ignores them (after a reset, or firmware that locks its
        fans) leaves the server on a speed nobody chose. After the speed moves by 20 points or more,
        the average RPM has to move the same way by 10 % within 30 s. Called with self.lock held."""
        rpm = fan_avg(fans, "rpm", whole=False)
        if effective != "manual" or speed is None or not rpm:
            self.last_manual = self.probe = None
            return
        now = time.time()
        if self.last_manual and abs(speed - self.last_manual[0]) >= 20:
            self.probe = {"t": now, "to": speed, "dir": 1 if speed > self.last_manual[0] else -1, "rpm": self.last_manual[1]}
        elif self.probe and abs(speed - self.probe["to"]) > 10:
            self.probe = None  # the speed moved on before the check was due
        self.last_manual = (speed, rpm)
        if not self.probe or now - self.probe["t"] < 30:
            return
        followed = (rpm - self.probe["rpm"]) / self.probe["rpm"] * self.probe["dir"] >= 0.10
        self.probe = None
        if not followed and not self.ignored:
            self.ignored, self.ignored_at = True, now
            self.log(f"The BMC did not follow the fans to {speed}%: their RPM stayed near {rpm:.0f}", "error")
            notify(self, "ignored", **vals)
        elif followed and self.ignored:
            self.ignored = False
            self.log("The BMC follows fan commands again")

    def record(self, point):
        """Add a reading to the 3-hour history and, every 5 minutes, an average to the 7-day one.
        Called with self.lock held."""
        self.history.append(point)
        self.bucket.append(point)
        if point["t"] - self.bucket[0]["t"] >= LONG_BUCKET:
            self.long.append(aggregate(self.bucket))
            self.bucket = []

    def release(self, timeout=None):
        """Hand the fans back to the BMC: on shutdown, removal, a change of driver or a stalled loop.
        timeout: how long to wait for a loop busy sending a command. Past it the command goes anyway,
        since a stuck loop must not keep the fans in manual."""
        if not self.control:
            return True
        locked = self.cmd_lock.acquire(timeout=-1 if timeout is None else timeout)
        try:
            self.driver.set_auto()
            return True
        except DriverError as e:
            self.log(f"Could not hand the fans back to the BMC: {e}", "error")
            return False
        finally:
            if locked:
                self.cmd_lock.release()

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name=self.id)
        self.thread.start()

    def run(self):
        restored = self.load_history()
        where = "simulated data" if self.driver.kind == "demo" else self.host or "local BMC"
        self.log(f"Started: {self.driver.label}, {where}, every {INTERVAL}s" + (", history restored" if restored else ""))
        if self.driver.kind == "demo" and not restored:
            self.demo_backfill()
        notify(self, "started")
        while not self.stop.is_set():
            self.tick = time.time()
            try:
                self.cycle()
            except Exception as e:  # never let a bug kill the loop and leave fans pinned low
                self.log(f"Controller error: {e!r}; handing fans back to the BMC", "error")
                notify(self, "controller_error", error=repr(e))
                self.release()
            if time.time() - self.saved > SAVE_EVERY:
                self.save_history()
            self.wake.wait(self.CALIB_EVERY if self.calibrating() else INTERVAL)
            self.wake.clear()

    def info(self):
        """Server description for the browser. Never includes the password."""
        d = self.driver
        return {"id": self.id, "name": self.name, "host": self.host, "driver": d.kind, "driver_label": d.label,
                "vendor": d.vendor, "control": d.control, "pcie": d.pcie, "experimental": d.experimental,
                "monitor_reason": d.monitor_reason, "source": self.cfg.get("source", "dashboard")}

    def summary(self):
        now = time.time()
        with self.lock:
            s = dict(self.state)
            hour = [p for p in self.history if p["t"] > now - 3600]
        step = max(1, len(hour) // 60)
        fans = (s["sensors"] or {}).get("fans", [])
        settings = self.settings()
        return {**self.info(), "model": s["model"], "cpu_temp": s["cpu_temp"], "effective": s["effective"],
                "mode": settings["mode"], "failsafe_temp": settings["failsafe_temp"],
                "applied_speed": s["applied_speed"], "failsafe": s["failsafe"], "error": s["error"],
                "dry_run": s["dry_run"], "reason": s["reason"], "following": s.get("following"),
                "power": s["power"], "updated": s["updated"],
                "watts": (s["sensors"] or {}).get("watts"), "inlet": (s["sensors"] or {}).get("inlet"),
                "fan_pct": fan_avg(fans, "pct"), "fan_rpm": fan_avg(fans, "rpm"),
                "spark": [[p["t"], p["cpu"], p["speed"]] for p in hour[::step]]}


def stalled(servers):
    """Servers whose control loop died or stopped turning: their fans sit at the last speed set."""
    now = time.time()
    return [s.id for s in list(servers) if s.thread and not s.stop.is_set()
            and (not s.thread.is_alive() or now - s.tick > STALL_SECONDS)]


def fan_avg(fans, key, whole=True):
    """Average of one fan reading ("rpm" or "pct") over the fans that report it, or None."""
    vals = [f[key] for f in fans if f.get(key) is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return round(avg) if whole else avg


def release_all(servers, deadline=40):
    """Stop every loop, then hand every server's fans back at the same time: one slow BMC must not
    use up the time the others need before Docker gives up waiting. Returns the ids not released."""
    servers = list(servers)
    for s in servers:  # first, so no loop can send manual control again after its release
        s.stop.set()
        s.wake.set()
    done = {}
    threads = [threading.Thread(target=lambda s=s: done.__setitem__(s.id, s.release(timeout=5)), daemon=True)
               for s in servers]
    for t in threads:
        t.start()
    end = time.time() + deadline
    for t in threads:
        t.join(max(0, end - time.time()))
    return [s.id for s in servers if not done.get(s.id)]


# ---------------------------------------------------------------- server registry

SERVERS_FILE = DATA_DIR / "servers.json"
SERVERS = {}
registry_lock = threading.Lock()


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "server"


def unique_id(name, taken):
    sid = base = slug(name)
    n = 2
    while sid in taken:
        sid, n = f"{base}-{n}", n + 1
    return sid


def env_servers(env=os.environ):
    """Servers declared in the environment: SERVER_1_HOST, SERVER_1_DRIVER, ... for each server.
    1.x names still work: IDRAC_HOST/... for a single server and IDRAC_1_HOST/... for several.
    The driver defaults to Dell (what 1.x supported), and to demo for HOST=demo."""
    specs = [("IDRAC_", True)] if env.get("IDRAC_HOST") else []
    for family in ("SERVER", "IDRAC"):
        numbered = sorted(int(m.group(1)) for k in env if (m := re.fullmatch(family + r"_(\d+)_HOST", k)))
        specs += [(f"{family}_{n}_", False) for n in numbered]
    out, taken = [], set()
    for prefix, legacy in specs:
        host = env.get(f"{prefix}HOST", "")
        driver = env.get(f"{prefix}DRIVER") or ("demo" if host == "demo" else "dell")
        if driver not in DRIVERS:
            log.warning("%sDRIVER=%s is unknown; use one of %s", prefix, driver, ", ".join(DRIVERS))
            continue
        name = env.get(f"{prefix}NAME") or ("Demo server" if driver == "demo" else host)
        sid = unique_id(name, taken)
        taken.add(sid)
        out.append({"id": sid, "name": name, "driver": driver, "host": "" if driver == "demo" else host,
                    "username": env.get(f"{prefix}USERNAME", "root"),
                    "password": env.get(f"{prefix}PASSWORD", "calvin"),
                    "verify_tls": env.get(f"{prefix}VERIFY_TLS", "").lower() in ("1", "true", "yes"),
                    "source": "environment", "legacy": legacy})
    return out


def dashboard_servers():
    rows = read_json(SERVERS_FILE, [], "servers.json")
    return [s for s in rows if isinstance(s, dict) and s.get("driver") in DRIVERS] if isinstance(rows, list) else []


def save_dashboard_servers():
    rows = [{k: v for k, v in s.cfg.items() if k != "source"}
            for s in SERVERS.values() if s.cfg.get("source") == "dashboard"]
    write_json(SERVERS_FILE, rows, private=True)  # holds BMC passwords


def start(cfg):
    srv = Server(cfg)
    SERVERS[srv.id] = srv
    srv.start()
    return srv


def stop(srv, forget=False):
    """Stop the control loop and hand the fans back. A loop busy reading a slow BMC is not waited
    for: cmd_lock and the stop flag guarantee it sends no fan command after release(), so it can
    never race the replacement loop for the same BMC."""
    srv.stop.set()
    srv.wake.set()
    if srv.thread and srv.thread is not threading.current_thread():
        srv.thread.join(timeout=2)
    srv.release()
    srv.save_history()
    SERVERS.pop(srv.id, None)
    if forget:
        for f in (srv.settings_file, srv.history_file, srv.smart_file, srv.calib_file, srv.handed_file, srv.changes_file):
            try:
                f.unlink()
            except OSError:
                pass


def validate_server(new, current=None):
    """A server added or edited in the dashboard. On edit, an empty password keeps the stored one."""
    cfg = dict(current or {"source": "dashboard"})
    name = str(new.get("name", cfg.get("name", ""))).strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("give the server a name of 1 to 60 characters")
    driver = new.get("driver", cfg.get("driver"))
    if driver not in DRIVERS:
        raise ValueError("pick a server type")
    host = str(new.get("host", cfg.get("host", ""))).strip()
    if DRIVERS[driver].needs_host:
        m = re.fullmatch(r"(.+?)(?::(\d{1,5}))?", host)
        name_part, port = (m.group(1), m.group(2)) if m and not (host.count(":") > 1 and not host.startswith("[")) else (host, None)
        redfish = issubclass(DRIVERS[driver], RedfishDriver)
        if not HOST_RE.fullmatch(name_part) or (port and (not redfish or not 0 < int(port) < 65536)):
            raise ValueError("the address must be a host name or IP" + (", optionally with :port" if redfish else ""))
        if host == "local" and redfish:
            raise ValueError("local access only works for IPMI servers")
    else:
        host = ""
    username = str(new.get("username", cfg.get("username", ""))).strip()
    if DRIVERS[driver].needs_host and not 1 <= len(username) <= 64:
        raise ValueError("enter the BMC user name")
    password = new.get("password")
    if password:
        if len(str(password)) > 128:
            raise ValueError("the password is limited to 128 characters")
        cfg["password"] = str(password)
    elif DRIVERS[driver].needs_host and not cfg.get("password"):
        raise ValueError("enter the BMC password")
    verify = new.get("verify_tls", cfg.get("verify_tls", False))
    if type(verify) is not bool:
        raise ValueError("verify_tls must be true or false")
    cfg.update(name=name, driver=driver, host=host, username=username, verify_tls=verify)
    return cfg


def load_registry():
    taken = set()
    for cfg in env_servers() + dashboard_servers():
        if cfg["id"] in taken:  # an environment server and a saved one with the same name
            cfg["id"] = unique_id(cfg["id"], taken)
        taken.add(cfg["id"])
        cfg.setdefault("source", "dashboard")
        SERVERS[cfg["id"]] = Server(cfg)
