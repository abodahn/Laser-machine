# Deploying the public mirror

## The one thing that must not be forgotten

**The collector keeps running on the factory PC.** Render is public cloud; it cannot
route to 10.100.x.x, so it can never poll a laser. The Windows service `TCLaserPlatform`
remains the only thing that talks to the machines. The mirror shows a copy of what the
collector has already collected, and nothing else. If the factory PC is off, the mirror
freezes and then empties — that is not a mirror fault.

## Two roles, one codebase

| `LASER_ROLE`          | Runs                                                    |
|-----------------------|---------------------------------------------------------|
| unset / `collector`   | Today's behaviour, unchanged: SMB heartbeat, sync, analytics, alerts, nightly, watchdog. Plus a push loop, but only when `LASER_MIRROR_URL` and `LASER_MIRROR_TOKEN` are both set. |
| `mirror`              | No loops at all. No SMB, no legacy import, no analytics. Serves the existing UI and read APIs from whatever the collector pushed. Every write endpoint answers 409. |

The mirror skips the analytics and nightly loops for correctness, not thrift:
`analytics.refresh_deep()` does an unconditional `DELETE FROM design_stats` and
`store_forecasts()` deletes today's forecasts. On a mirror those would erase the data
that was just pushed and rebuild the rollups from an empty `production` table.

## On Render

`render.yaml` describes the service (free plan, Docker, health check `/health`). Set
three secrets in the Render dashboard — all are `sync: false`, so they are never in git:

| Variable               | Value                                                       |
|------------------------|-------------------------------------------------------------|
| `LASER_MIRROR_TOKEN`   | `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `LASER_SECRET_KEY`     | `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` |
| `LASER_ADMIN_PASSWORD` | the password you want for the mirror's `admin` account       |

`LASER_ROLE=mirror` and `LASER_DATA=/data` are already in `render.yaml`.

**Never copy the factory's `data/secret.key` to Render.** It decrypts every machine
password and the mirror has no machine passwords to decrypt — `password_enc` is one of
the seven columns blanked before anything leaves the building. A throwaway key is
correct here.

`LASER_ADMIN_PASSWORD` matters because the free plan's disk is ephemeral: every redeploy
starts from an empty database, so the admin account is recreated each time. With the
variable set the password is the same every time; without it, a random one is generated
and only appears in the Render log.

No persistent disk is requested. A wipe costs one push cycle (measured: a full refill is
514 KiB and one HTTP round trip). The 60 s push also counts as inbound traffic, so the
free service does not idle down.

## On the factory PC

Two variables on the existing service, nothing else:

```
LASER_MIRROR_URL=https://tc-laser-mirror.onrender.com
LASER_MIRROR_TOKEN=<the same token as Render>
```

Optional: `LASER_MIRROR_INTERVAL` (seconds, default 60).

The push loop only starts when **both** are set, so an unconfigured collector behaves
exactly as it does today. A push failure logs one warning and retries next cycle; it
cannot stall or crash collection.

## The mirror has to be served over HTTPS

The session cookie is issued with `Secure` in the mirror role — it is the one deployment
reachable from the public internet. Render terminates TLS, so this is invisible there.
It is not invisible in a local test: a mirror on plain `http://` answers **200 to the
login and 401 to every request after it**, because curl/httpx/requests will not store a
Secure cookie. Browsers exempt `localhost`, so a browser on `http://127.0.0.1:8899`
still works; a scripted smoke test does not. Check replication with `/api/mirror/state`
and the token instead — that route needs no session.

## Confirming data is flowing

From anywhere, with the token:

```bash
curl -H "X-Mirror-Token: $LASER_MIRROR_TOKEN" https://<mirror>/api/mirror/state
```

Returns `machines` (should be 26), `last_push` (a timestamp that must advance every
minute) and `have` — the 17 slice hashes the mirror is holding.

In the collector's log, one line per cycle:

```
INFO laser.mirror mirror push: 2 slice(s), 5.3 KiB (machine_state,alerts_open)
```

If the pusher sends N rows and the mirror writes a different number, it logs
`mirror wrote X rows for <slice>, we sent Y`. That should never appear.

## What actually goes over the wire

17 slices, hashed independently; only the ones that changed are sent, gzipped.
Measured against the live 670 MB database:

| | |
|---|---|
| Full refill (cold start or after a wipe) | **514 KiB** gzipped, 43k rows |
| Quiet 60 s cycle (`machine_state` + `alerts_open`) | **5.3 KiB** |
| Cycle that catches the 180 s analytics run (+ rollups, forecast) | **45.5 KiB** |
| Per day | **~26 MiB** |
| Resulting mirror database | **6.7 MiB** (vs 670 MB on-prem) |

## What is deliberately missing on the mirror

Not a bug, do not "fix" it by mirroring more:

* **Machine credentials.** `host`, `port`, `share`, `db_file`, `username`,
  `password_enc`, `push_token` are pushed as explicit `NULL`. The machine detail header
  shows `smb` instead of an IP, and the admin machines table has blank connection
  columns. **They are sent as explicit NULLs, never omitted** — omitting a column makes
  SQLite fill the schema DEFAULT, which would republish `username='Admin'`,
  `share='JeanologiaDB'`, `db_file='stats.db'`, `port=445` on the public internet.
* **`production` (871k rows) and `machine_alarms` (1.5M).** Too big, and they only feed
  per-job drill-downs. Consequence: the **Operators** and **Designs** panels and the
  **alarm analysis** page render empty. Per-machine "recent jobs" and "recent alarms"
  lists are empty too. Every rollup built from them (daily, hourly, health, OEE,
  forecast, design stats) is mirrored and correct.
* **`settings`.** It holds the Brevo, WhatsApp and Teams keys. `db.get_setting()` falls
  back to `config.DEFAULTS`, so the mirror runs correctly with no settings rows.
* **`users`, `sessions`, `audit_log`, `connection_log`, `sync_log`.** On-prem
  operational data. The mirror has its own single admin account.
* **Every write.** Acknowledging an alert, editing a machine, changing settings — all
  answer `409 read-only mirror`. Acks happen on-prem. Proxying them back through the
  pusher is a feature request, not a bug.

## No new dependencies

`httpx>=0.27` was already in `requirements.txt` (alerts.py uses it for Brevo/Teams);
`gzip`, `hmac`, `hashlib` and `json` are stdlib. Do not add `requests` or a compression
library.

## Self-check

`python -m app.mirror` prints every slice, its row count and hash, the full gzipped
payload size, and asserts that no machine row carries a credential.
