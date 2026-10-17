# Changelog

Versions follow [semantic versioning](https://semver.org): a major version (`:2`) never removes a setting
or changes what an existing one does, so pinning the image to it is safe.

## [2.9.8] - 2026-10-17

Operations, from an independent review:

- **fan-guard.sh**:
  - reads the compose file's `SERVER_1_...` servers (it only knew `GUARD_SERVERS` and the 1.x `IDRAC_HOST`,
    so a current setup was guarded by nothing, in silence), and fails with a message when it has none;
  - reads the `.env` as `KEY=value` lines instead of running it as shell code as root (a password with a
    space or a `;` ran as a command), and refuses one the group or others can write (0646 passed);
  - times Docker out after 15 s and runs one at a time, so a hung dockerd no longer stacks stuck guards;
  - acts on a running but *unhealthy* container only after 5 minutes (a slow health check is not a dead loop).
- **The example compose file** adds `init: true` (no zombie ssh processes) and log rotation, and Fan Control
  logs an error while the password is still the example's `change-me`.
- **Backups carry the fan calibrations**, and an import without passwords says how to finish it.
- CI checks that `docker stop` really handed the fans back, and the browser tests open the Automation page.
- README: the `/data` table lists every file; the watchdog's real timing.

## [2.9.7] - 2026-10-17

Control quality, from an independent review with simulations:

- **Smart mode under quiet hours no longer boosts to 100 % and drops back to the cap every minute** (about 60
  times an hour in a simulation): a boost is one step up, lifts the cap for a while and fades slowly.
- **Fixed and curve modes let a cap give way within 8 °C of the failsafe**, as smart mode does: a cap the load
  could not live with tripped the failsafe 28 times in 4 hours in a simulation, with two alerts each time.
- **A smart target closer than 8 °C to the failsafe is aimed 8 °C under it**, as for every other sensor; closer,
  it boosted every few minutes and never settled.
- **Smart mode no longer slows the fans while the temperature is above its target**, learns a load only when it
  held for the whole steady minute, never winds its trim past 100 %, and no longer shows "None cap 100%".
- **The hardware's floors (Supermicro's 25 %, a fan's BMC minimum) are smart mode's floor too**, so it learns
  only speeds that were sent; while the fans do not follow it adjusts nothing.
- **An empty fan bay no longer pushes the minimum speed up to 60 %.**

## [2.9.6] - 2026-10-17

Security, from an independent review:

- **Without a password, DNS rebinding no longer gives a web page admin access**: the dashboard then only
  answers addresses, `localhost`, local names and the new `ALLOWED_HOSTS`.
- **Reading a BMC's event log** is one read at a time per server and kept for a minute: a viewer could crowd
  the BMC with sessions until the control loop's readings failed.
- **No connection lives longer than 120 s**, so slow clients cannot hold every request slot.
- **BMC timeouts no longer put the BMC address and user name in errors** that viewers, widgets and alerts see.
- **Diagnostics hide the BMC address in every answer and form**, the BMC user, and Dell's service tag.
- **A Redfish path from the BMC must stay on that host**; scan ranges written as IPv4-mapped IPv6 are refused;
  alert channels refuse link-local addresses however they are written.
- **Server names cannot hold quotes or control characters** (they reached a generated Home Assistant snippet);
  widgets never show a BMC address used as a name.
- **Deeply nested JSON is a 400**, and a 1 MB backup body is only read from an admin session.
- Servers named "test", "scan" or "detect" get an id that does not collide with other routes.

## [2.9.5] - 2026-10-17

From a review of the control loop by independent reviewers:

- **Turning dry run on while the BMC was busy left the fans in manual for the whole dry run**; the hand-over
  is now tried every cycle until it works.
- **`docker stop` during a slow command could leave the fans in manual**: a release that did not wait for
  the loop hands the fans back again once the loop's last command is done.
- **A failed fan that then vanished from the readings cleared its own fault** ("spinning again"); a fan that
  turned and is no longer listed now trips the failsafe like a sensor that stops reporting.
- **The ignored-command check compared with a reading one cycle too old**, so a quick 30 → 60 → 30 % looked
  ignored and tripped a 10-minute failsafe. It is also off during a calibration.
