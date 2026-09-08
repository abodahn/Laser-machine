# T&C Laser Intelligence & Command Center

Centralised monitoring, analytics, alerting and forecasting for the Jeanologia laser department.

Machine data → real-time monitoring → central database → analytics → alerts → forecasting → decisions.

---

## 1. What it does

| Area | Capability |
|---|---|
| Connectivity | Per-machine SMB pull or HTTP push, heartbeat, auto-reconnect, latency, incident logging |
| Central database | One SQLite database holding current state **and** full history (880k+ job records, 8 years) |
| Live state | PRODUCING / IDLE / STOPPED / ALARM / MAINTENANCE / OFFLINE / DISABLED, with a state timeline |
| Analytics | Utilization, availability, performance, OEE, laser duty, throughput, downtime, bottlenecks, capacity |
| Alerts | 18 rule types, three severities, acknowledge / assign / resolve, escalation, Brevo email + Teams + WhatsApp |
| Forecasting | End-of-day output, target risk, downtime, health trajectory, per-machine behavioural baselines |
| Health | 0–100 Machine Health Score with a visible component breakdown |
| Dashboards | Command Center, Management, Production, Maintenance, Technical/IT, plus per-machine drill-down |
| Reports | 13 reports, exportable to Excel and PDF |
| Security | Role-based access, encrypted machine credentials, full audit trail, nightly backup |

---

## 2. Install and run

```bash
cd D:\Jeanologia\platform
pip install -r requirements.txt
python run.py --port 8800
```

Open <http://localhost:8800>.

On first start the platform:

1. creates `data/laser_platform.db` and its schema,
2. imports `machines.json` into the machine registry (26 machines, credentials encrypted),
3. imports the history from `central.db`,
4. builds all rollups, baselines and forecasts,
5. prints a **one-time random admin password** to the console — change it after signing in.

To set the first password yourself instead:

```bash
set LASER_ADMIN_PASSWORD=YourStrongPassword
```

### Run as a Windows service

Register with NSSM (or Task Scheduler, "run whether user is logged on or not"):

```bash
nssm install LaserCommandCenter "C:\Program Files\Python314\python.exe" "D:\Jeanologia\platform\run.py --port 8800"
```

The service account must be a domain account that can reach `\\<machine>\JeanologiaDB` on every
machine — see *Connectivity* below.

---

## 3. Connectivity

Each machine exposes a read-only SMB share `\\<host>\JeanologiaDB` containing the eMark databases.
The collector never writes to a machine; it copies the SQLite files to `data/cache/<machine id>/`
and reads the copy, so it can never lock or block the machine's own software.

Per machine, everything is configurable from **Machine Config** — no code change, no restart:

Machine name · Machine ID/code · IP or hostname · Port · Share · Database file · Username ·
Password · Connection method · Model · Machine type · Serial · Location · Production area ·
Status · Heartbeat interval · Sync interval · Timeout · Retry attempts · Retry backoff ·
Daily target · Ideal seconds per unit · Enabled.

**Credentials** are stored Fernet-encrypted, per machine, and are never written to this
repository. The key lives in `data/secret.key` (or the `LASER_SECRET_KEY` environment variable —
prefer that in production, and keep the key off the same disk as the backups). Only users with the
`admin` role can read a machine password back, through the Machine Configuration screen.

Machines do not all share one account: several have their own local Windows account. Read and
change them in Machine Configuration — never in source. Nothing in this repository contains a
factory password, hostname or IP, and nothing should: the public mirror redacts host, share and
`db_file` values, including the UNC paths that Windows quotes back inside error text.

**Machines still not connected (4 of 26).** Each has a confirmed cause:

| Machine | Host | Error | Action needed |
|---|---|---|---|
| 3 | *(see Machine Config)* | WinError 53/67 — name does not resolve | The PC was renamed or replaced. Set its current IP in Machine Config. It has 0 rows in 8 years of history, so it has never been collected. |
| 14 | *(see Machine Config)* | WinError 1272 — guest access blocked | The share is published for **guest** access, so it ignores any credentials and Windows blocks the guest logon. Fix on that PC: share `JeanologiaDB` to that machine's own service account with Read, remove Everyone/Guest. |
| 26 | *(see Machine Config)* | WinError 1272 | Same as 14. |
| 20 | *(no host)* | push transport, nothing received | Configure the machine to POST to `/api/ingest/20` with its push token, or give it an IP and switch it to SMB. |

