"""Sign-in sessions and the HTTP server."""

import csv
import hashlib
import hmac
import io
import json
import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from . import config, tokens, updates
from .alerts import (ALERT_KINDS, CHANNELS, SAMPLE, alert_config, build_payload, build_report, post_webhook,
                     public_alert_config, save_alerts, send_channel, validate_alerts, webhook_of)
from .backup import export_config, import_config
from .config import DATA_DIR, HISTORY_SECONDS, INTERVAL, VERSION, WEB
from .control import validate_settings
from .drivers import DRIVERS, DriverError, bare_host, detect, forget_pin, scan
from .metrics import metrics
from .server import SERVERS, registry_lock, save_dashboard_servers, stalled, start, stop, unique_id, validate_server
from .widgets import WIDGET_FIELDS, homarr_widget, save_widget_fields, widget_fields, widget_view

KEY = b""  # set by app.main() once the data folder is known to be writable
# what stands between a mistake and a hot server: only an admin session changes these, never a control token
PROTECTIONS = ("failsafe_temp", "exhaust_limit", "bmc_thresholds", "threshold_margin", "min_speed", "pcie_cooling")
log = logging.getLogger("fanctl.web")

# Sessions and sign-in throttling live in sessions.py; the names stay importable from here.
from .sessions import (SESSION_LONG, SESSION_SHORT, client_address, failed, make_token, revoke,  # noqa: E402,F401
                       revoke_all, session_key, throttled, valid_token)


def same_secret(given, expected):
    return bool(expected) and hmac.compare_digest(str(given).encode(), expected.encode())


# ---------------------------------------------------------------- HTTP

STATIC = {  # allowlist: nothing outside it is ever read from disk
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "util.js": "text/javascript; charset=utf-8",
    "editor.js": "text/javascript; charset=utf-8",
    "login.js": "text/javascript; charset=utf-8",
    "embed.js": "text/javascript; charset=utf-8",
    "alerts.js": "text/javascript; charset=utf-8",
    "integrations.js": "text/javascript; charset=utf-8",
    "i18n.js": "text/javascript; charset=utf-8",
    "grafana.json": "application/json",
    "prometheus-alerts.yml": "text/yaml; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "fonts/archivo.woff2": "font/woff2",
    "fonts/plex-mono-400.woff2": "font/woff2",
    "fonts/plex-mono-500.woff2": "font/woff2",
}
PAGES = {"/": "index.html", "/login": "login.html", "/embed": "embed.html"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
       "font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'")


# ---------------------------------------------------------------- diagnostics

def diagnostics(srv):
    """Everything needed to look into a server's behaviour without access to it: the raw BMC
    answers, what was made of them, settings and recent events. No password, no BMC address."""
    with srv.lock:
        state = {k: v for k, v in srv.state.items() if k != "smart_map"}
        events = list(srv.events)[:50]
    try:
        raw = srv.driver.diagnose()
    except Exception as e:  # a diagnostics bug must still return what it has
        raw = {"error": repr(e)}
    if srv.host:  # "Started: ..., 10.0.0.5, every 15s", "Cannot read the BMC: 10.0.0.5 ...", raw answers
        forms = sorted({srv.host, bare_host(srv.host), srv.cfg.get("username") or srv.host}, key=len, reverse=True)

        def hide(value):
            text = json.dumps(value)
            for f in forms:
                text = text.replace(json.dumps(f)[1:-1], "<bmc>")
            return json.loads(text)
        events, state, raw = hide(events), hide(state), hide(raw)
    return {"format": "fan-control-diagnostics", "app": VERSION, "created": int(time.time()),
            "driver": srv.driver.kind, "driver_label": srv.driver.label, "model": srv.driver.model,
            "interval": INTERVAL, "settings": srv.settings(), "state": state, "events": events, "raw": raw}