- **Supermicro: a hand-back before any take-over replaced the user's fan mode (HeavyIO) with Optimal**, and a
  failed read of the mode was saved as the user's choice.
- **A calibration no longer measures readings taken while the BMC had the fans.**
- **The RPM each speed should give is not learned right after a take-back** (the BMC may still have the fans,
  as after a power-on), nor out of order with the speeds around it.
- **Quiet hours and schedule caps no longer cancel the easing out of a failsafe** in fixed and curve modes.
- **The watchdog runs on a monotonic clock** (an NTP step no longer hides or fakes a stall), and a slow BMC
  reading no longer counts towards a stall.
- **A controller error is logged and alerted once per error**, not every cycle; odd Redfish answers are failed
  readings instead of controller errors; a Redfish path from the BMC must stay on that host.
- **A fan under its BMC minimum needs two readings** before the floor rises, and a new minimum speed resets it.
- **iLO 4: a fan gone from the list keeps its PWM capped and released.**
- **While the fans do not follow, they are taken back without waiting for a steady speed** (smart mode).

## [2.9.4] - 2026-10-17

- **Smart mode learns nothing while the BMC has the fans** (they do not follow): the speed it saw then is not
  what held the target, and the learned map would have been wrong for good.
- **A calibration starts by taking hold of the fans properly**, so it measures Fan Control's fans.
- **Discord alerts rate-limited during an incident are sent again** after the wait Discord asks for (a
  webhook takes 5 requests in 2 s; several servers going down at once went past that and lost alerts).

## [2.9.3] - 2026-10-17

- **One failed BMC reading no longer hands the fans over and sends two alerts.** A busy iDRAC drops a reading
  now and then; that made the fans jump to the BMC's control and sent *unreachable* then *recovered*. Two
  failed readings in a row are needed now (30 s at the default interval); the last decision holds meanwhile.
- **A dashboard left open across an upgrade reloads itself** (when nothing is unsaved), instead of running
  the old page against the new server.
- The README says what no software can cover: a BMC that cannot be reached keeps the last manual speed.

## [2.9.2] - 2026-10-17

From a review of every way the BMC's real state can drift from what Fan Control believes (the family of #3):

- **Every take-over takes hold properly**, not only the start and a host power-on: the end of a failsafe, a
  hand-over or a dry run, a change from automatic and a refused command all let go and take hold again. A
  warm reboot of the host (no power-off, but a POST) shows up as one of those, or as fans not following.
- **iLO 4 unlocked re-sends its caps and Supermicro its Full mode at every take-over**, instead of up to 10
  minutes later: a restarted iLO forgets the caps, a Supermicro back from a power cycle its mode.
- **A power state the BMC cannot tell** ("Chassis Power is unknown" while it is busy) keeps the last one
  known, instead of counting as off: that handed the fans over and faked a power-on.
- **An empty fan bay no longer trips the failsafe for good**: a fan at 0 RPM that has not turned since Fan
  Control started is logged as an empty bay. One that turned and stops, or that the BMC reports as failed,
  still trips it.
- **The first command after a start no longer waits for the BMC's thresholds** (`ipmitool sensor` can take a
  minute, and a crash may have left the fans low); they are read with the second reading.
- **The overview flags fans that do not follow**, like a failsafe.
- **Quiet hours and the schedule say when the container's clock is not the browser's**, with the fix (`TZ`).
- Troubleshooting: what to do when the fans do not follow (two controllers fight), empty bays, the clock.

## [2.9.1] - 2026-10-17