Error 1272 cannot be fixed with a username/password — eight credential combinations were tested and
all returned it. The alternative to fixing the two shares is enabling insecure guest SMB on the
collector host, which weakens every SMB connection that server makes and is not recommended.

The platform reports the exact Windows error per machine on the Technical dashboard and in
**Test connection**.

**HTTP push** (machine 20): set `connection_method = http_push`, set a `push_token`, and have the
machine POST:

```
POST /api/ingest/20
{"token": "<push_token>", "rows": [{"id": 1, "init_date": "...", "end_date": "...", "design": "...",
  "copies": 1, "units": 1, "laser_time": 58422, "user": "design", "alarm1": 0, "alarm2": 0}]}
```

> Set a push token on every push machine. With `push_token` empty the endpoint accepts anonymous
> data — acceptable on an isolated plant LAN, not on a routed network.

---

## 4. How the numbers are calculated

Every KPI is explainable. If a manager disputes a figure, this is the answer.

| Metric | Definition |
|---|---|
| **Busy time** | Sum of job durations (`end_date − init_date`) |
| **Idle time** | Planned time − busy − downtime |
| **Downtime** | Time in STOPPED / ALARM / MAINTENANCE state, recorded by the state engine |
| **Utilization** | busy ÷ planned time (planned = 24 h − 3 × 15 min breaks, configurable) |
| **Availability** | busy ÷ (planned − offline time) — excludes time the machine was unreachable |
| **Performance** | ideal ÷ actual run time, where `ideal = laser-on seconds + best-decile handling seconds × units`. Laser time is fixed by the design and cannot be recovered; handling time can. So performance measures the part a supervisor can act on. Capped at 200 %. |
| **Quality (proxy)** | Share of jobs that completed without an alarm. **The machines do not record rejects** — this is a proxy and is labelled as such. Disable it in Settings for a two-factor OEE. |
| **OEE** | Availability × Performance × Quality |
| **Laser duty** | Laser-on time ÷ job wall time — the clearest measure of handling loss (fleet average ≈ 0.6, best machines ≈ 0.8) |
| **Health score** | Weighted 0–100 from connectivity 20, alarms 20, downtime 20, utilization 15, performance 15, thermal 10. Weights configurable. |
| **Forecast** | Today's units + (recent 3-hour rate blended 50/50 with the machine's own hour-of-week baseline) × hours remaining. Machines with under three weeks of history use the recent rate only. |
| **Maintenance risk** | Critical-alarm rate vs the previous six weeks, peak-temperature trend, scan-head calibration drift, and health trajectory |
| **Lost capacity** | (idle + downtime hours) × that machine's units per running hour |

Shifts default to A 06:00–14:00, B 14:00–22:00, C 22:00–06:00. A shift crossing midnight is credited
to the day it started. Change them in Settings; all history re-buckets on the next rebuild.

---

## 5. Roles

| Role | Can do |
|---|---|
| `admin` | Everything, including machine credentials and user management |
| `manager` | All dashboards, reports, acknowledge alerts |
| `production` | Live state, acknowledge alerts, reports |
| `maintenance` | Health, alarms, acknowledge alerts, set machines to maintenance |
| `it` | Connectivity, machine configuration, settings, alerts |
| `viewer` | Read only |

Every configuration change, login, export and acknowledgement is written to the audit trail.

---

## 6. Alerts

18 rule types ship enabled, each with a configurable threshold, window, cooldown, severity,
channels, recipients and escalation:

`machine_offline` · `heartbeat_lost` · `slow_response` · `communication_failure` · `sync_failure` ·
`machine_stopped` · `excessive_idle` · `repeated_alarm` · `production_drop` · `performance_low` ·
`excessive_downtime` · `abnormal_behaviour` · `maintenance_risk` · `target_risk` · `thermal_risk` ·
`head_drift` · `laser_credit_low` · `data_quality`

Rules are evaluated every 60 seconds. Set channels per rule to `app`, `email`, `teams`, `whatsapp`
(comma-separated).

**Email via Brevo.** Settings → Notifications → paste the Brevo API key and sender address, then
use *Send test email*. The platform calls Brevo's transactional endpoint directly; no SDK.

---

## 7. Reports

Daily Laser Production · Shift Performance · Machine Utilization · Downtime · Alarm ·
Machine Health · Weekly Performance · Monthly Management · Production Loss Analysis ·
Capacity Analysis · Maintenance Analysis · Alert Report · Connectivity & Data Ingestion.