# ---------------------------------------------------------------- routes
#
# Every route says who may call it, and one dispatcher checks that before the route runs, so no
# route is ever open because of where it sits in a chain of ifs:
#   public  anyone (sign-in page, health checks, static files)
#   widget  a session, or a widget token as ?token= or a Bearer header
#   viewer  any session, admin or read-only, or a control token as a Bearer header
#   control an admin session, or a control token: the changes automations may make
#   admin   an admin session
# /metrics checks its own token, since it has a rule of its own (open when nothing is protected).

ROUTES = []  # (method, compiled path pattern, access, function)


def route(method, pattern, access):
    def register(fn):
        ROUTES.append((method, re.compile(pattern + r"\Z"), access, fn))
        return fn
    return register


def server_or_404(h, sid=None):
    srv = SERVERS.get(sid) if sid is not None else h.server_arg()
    if srv is None:
        h.send(404, {"error": "unknown server"})
    return srv


def attachment(h, data, filename):
    return h.send(200, json.dumps(data, indent=2, ensure_ascii=False).encode(), "application/json",
                  headers=[("Content-Disposition", f'attachment; filename="{filename}"')])


# ---- public

@route("GET", r"/livez", "public")
def get_livez(h):
    # the process and every control loop turn, whatever the BMCs answer: Docker's health check
    stuck = stalled(SERVERS.values())
    return h.send(503 if stuck else 200, {"ok": not stuck, "stalled": stuck})


@route("GET", r"/healthz", "public")
def get_healthz(h):
    # 200 only while every BMC answers: an app tile goes red when one stops responding
    down = [s.id for s in list(SERVERS.values()) if s.state["error"]
            or not s.state["updated"] or time.time() - s.state["updated"] > INTERVAL * 4]
    return h.send(503 if down else 200, {"ok": not down, "down": down})


@route("GET", r"/static/(.+)", "public")
def get_static(h, name):
    if name not in STATIC:
        return h.send(404, {"error": "not found"})
    return h.file(name, STATIC[name])


@route("GET", r"/metrics", "public")
def get_metrics(h):
    bearer = h.headers.get("Authorization", "").removeprefix("Bearer ")
    needed = tokens.any_token("metrics")
    if not (tokens.check(bearer, "metrics") or (not config.WEB_PASSWORD and not needed)):
        return h.send(401 if needed else 404, {"error": "metrics need a metrics token"})
    return h.send(200, metrics(SERVERS).encode(), "text/plain; version=0.0.4; charset=utf-8")


@route("GET", r"/login", "public")
def get_login(h):
    return h.redirect("/") if h.authed() else h.file(PAGES["/login"], "text/html; charset=utf-8")


@route("POST", r"/api/login", "public")
def post_login(h):
    if not config.WEB_PASSWORD:
        return h.send(200, {"ok": True})
    address = h.client()
    wait = throttled(address)
    if wait:
        return h.send(429, {"error": f"Too many attempts. Try again in {wait // 60 + 1} min."},
                      headers=[("Retry-After", str(wait))])
    password = str(h.body.get("password", ""))
    role = ("admin" if same_secret(password, config.WEB_PASSWORD)
            else "viewer" if same_secret(password, config.VIEW_PASSWORD) else None)
    if not role:
        failed(address)
        time.sleep(1)
        log.warning("failed sign-in from %s", address)
        return h.send(401, {"error": "Wrong password"})
    ttl = SESSION_LONG if h.body.get("remember") else SESSION_SHORT
    log.info("%s signed in from %s", role, address)
    return h.send(200, {"ok": True, "role": role}, headers=[
        h.set_session(make_token(KEY, ttl, role), ttl if h.body.get("remember") else None)])


@route("POST", r"/api/logout", "public")
def post_logout(h):
    revoke(KEY, h.cookie("session"))  # this session ends for good, even if its cookie was copied
    return h.send(200, {"ok": True}, headers=[h.set_session("", 0)])


# ---- widgets

@route("GET", r"/embed", "widget")
def get_embed(h):
    return h.file(PAGES["/embed"], "text/html; charset=utf-8", frame=True)