- **Fans the BMC took back are noticed and taken again** ([#3](https://github.com/ST-DEVPT/rack-fan-control/issues/3)).
  An iDRAC 7 returned to its own control while its host started and then ignored the manual-mode command
  sent every cycle, for 30 hours, with the dashboard showing nothing wrong. Now:
  - control is taken back by letting go and taking hold again (on Dell, also looking for the fans afresh)
    when Fan Control starts, when the host powers on and twice more during its start-up, and when the BMC
    answers again after a silence;
  - at a steady speed, the fans must turn as fast as that speed made them turn before (the calibration, or
    what was seen while they followed). When they do not, the server page says so, the event log and the
    *fan commands ignored* alert tell, and control is taken back every 5 minutes until they follow;
  - `fanctl_fans_following` and a `FansNotFollowing` Prometheus rule;
  - falling back to setting Dell fans one by one is logged, and retried with all fans at every take-back,
    so a refusal during the host's start-up does not stick.

## [2.9.0] - 2026-10-16

- **Earlier settings**: the last 20 settings saved for each server, with when, who and what changed; any can
  be loaded back into the panel and applied.
- **The BMC's event log** (IPMI servers): how full it is and the last 25 entries, read when opened; an admin
  can clear it. A full log stops recording, and fans under their minimum land there.
- **How smart mode is doing**: over the last hours, how often the CPU was within 2 °C of the target, the
  average fan speed and how often the fans changed direction; the target is drawn on the history chart.
- **What the fans draw**, from the calibration: now, in kWh a month, and over 7 days with Fan Control against
  with the BMC.
- **Proxmox backup hook** on the Automation page: the BMC has the fans while `vzdump` jobs run.

## [2.8.1] - 2026-10-16

Safety, from a critical review of 2.6-2.8.

- **Each fan's BMC minimum RPM is read and respected.** Under it the BMC logs an event, lights the amber LED
  and may run every fan at full speed, and a minimum speed of 0 % or a calibration could get there. The fan
  list shows it; the calibration goes no slower once a fan is within 15 % of it; a fan that reads under it
  in normal control keeps the fans 5 % higher from then on; the minimum speed field warns from the
  calibration's measurements.
- **A control token can no longer weaken the protections**: the failsafe, the exhaust limit, the BMC
  thresholds and their margin, the minimum speed and Dell's PCIe cooling are changed by an admin only. The
  browser asked before such changes, but a script never did.

## [2.8.0] - 2026-10-16

- **Automation page and control tokens**: Home Assistant, n8n, cron or a script can change fan settings and
  hand the fans to the BMC with a Bearer token, and read what a read-only account sees. A control token never
  sees BMC accounts or touches servers, alerts, tokens or backups; its changes are logged with its name. The
  page fills in `curl` examples and a Home Assistant `rest_command` and sensor.
- **The curve can follow any temperature**: the hottest CPU (as before), the exhaust air, or a sensor such
  as a GPU or a disk bay. The CPU failsafe applies whichever it follows; a sensor that gives no reading hands
  the fans to the BMC.
- **The history chart marks what happened**: settings changes, hand-overs, calibrations, failsafes and errors
  as ticks along the top, listed in the tooltip.

## [2.7.1] - 2026-10-16

- **An alerts file with an empty section** (`"colors": null`, an event set to `null`), edited by hand, no
  longer stops every alert and the control loop's checks; the defaults fill in.
- **The dashboard's polling never stops for good** after one unexpected error, and the 24 h and 7 d charts
  no longer show an error while the dashboard is offline (the banner already says so).
- **Bursts of connections through a reverse proxy** wait in a deeper queue (64) instead of being refused.

## [2.7.0] - 2026-10-16

- **Fan calibration**: one button under the fan list steps the fans from 100 % down to 0 %, measures each
  fan's RPM and the power draw at every step, and draws the curve. It says below which speed the fans turn
  no slower, the lowest speed at which every fan kept turning, and what the fans cost in watts at full
  speed, and puts the suggested minimum speed into the settings with one click. Every protection stays on;
  when a fan stops at a low step, the steps above it still count.
- **Hand the fans to the BMC for a while**: 1, 4 or 12 hours (any time up to a week through the API), for
  maintenance or a stress test; control comes back on its own, also across a restart.
- **History as CSV**: a button next to the chart's time range downloads it.

## [2.6.2] - 2026-10-16

- **Dell: the BMC's warning thresholds are read again on servers that have one unreadable sensor** (an empty
  CPU socket, a PSU). `ipmitool sensor` lists every sensor and then exits with an error over that one; the
  whole list was thrown away and the log said the thresholds could not be read. When they really cannot be,
  the log now says why.
- **Errors say what happened**: when a reverse proxy answers instead of Fan Control (502, 503, 504), the
  message says so instead of a JSON parse error, and a request that never got an answer shows an error
  instead of nothing. Every button reports failures the same way.

## [2.6.1] - 2026-10-16

- **Minimum speed goes down to 0 %** (it stopped at 10 %): fixed, curve and smart modes can run the fans as
  slowly as the hardware allows. Lowering it still asks first, and a stopped fan still trips the failsafe.
- **A request that fails inside Fan Control answers 500 with the reason**, and the log gets the traceback.
  Before, the connection just closed, which a reverse proxy (Nginx Proxy Manager and the like) shows as an
  opaque 502 with the cause nowhere.
- **Refusals no longer reset the connection**: the request body is read before a 403, 415 or 400, so a proxy
  passes the refusal on instead of a 502. A server busy with 64 requests answers 503 instead of dropping
  the 65th. `Content-Type: application/json; charset=utf-8` is accepted.

## [2.6.0] - 2026-10-06

Engineering, from an independent review: what keeps the other five areas true over time.

- **Browser tests** run against the built image on every push: every page loads without a script error and
  passes axe (WCAG 2 AA), the curve is edited by field and by keyboard, a change that reduces cooling asks
  first, invalid values say so, Portuguese is complete on screen, the phone layout fits, a widget token reads
  widgets and nothing else, a viewer cannot change anything. They found and fixed: curve points losing the
  keyboard focus on every refresh in current Chromium, faded text under contrast, small buttons on touch
  screens, an unlabelled file field and scroll areas the keyboard could not reach.
- **CI**: ruff, a check that each page's scripts never declare a name twice, shellcheck, promtool on the
  alert rules, tests on Python 3.10 to 3.13, Docker's health check, `docker stop` handing the fans back, and a
  Trivy scan of the image. Actions are pinned by commit, the base image by digest, both kept current by
  Dependabot; images carry an SBOM and provenance; a tag without its CHANGELOG section is refused.
- **The image has no pip or setuptools** (the app installs nothing), and with them went the four HIGH
  vulnerabilities of the libraries they bundle.
- **Routes declare their access** (public, widget, viewer, admin) and one dispatcher checks it; a test walks
  the table. No route can be open because of where it sits.
- **Logging** through the standard library, with ISO-8601 times; `LOG_FORMAT=json` for log collectors.
- **Metrics**: counters for BMC errors, fan commands, refused commands, failsafe trips and alerts, the
  control loop's last tick, failed fans; and a file of Prometheus alert rules.
- **Backups** include what smart mode learned. Tests load data files as 1.x and 2.0 wrote them.
- `fan-guard.sh` is shellcheck-clean and tested in the suite. One shared `util.js` for every page.
- Quiet hours from a time to the same time now mean the whole day, as schedule profiles already did.

## [2.5.0] - 2026-10-05

The dashboard, for people who are not engineers and for phones, from an independent review.

- **The control panel puts the essentials first**: mode, failsafe, minimum speed and quiet hours. Exhaust
  and sensor limits, ramp-down, schedule, dry run and PCIe cooling sit under *More protection and
  smoothing*. Help texts say what happens ("the BMC takes the fans back").
- **Applying a change that reduces cooling asks first** and lists what is reduced: a lower minimum speed,
  a higher failsafe, a limit turned off, a curve that runs the fans slow when hot.
- **Out-of-range values are no longer dropped in silence**: the field turns red and says what it accepts.
- **The fan curve works by touch and keyboard**: every point is also a pair of fields, points can be added
  and removed with buttons, and a focused point moves with the arrow keys (Shift for steps of 5) or goes
  with Delete.
- **Phones**: a compact header that scrolls away (pages and servers each slide sideways), and 40 px touch
  targets on touch screens.
- **Contrast meets WCAG AA** in both themes (secondary text, the accent, alerts); the two themes come from
  one set of colour tokens.
- **Offline is visible**: a banner says when the dashboard cannot be reached and since when, and the
  readings below are dimmed. Polling pauses in hidden tabs and never piles up requests.
- The status dots in the sidebar have text for screen readers, and "hot" means the same everywhere: within
  5 °C of that server's failsafe. The BMC error box is no longer re-announced every 5 seconds.
- The chart keeps its crosshair under the pointer across refreshes; resizing redraws once.
- Leaving the Alerts page with unsaved changes asks first, like the server page.
- Adding a server leads with the network scan; an empty name becomes the address, never the example.
- Navigation: Overview, then servers, then settings (Alerts, Prometheus, Grafana, Homarr, Backup).
- Portuguese: sentences split by links or code are translated whole; the one-word fragments that used to
  translate any matching text anywhere are gone, and a test keeps it that way.

## [2.4.0] - 2026-10-05

Security, from an independent review.

- **X-Forwarded-For** is read from the right and only believed from trusted proxies (`TRUSTED_PROXIES`,
  by default loopback and Docker's networks). Before, the leftmost address, which the client writes, was
  used, so a client could dodge the sign-in limit by inventing a new address for every guess.
- **Sessions can be revoked**: signing out ends that session on the server, even if the cookie was copied,
  and **Sign out everywhere** on the Backup page ends them all. Sessions from before this version end once.
- **BMC certificates are pinned on first use**: a Redfish BMC that suddenly shows another certificate is
  refused instead of being sent the password. A replaced certificate is accepted from the server's page.
- At most 64 requests at once; the table of failed sign-ins is bounded.
- Alert channels refuse redirects to other hosts and link-local addresses.
- Viewers can no longer read BMC account names; diagnostics leave the BMC address out of events too.
- The BMC error box is announced to screen readers only when it changes.

## [2.3.0] - 2026-10-05

Hardware safety: when the controller does not know, the BMC decides.

- **No CPU reading means automatic.** Another sensor (inlet air, a DIMM) no longer stands in for the CPU.
- **An unreadable BMC gets the fans back** as soon as it takes a command, instead of keeping the last
  manual speed with nobody watching.
- **Lost or faulty sensors and failed fans trip the failsafe**, and so does a BMC that ignored a fan
  command (manual control is tried again after 10 minutes).
- **The failsafe holds for at least 5 minutes**, and manual control resumes from 50 %, easing down, so
  fans no longer flap between the BMC and the controller.
- **Minimum speed is 20 % by default and 10 % at the lowest**; the failsafe goes up to 90 °C. Older
  settings outside these limits are brought inside them, never refused.
- **Supermicro**: the fan mode the BMC had (HeavyIO on a GPU box, say) is read once, kept on disk and
  restored, instead of always Optimal; never below 25 %; X9 is no longer claimed (it needs other commands).
- **HPE iLO 4 unlocked**: a failed release is now reported instead of silently leaving a low cap.
- **docker stop releases every server at once** after stopping every loop, and the example compose file
  allows 60 s. The watchdog no longer waits on a stuck loop's lock.
- BMC warning thresholds that could not be read are retried every 10 minutes, and the dashboard says so.
- **Smart mode** takes the fans' own power out of the load signal and carries the learned map at most
  15 % past its last point. It was re-checked against a harsher simulation: readings every 15 s and 10 s
  old, fans that stall below 8 %, heat transfer that saturates with airflow, two CPUs sharing air.
- The control loop is split into read, protect, choose speed, command and report.

## [2.2.1] - 2026-10-05

Fixes from an independent review of 2.2.0.

- **fan-guard** found the container by a fixed name, `idrac_fan_web`, which the example compose file does
  not use: with it, the guard took a running Fan Control for dead and handed the fans to the BMC every
  minute. It now finds the container by its image, leaves a removed container alone, also acts when Docker
  is down, and covers Supermicro and HPE iLO 4 unlocked as well as Dell. It refuses an `.env` others can
  write and keeps its state in `/run`.
- **Data files are written atomically and durably**: a unique temporary file, flushed to disk, renamed,
  and the folder flushed too. Two writers can no longer collide on one temporary file, and files with
  passwords or tokens are private from the first byte.
- **A corrupt or invalid settings file now means automatic fans**, logged once, instead of falling back
  to the default curve. A corrupt `servers.json`, `alerts.json` or `tokens.json` is kept aside as
  `<name>.corrupt-<time>` instead of being overwritten by the next save.
- A negative or malformed `Content-Length` is refused before anything is read.
- The server page and history are copied under the lock and sent after it, so a slow browser can no
  longer hold up a control loop. Events are logged under the lock.
- A backup without secrets no longer contains the ntfy, Gotify or webhook addresses and tokens.

## [2.2.0] - 2026-10-05

- **Watchdog.** A control loop that stops turning for 3 minutes hands every fan back to its BMC and
  restarts the process. The health check follows the control loops (`/livez`), not the BMCs.
  `scripts/fan-guard.sh`, run from the host's cron, hands Dell fans back to the iDRAC when the container
  itself is gone.
- **Fans are checked.** New alerts for a fan that stops while the others spin, a BMC whose fans do not
  follow a 20-point speed change within 30 s, a failsafe that lasts longer than a set time, and inlet air
  above a warning temperature.
- **Alerts on ntfy, Gotify and any webhook**, besides Discord. The page is now called Alerts.
- **Tokens in the dashboard.** Widget and metrics tokens are created and revoked on the Homarr and
  Prometheus pages, shown once and stored as a hash.
- **Schedule.** Up to 8 profiles by day and time, each with a speed cap and/or a smart mode target.
- **Diagnostics.** A server page downloads what its BMC answers, without passwords or addresses, for bug
  reports about untested hardware.
- **Smart mode metrics** in Prometheus, and two Grafana panels that show why it chose each speed.
- The sidebar shows when a newer release is out (`UPDATE_CHECK=false` turns it off).
- The docs pin the image to its major version, `:2`.
- A test fails on any page text without a Portuguese translation; the 40 it found are translated.
- Code: `metrics.py`, `widgets.py`, `backup.py`, `tokens.py` and `updates.py` come out of `web.py`;
  `editor.js` out of `app.js`.

## [2.1.0] - 2026-10-05

- **Smart mode, rebuilt.** It learns the fan speed each heat load needs on your server and goes straight
  there when the load changes. It judges temperatures where their trend puts them 40 s on, so heat is met
  before it arrives. It keeps a PI trim on top, which the learned map slowly takes over. Heat load is power
  draw over the room between the inlet air and the target, so a warm day or a new target needs no
  relearning.
- Smart mode rides out bursts: fans fall only after lower demand has lasted the ramp-down delay. In a
  simulated job that runs 30 s of every 90 s, the fans travel less than half as far.
- Smart mode boosts the fans when anything heads for its trip point, and quiet hours give way gradually
  near a trip point. In simulation the failsafe no longer trips under a quiet-hours cap that is too low
  for the load.
- Smart mode no longer chases the inlet air temperature, which no fan speed can lower.
- The server page shows what smart mode is doing (trend, learned speed) and draws what it has learned,
  with a button to forget it. The map is kept in `smart-<server>.json` in the data folder.
- The event log records speed changes from 5 % on, so slow ramps no longer push everything else out.
- Portuguese: text that wraps over several lines in the page is translated too.
- **Homarr**: choose what widgets may show (status, CPU, fans, power, inlet, exhaust, chart, model). The
  embed token now reads only those, through the new `/api/widget`; it no longer opens `/api/state` or
  `/api/history`, which carried the BMC address, settings and events.
- **Native Homarr 2.0 widget**: a Custom Widget to import, which Homarr fetches itself with the token as a
  Bearer credential. It has options for the server or the whole rack, °C or °F, the chart and the air.
- The iFrame widget fits any frame: the chart gives way first, then the footer, instead of being cut. It
  can show the whole rack, and only the fields you pick.
- `/healthz` answers `503` when a BMC stops answering, not only when the controller stalls, so a Homarr
  app tile goes red when it should.
- Fixed: opening the Prometheus, Grafana or Homarr page directly could leave it empty.

## [2.0.1] - 2026-10-05

- Redfish: follow redirects that stay on the same BMC. HPE iLO 4 answers `/redfish/v1/Chassis` with a
  308 to the trailing-slash path, which showed as "HTTP 308 Moved Permanently". Redirects to any other
  host are still refused, so the password never leaves the BMC.

## [2.0.0] - 2026-10-05

- Supports more than Dell: Supermicro (fan control, experimental), HPE iLO 4 with unlocked firmware
  (fan caps over SSH, experimental), any Redfish BMC such as HPE iLO 4/5/6 and Lenovo XCC, and any IPMI
  BMC (monitoring only).
- Servers are added, tested, edited and removed in the dashboard. Environment variables still work and
  gain `IDRAC_<n>_DRIVER` and `IDRAC_<n>_VERIFY_TLS`.
- Redesign: sidebar with every server, an overview of the whole rack, a page per server and a setup
  assistant with a connection test.
- Prometheus, Grafana and Homarr each have their own page: generated scrape config and live metrics,
  dashboard download with import steps, and a widget address builder with a live preview.
- "Dell" mode is now "Automatic"; saved settings are converted.
- **Renamed for what it has become.** The project is "Fan Control" (repository `rack-fan-control`).
  Metrics are `fanctl_*` instead of `idrac_*` (`idrac_dell_control` is now `fanctl_bmc_control`), with a
  `driver` label and a new `fanctl_fan_percent`; re-import the Grafana dashboard. Servers in the environment
  use `SERVER_<n>_*`; the 1.x `IDRAC_*` variables keep working.
- Redfish requests refuse redirects, so credentials never follow a redirect to another host.
- **Smart mode**: a PI controller that finds the slowest speed holding the CPU at a target while keeping
  the exhaust air and every sensor with a BMC warning threshold clear of their limits.
- **Protection beyond the CPU**: the BMC also takes over on hot exhaust air and when any sensor (PCIe cards,
  disks, DIMMs, RAID controller) nears the warning threshold its own BMC defines. Minimum fan speed. Dry run.
- **Quiet hours**: cap the fan speed between two times of day (`TZ` sets the time zone).
- **History for 24 hours and 7 days**, as 5-minute averages kept on disk.
- Curve presets (quiet, balanced, cool, storage) and copying the curve of another server.
- **Network discovery**: scan a private range for BMCs answering Redfish (HTTPS) or IPMI (UDP 623), with the
  suggested type for each, then add them in a click. **Detect** asks a single BMC for its vendor and firmware.
- **Read-only account** with `VIEW_PASSWORD`; changes are attributed in the event log; failed sign-ins are
  limited per address (`TRUST_PROXY` to believe `X-Forwarded-For`).
- **Backup**: export and import servers, settings and Discord, with passwords only when asked for.
- Portuguese and °F, chosen per browser.
- Supermicro switches to Full fan mode once instead of on every cycle. A server being removed or edited can
  no longer receive a fan command after it was handed back to the BMC.
- Code split into the `fanctl` package; tests use `unittest` (`python -m unittest`); CI starts the image and
  checks it serves pages and a demo server before publishing.

## [1.2.0] - 2026-10-04

- Discord status reports: temperatures, fan speed, power, min/avg/max, time in Dell mode, trend lines
  and recent events for every server, on a schedule. By default one message is edited in place.

## [1.1.0] - 2026-10-04

- Several servers in one dashboard (`IDRAC_1_HOST`, `IDRAC_2_HOST`, ...), with tabs.
- Ramp-down delay: fans speed up at once and slow down only after demand stays lower.
- Failsafe hysteresis: manual control resumes 3 °C below the failsafe temperature.
- History and events survive restarts (`/data/history-<server>.json`).
- Discord alerts, configured in the dashboard: webhook, bot name, avatar, footer, colours, mentions,
  cooldown, nine events with editable title and message templates, live preview and test sends.
- Prometheus `/metrics` (`METRICS_TOKEN`) and a Grafana dashboard in `docs/grafana`.
- Read-only `/embed` widget for Homarr and other dashboards (`EMBED_TOKEN`).
- Per-fan commands for 11th-generation servers that refuse the "all fans" selector.
- Fonts bundled: the dashboard makes no third-party requests.
- Fixed a race between saving settings and the control loop that could briefly hand the fans to Dell mode.
- Security: strict Content-Security-Policy, origin check on POST, request size and time limits,
  container runs as uid 1000 with a read-only root filesystem.

## [1.0.0] - 2026-10-04

- First release: Dell, fixed and curve modes, failsafe, history, sign-in page.