Preview in the browser, or export to `.xlsx` / `.pdf` from Reports.

---

## 7b. Wall display (production-floor screen)

Open **Wall Display** in the sidebar, or go straight to `http://<server>:8800/#/wall`.

A full-screen board with no sidebar or menus, sized to be read across the shop floor. It cycles
through six screens on a timer (**5 seconds by default**) and refetches data once per full cycle,
so it can be left running for days.

| Screen | Shows |
|---|---|
| Fleet status | Every machine as a colour-coded tile with today's units, sorted producing-first |
| Production today | Units vs target, end-of-day forecast, laser duty, output by hour |
| Machine output ranking | Top 10 machines over 7 days |
| Alerts & attention | Open criticals and warnings, machines needing attention |
| Machine health | The 10 lowest health scores |
| Shift performance | Shift A/B/C totals over 7 days |

Controls (hover the header to reveal them): interval **3/5/8/12/20/30 s**, pause, previous, next,
full screen, exit. Keyboard: **space** pause, **←/→** step, **F** full screen, **Esc** exit.
The screen tints red whenever a critical alert is open, and the critical siren still sounds.

**To run it on a TV:** point a browser at the URL, sign in once, press **F** for full screen. Set
the PC to not sleep. The session cookie lasts 12 hours — for a permanently mounted screen, create a
dedicated `viewer` role user so it has read-only access.

---

## 8. Operations

- **Backup** — nightly at 02:15 to `data/backup/`, 14 kept, using SQLite's online backup API
  (consistent while the platform is running). Manual backup: Technical dashboard → *Backup now*.
- **Nightly maintenance** — refreshes machine identity, rebuilds 45 days of rollups, rebuilds
  baselines, prunes logs older than the retention setting, expires sessions.
- **Adding a machine** — Machine Config → *Add machine*. It is collected from the next cycle.
- **Recomputing history** — Settings → *Recompute all analytics* after changing shifts, thresholds
  or targets.
- **Health check** — `GET /health` (no authentication) for a load balancer or monitoring probe.
- **Self-check** — `python test_platform.py` runs assertions over the shift logic, design parsing,
  duration handling, KPI arithmetic, health scoring, RBAC and credential encryption.

---

## 9. Data investigation

`DATA_DICTIONARY.json` documents every field available from the machines: what it represents, its
type, source, machine relationship, operational meaning, whether it changes in real time, and its
KPI / alert / forecasting / maintenance / productivity value. It is also browsable in the UI at
Settings → Data dictionary.

Data the previous collector was **not** capturing and this platform now uses:

- **operator** (`user`) — output and throughput per operator
- **alarm1 / alarm2** — per-job alarm counts, per laser head
- **galvo and servo peak temperatures** — thermal trend, predictive maintenance
- **optimizer flag** — measured benefit of optimised designs
- **eMarkVersion** — fleet software drift
- **error.db** — ~1.3 million alarm, error and warning records fleet-wide
- **headHCM** — scan-head calibration drift, the strongest predictor of scanner service
- **machines.db** — model, serial, laser count, duty ceiling, measured laser power
- **codes.db** — laser licence/rental credit; an unnoticed expiry stops production outright

Known data-quality issues are handled rather than hidden: 331 job records with impossible
timestamps (machine clock drift) are flagged `suspect=1`, excluded from every KPI, and raise a
`data_quality` alert naming the machine whose clock needs correcting.

---

## 10. Layout

```
platform/
  run.py                 entry point
  requirements.txt
  README.md
  DATA_DICTIONARY.json   full field investigation
  test_platform.py       self-check
  app/
    schema.sql           central database schema
    config.py            paths, credential encryption, defaults
    db.py                sqlite access, shifts, design parsing, legacy import
    auth.py              users, sessions, RBAC
    collector.py         SMB/heartbeat/ingestion/state engine
    analytics.py         rollups, OEE, health, baselines, forecasting
    alerts.py            rule engine and notification delivery
    reports.py           report builders, Excel and PDF renderers
    api.py               HTTP API
    main.py              app and background schedulers
  web/                   index.html, app.js, style.css
  data/                  laser_platform.db, cache/, backup/, secret.key
```

Scaling to more machines is a row in the machine registry. SQLite in WAL mode comfortably handles
this workload (26 machines, ~1,000 jobs/day, 880k rows); if the fleet grows past roughly 100
machines, `db.py` is the only module that needs to change to move to PostgreSQL.