@route("GET", r"/api/widget", "widget")
def get_widget(h):
    sid = h.query.get("server", ["all"])[0] or "all"
    servers = [s for s in list(SERVERS.values()) if sid == "all" or s.id == sid]
    if sid != "all" and not servers:
        return h.send(404, {"error": "unknown server"})
    fields = widget_fields()
    return h.send(200, {"servers": [widget_view(s, fields) for s in servers], "fields": fields,
                        "unit": "C", "interval": INTERVAL})


# ---- reading (any session)

@route("GET", r"/", "viewer")
def get_index(h):
    return h.file(PAGES["/"], "text/html; charset=utf-8")


@route("GET", r"/api/overview", "viewer")
def get_overview(h):
    return h.send(200, {"servers": [s.summary() for s in list(SERVERS.values())], "role": h.role(),
                        "drivers": [d.info() for d in DRIVERS.values()],
                        "auth": bool(config.WEB_PASSWORD), "version": VERSION, "interval": INTERVAL,
                        "update": updates.available(), "alerts": bool(webhook_of(alert_config()))})


@route("GET", r"/api/state", "viewer")
def get_state(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    settings = srv.settings()
    with srv.lock:  # copy under the lock, send after: a slow client must never hold up the fans
        body = {**srv.state, **srv.info(), "interval": INTERVAL, "auth": bool(config.WEB_PASSWORD),
                "version": VERSION, "settings": settings, "history": list(srv.history), "events": list(srv.events),
                "calibration": json.loads(json.dumps(srv.calib)),  # a copy: the loop changes it in place
                "handed_until": srv.handed_until,
                "clock": {"time": time.strftime("%H:%M"), "offset": time.strftime("%z")}}
    return h.send(200, body)


@route("GET", r"/api/history", "viewer")
def get_history(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    try:
        seconds = min(7 * 86400, max(3600, int(h.query.get("seconds", ["86400"])[0])))
    except ValueError:
        return h.send(400, {"error": "seconds must be a number"})
    since = time.time() - seconds
    with srv.lock:  # copy under the lock, send after
        points = [p for p in srv.long if p["t"] >= since]
    return h.send(200, {"points": points, "bucket": 300})


@route("GET", r"/api/history\.csv", "viewer")
def get_history_csv(h):
    """The history as a spreadsheet: every reading for the last 3 hours, 5-minute averages beyond."""
    srv = server_or_404(h)
    if srv is None:
        return None
    try:
        seconds = min(7 * 86400, max(600, int(h.query.get("seconds", ["86400"])[0])))
    except ValueError:
        return h.send(400, {"error": "seconds must be a number"})
    since = time.time() - seconds
    with srv.lock:
        points = [p for p in (srv.history if seconds <= HISTORY_SECONDS else srv.long) if p["t"] >= since]
    cols = ("cpu", "cpu_max", "speed", "inlet", "exhaust", "rpm", "fanpct", "watts")
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["time", "cpu_c", "cpu_max_c", "fan_speed_pct", "inlet_c", "exhaust_c", "fan_rpm", "fan_pct", "watts"])
    for p in points:
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p["t"]))] + ["" if p.get(c) is None else p[c] for c in cols])
    return h.send(200, out.getvalue().encode(), "text/csv; charset=utf-8",
                  headers=[("Content-Disposition", f'attachment; filename="{srv.id}-history.csv"')])


@route("GET", r"/api/alerts", "viewer")
def get_alerts(h):
    return h.send(200, public_alert_config(alert_config()))


@route("GET", r"/api/integrations", "viewer")
def get_integrations(h):
    # whether each integration is switched on; the tokens themselves never leave the server
    return h.send(200, {"auth": bool(config.WEB_PASSWORD), "metrics_token": tokens.any_token("metrics"),
                        "metrics_open": not config.WEB_PASSWORD and not tokens.any_token("metrics"),
                        "embed_token": tokens.any_token("widget"), "interval": INTERVAL,
                        "tokens": tokens.public(), "role": h.role(),
                        "env_tokens": {"widget": bool(config.EMBED_TOKEN), "metrics": bool(config.METRICS_TOKEN), "control": False},
                        "widget_fields": widget_fields(), "widget_all": WIDGET_FIELDS,
                        "servers": [{"id": s.id, "name": s.name} for s in list(SERVERS.values())]})


@route("GET", r"/api/metrics-preview", "viewer")
def get_metrics_preview(h):
    return h.send(200, metrics(SERVERS).encode(), "text/plain; charset=utf-8")


@route("GET", r"/api/homarr-widget", "viewer")
def get_homarr_widget(h):
    try:
        definition = homarr_widget(h.query.get("base", [""])[0], h.query.get("server", ["all"])[0],
                                   h.query.get("scope", ["private"])[0])
    except ValueError as e:
        return h.send(400, {"error": str(e)})
    return attachment(h, definition, "fan-control-homarr-widget.json")


# ---- admin: reading what is sensitive

@route("GET", r"/api/servers/([a-z0-9-]+)", "admin")
def get_server(h, sid):
    srv = server_or_404(h, sid)
    if srv is None:
        return None
    # the BMC account name is half a credential
    return h.send(200, {**srv.info(), "username": srv.cfg.get("username", ""),
                        "verify_tls": srv.cfg.get("verify_tls", False), "has_password": bool(srv.cfg.get("password"))})


@route("GET", r"/api/servers/([a-z0-9-]+)/diagnostics", "admin")
def get_diagnostics(h, sid):
    srv = server_or_404(h, sid)
    if srv is None:
        return None
    return attachment(h, diagnostics(srv), f"fan-control-{srv.id}-diagnostics-{time.strftime('%Y%m%d-%H%M')}.json")


@route("GET", r"/api/export", "admin")
def get_export(h):
    secrets_too = h.query.get("secrets", [""])[0] == "1"
    return attachment(h, export_config(secrets_too), f"fan-control-{time.strftime('%Y%m%d-%H%M')}.json")


# ---- admin: changes

@route("POST", r"/api/settings", "control")
def post_settings(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    current = srv.settings()
    try:
        s = validate_settings(h.body, current)
    except (ValueError, TypeError) as e:
        return h.send(400, {"error": str(e)})
    if h.role() != "admin":  # a control token: the protections stay as an admin set them
        touched = [k for k in PROTECTIONS if s[k] != current[k]]
        if touched:
            return h.send(403, {"error": f"a control token cannot change {', '.join(touched)}; an admin can"})
    srv.save_settings(s, h.who())
    return h.send(200, s)


@route("POST", r"/api/alerts", "admin")
def post_alerts(h):
    try:
        cfg = validate_alerts(h.body, alert_config())
    except (ValueError, TypeError, AttributeError) as e:
        return h.send(400, {"error": str(e)})
    save_alerts(cfg)
    log.info("alert settings changed by %s", h.who())
    return h.send(200, public_alert_config(cfg))


@route("POST", r"/api/test-alert", "admin")
def post_test_alert(h):
    # tests the configuration being edited, before it is saved
    try:
        cfg = validate_alerts(h.body.get("config") or {}, alert_config())
    except (ValueError, TypeError, AttributeError) as e:
        return h.send(400, {"error": str(e)})
    kind, channel = h.body.get("kind", "failsafe"), h.body.get("channel")
    if channel is not None:  # ntfy, Gotify or the webhook: plain text, sample values
        if channel not in CHANNELS or kind not in ALERT_KINDS or kind == "report":
            return h.send(400, {"error": "unknown channel or alert"})
        if not cfg["channels"][channel]["url"]:
            return h.send(400, {"error": f"no {channel} address configured"})
        try:
            send_channel(cfg, channel, kind, SAMPLE, "[test] ")
        except Exception as e:  # whatever the other side answered, the user should read it
            return h.send(502, {"error": f"{channel} answered: {e}"})
        return h.send(200, {"ok": True})
    url = webhook_of(cfg)
    if kind not in ALERT_KINDS or not url:
        return h.send(400, {"error": "unknown alert" if url else "no Discord webhook configured"})
    if kind == "report" and not SERVERS:
        return h.send(400, {"error": "add a server first"})
    # the status report is tested with the real readings, everything else with sample values
    payload = build_report(cfg, SERVERS) if kind == "report" else build_payload(cfg, kind, SAMPLE)
    payload["embeds"][0]["title"] = "[test] " + payload["embeds"][0]["title"][:249]
    try:
        post_webhook(url, payload)
    except Exception as e:  # whatever Discord answered, the user should read it
        return h.send(502, {"error": f"Discord answered: {e}"})
    return h.send(200, {"ok": True})


@route("POST", r"/api/import", "admin")
def post_import(h):
    with registry_lock:
        try:
            report = import_config(h.body.get("data"), h.who())
        except (ValueError, TypeError, AttributeError) as e:
            return h.send(400, {"error": f"not a Fan Control backup: {e}"})
    return h.send(200, report)


@route("POST", r"/api/servers/test", "admin")
def post_server_test(h):
    """Read the BMC once with the details being entered, before they are saved."""
    stored = SERVERS.get(str(h.body.get("id", "")))
    try:
        cfg = validate_server(h.body, stored.cfg if stored and stored.cfg.get("source") == "dashboard" else None)
    except (ValueError, TypeError) as e:
        return h.send(400, {"ok": False, "error": str(e)})
    drv = DRIVERS[cfg["driver"]](cfg["host"], cfg["username"], cfg.get("password", ""), cfg["verify_tls"], str(DATA_DIR))
    try:
        data = drv.read()
    except DriverError as e:
        return h.send(200, {"ok": False, "error": str(e)})
    except Exception as e:  # an unknown BMC can answer anything: report it, don't crash the page
        return h.send(200, {"ok": False, "error": repr(e)})
    cpus = [t["value"] for t in data["temps"] if t["cpu"]]
    return h.send(200, {"ok": True, "model": drv.model, "temps": len(data["temps"]), "fans": len(data["fans"]),
                        "cpu": max(cpus) if cpus else None, "watts": data["watts"], "power": data["power"],
                        "control": drv.control})


@route("POST", r"/api/servers/scan", "admin")
def post_server_scan(h):
    try:
        found = scan(str(h.body.get("range", "")))
    except ValueError as e:
        return h.send(400, {"error": str(e)})
    known = {s.cfg.get("host", "").split(":")[0].strip("[]") for s in list(SERVERS.values())}
    for f in found:
        f["added"] = f["host"] in known
    log.info("%s scanned %s: %d found", h.who(), h.body.get("range"), len(found))
    return h.send(200, {"found": found})


@route("POST", r"/api/servers/detect", "admin")
def post_server_detect(h):
    host = str(h.body.get("host", "")).strip()
    try:
        validate_server({"name": "detect", "driver": "redfish", "host": host, "username": "-", "password": "-"})
    except ValueError as e:
        return h.send(400, {"found": False, "error": str(e)})
    return h.send(200, detect(host, str(h.body.get("username", "")), str(h.body.get("password", "")),
                              h.body.get("verify_tls") is True))


@route("POST", r"/api/servers", "admin")
def post_server_add(h):
    with registry_lock:
        try:
            cfg = validate_server(h.body)
        except (ValueError, TypeError) as e:
            return h.send(400, {"error": str(e)})
        cfg["id"] = unique_id(cfg["name"], SERVERS)
        forget_pin(str(DATA_DIR), cfg["host"])  # a new server: whatever certificate it shows first
        srv = start(cfg)
        save_dashboard_servers()
        srv.log(f"Added by {h.who()}")
    return h.send(201, srv.info())


@route("POST", r"/api/servers/([a-z0-9-]+)/accept-certificate", "admin")
def post_accept_certificate(h, sid):
    # the BMC's certificate was replaced on purpose: remember the new one from the next reading
    srv = server_or_404(h, sid)
    if srv is None:
        return None
    forget_pin(str(DATA_DIR), srv.host)
    srv.log(f"New TLS certificate of the BMC accepted by {h.who()}", "warn")
    srv.wake.set()
    return h.send(200, {"ok": True})


@route("POST", r"/api/servers/([a-z0-9-]+)(/delete)?", "admin")
def post_server_edit(h, sid, delete):
    with registry_lock:
        srv = server_or_404(h, sid)
        if srv is None:
            return None
        if srv.cfg.get("source") == "environment":
            return h.send(409, {"error": "this server is defined in the environment; change it there"})
        if delete:
            stop(srv, forget=True)
            forget_pin(str(DATA_DIR), srv.host)
            save_dashboard_servers()
            log.info("%s removed by %s", srv.name, h.who())
            return h.send(200, {"ok": True})
        try:
            cfg = validate_server(h.body, srv.cfg)
        except (ValueError, TypeError) as e:
            return h.send(400, {"error": str(e)})
        stop(srv)  # restart with the new address, credentials or driver
        forget_pin(str(DATA_DIR), cfg["host"])  # saving accepts the certificate the BMC shows now
        srv = start(cfg)
        save_dashboard_servers()
        srv.log(f"Connection settings changed by {h.who()}")
    return h.send(200, srv.info())


@route("POST", r"/api/sessions/revoke-all", "admin")
def post_revoke_all(h):
    revoke_all()
    log.info("every session signed out by %s", h.who())
    # the admin who asked stays signed in, with a fresh session
    return h.send(200, {"ok": True}, headers=[h.set_session(make_token(KEY, SESSION_SHORT, "admin"), None)])


@route("POST", r"/api/tokens", "admin")
def post_token(h):
    try:
        token, row = tokens.create(h.body.get("name"), h.body.get("kind"))
    except ValueError as e:
        return h.send(400, {"error": str(e)})
    log.info('%s token "%s" created by %s', row["kind"], row["name"], h.who())
    return h.send(200, {"token": token, **row})


@route("POST", r"/api/tokens/revoke", "admin")
def post_token_revoke(h):
    try:
        tokens.revoke(str(h.body.get("id", "")))
    except ValueError as e:
        return h.send(404, {"error": str(e)})
    log.info("token %s revoked by %s", h.body.get("id"), h.who())
    return h.send(200, {"ok": True})


@route("POST", r"/api/widget-config", "admin")
def post_widget_config(h):
    try:
        save_widget_fields(h.body.get("fields"))
    except ValueError as e:
        return h.send(400, {"error": str(e)})
    log.info("widget fields set to %s by %s", widget_fields(), h.who())
    return h.send(200, {"fields": widget_fields()})


@route("GET", r"/api/settings/history", "viewer")
def get_settings_history(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    return h.send(200, {"changes": srv.changes()[::-1]})


SEL_LOCKS, SEL_CACHE = {}, {}  # server id -> lock; -> (when, result)


@route("GET", r"/api/sel", "viewer")
def get_sel(h):
    """The BMC's event log, read when asked: it takes the BMC a few seconds."""
    srv = server_or_404(h)
    if srv is None:
        return None
    cached = SEL_CACHE.get(srv.id)
    if cached and time.monotonic() - cached[0] < 60 and "fresh" not in h.query:
        return h.send(200, cached[1])
    lock = SEL_LOCKS.setdefault(srv.id, threading.Lock())
    if not lock.acquire(blocking=False):  # each read opens BMC sessions the control loop needs
        return h.send(429, {"error": "the event log is being read already; try again in a moment"})
    try:
        sel = srv.driver.sel()
    except DriverError as e:
        return h.send(503, {"error": str(e)})
    finally:
        lock.release()
    SEL_CACHE[srv.id] = (time.monotonic(), sel)
    return h.send(200, sel)


@route("POST", r"/api/sel/clear", "admin")
def post_sel_clear(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    try:
        srv.driver.clear_sel()
    except DriverError as e:
        return h.send(503, {"error": str(e)})
    SEL_CACHE.pop(srv.id, None)
    srv.log(f"BMC event log cleared by {h.who()}")
    return h.send(200, {"ok": True})


@route("POST", r"/api/calibrate", "admin")
def post_calibrate(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    if h.body.get("action") == "stop":
        with srv.lock:
            if srv.calibrating():
                srv.finish_calibration("stopped", f"stopped by {h.who()}")
        return h.send(200, {"ok": True})
    why = srv.start_calibration(h.who())
    return h.send(409, {"error": why}) if why else h.send(200, {"ok": True})


@route("POST", r"/api/handover", "control")
def post_handover(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    minutes = h.body.get("minutes")
    if not (type(minutes) is int and 0 <= minutes <= 7 * 24 * 60):
        return h.send(400, {"error": "minutes must be a whole number from 0 to 10080 (7 days)"})
    if not srv.control:
        return h.send(409, {"error": "this server is monitored only"})
    srv.hand_over(minutes, h.who())
    return h.send(200, {"handed_until": srv.handed_until})


@route("POST", r"/api/smart/forget", "admin")
def post_smart_forget(h):
    srv = server_or_404(h)
    if srv is None:
        return None
    srv.forget_learned(h.who())
    return h.send(200, {"ok": True})


# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    server_version = "fan-control"
    sys_version = ""
    timeout = 30  # drop clients that open a connection and never finish the request
    DEADLINE = 120  # s a connection may live at all: a client dripping a byte now and then keeps no slot

    def setup(self):
        super().setup()
        self.deadline = threading.Timer(self.DEADLINE, self.cut)
        self.deadline.daemon = True
        self.deadline.start()

    def cut(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.deadline.cancel()
        super().finish()

    def log_message(self, *_):
        pass

    def cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def role(self):
        """"admin", "viewer" or None for the person behind this request."""
        if not config.WEB_PASSWORD:
            return "admin"
        return valid_token(KEY, self.cookie("session"))

    def authed(self, widget=False):
        """A session of any role; with widget, a widget token also counts (?token= or Bearer: dashboards
        that fetch server side, like Homarr's custom widgets, send it as a header)."""
        if self.role():
            return True
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return widget and (tokens.check(self.query.get("token", [""])[0], "widget") or tokens.check(bearer, "widget"))

    def client(self):
        """The address to account sign-ins and changes to (see sessions.client_address)."""
        return client_address(self.client_address[0], self.headers.get("X-Forwarded-For", ""))

    def control_token(self):
        """The name of the control token sent as a Bearer header, or None."""
        bearer = self.headers.get("Authorization", "")
        return tokens.name_of(bearer.removeprefix("Bearer "), "control") if bearer.startswith("Bearer ") else None

    def who(self):
        token = None if self.role() else self.control_token()
        return f'token "{token}" at {self.client()}' if token else f"{self.role()} at {self.client()}"

    answered = False  # the status line went out: past it, an error can only close the connection

    def send(self, code, body=b"", ctype="application/json", headers=(), frame=False):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.answered = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if frame:  # the read-only embed may sit in a dashboard's iframe
            self.send_header("Content-Security-Policy", CSP + "; frame-ancestors *")
        else:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP + "; frame-ancestors 'none'")
        for k, v in headers:
            self.send_header(k, v)
        if not any(k == "Cache-Control" for k, _ in headers):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def file(self, rel, ctype, frame=False):
        body = (WEB / rel).read_bytes()
        etag = '"' + hashlib.sha1(body, usedforsecurity=False).hexdigest()[:16] + '"'
        cache = [("Cache-Control", "no-cache"), ("ETag", etag)]
        if self.headers.get("If-None-Match") == etag:
            return self.send(304, headers=cache, frame=frame)
        self.send(200, body, ctype, headers=cache, frame=frame)

    def redirect(self, to):
        self.send(303, headers=[("Location", to)])

    def set_session(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        age = f"; Max-Age={max_age}" if max_age is not None else ""
        return ("Set-Cookie", f"session={value}; Path=/; HttpOnly; SameSite=Strict{secure}{age}")

    def server_arg(self):
        sid = self.query.get("server", [""])[0]
        return SERVERS.get(sid) or (None if sid or not SERVERS else next(iter(SERVERS.values())))

    def parse(self):
        url = urlsplit(self.path)
        self.route, self.query = url.path, parse_qs(url.query)

    def host_allowed(self):
        """Without a password, a page on any site could rebind its own name to this address and act as
        admin (DNS rebinding). So the Host must be an address, localhost, a name without dots or under
        .local/.lan/.home/.internal/.home.arpa, or one listed in ALLOWED_HOSTS. With a password the
        session cookie, which such a page never has, does the job."""
        if config.WEB_PASSWORD:
            return True
        import ipaddress
        host = (self.headers.get("Host") or "").lower()
        name = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if host in config.ALLOWED_HOSTS or name in config.ALLOWED_HOSTS or name in ("", "localhost") or "." not in name:
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return name.endswith((".local", ".lan", ".home", ".internal", ".home.arpa", ".localhost"))

    def dispatch(self, method):
        if not self.host_allowed():
            return self.send(403, {"error": "this host name is not allowed without a password: set WEB_PASSWORD, "
                                            "or add it to ALLOWED_HOSTS"})
        for m, pattern, access, fn in ROUTES:
            match = pattern.match(self.route) if m == method else None
            if not match:
                continue
            if access == "widget" and not self.authed(widget=True):
                if self.route == "/embed":
                    return self.send(401, b"Sign in, or add ?token= with a widget token", "text/plain; charset=utf-8", frame=True)
                return self.send(401, {"error": "sign in, or send a widget token as a Bearer token or ?token="})
            by_token = access in ("viewer", "control") and not self.role() and self.control_token()
            if access in ("viewer", "control", "admin") and not self.authed() and not by_token:
                return self.redirect("/login") if (method, self.route) == ("GET", "/") else \
                    self.send(401, {"error": "sign in required"})
            if access in ("control", "admin") and self.role() != "admin" and not by_token:
                return self.send(403, {"error": "this account can only look"})
            try:
                return fn(self, *match.groups())
            except Exception as e:
                # an answer, never a dropped connection: behind a reverse proxy that shows up as an
                # opaque 502, with the cause nowhere. The traceback goes to the log.
                log.exception("%s %s failed", method, self.route)
                if self.answered:  # too late for a status; the connection just closes
                    return None
                return self.send(500, {"error": f"internal error: {e!r}; the log has the details"})
        return self.send(404, {"error": "not found"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.parse()
        self.dispatch("GET")

    def do_POST(self):
        self.parse()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send(400, {"error": "bad Content-Length"})
        big = self.route == "/api/import" and self.role() == "admin"  # backups can be large; only an admin sends one
        if not 0 <= length <= (1_000_000 if big else 10000):
            return self.send(413, {"error": "request too large"})
        # read before any refusal: closing a socket with unread data resets the connection, which a
        # reverse proxy reports as 502 instead of passing the refusal on
        raw = self.rfile.read(length)
        # JSON only: a cross-site form cannot send this content type without a CORS preflight
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.send(415, {"error": "expected application/json"})
        origin = self.headers.get("Origin")
        hosts = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")}
        if origin and urlsplit(origin).netloc not in hosts:
            return self.send(403, {"error": "cross-origin request refused"})
        try:
            self.body = json.loads(raw or b"{}")
            if not isinstance(self.body, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, RecursionError) as e:
            return self.send(400, {"error": str(e)[:200] or "nested too deep"})
        self.dispatch("POST")
