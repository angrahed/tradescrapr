# tradescrapr

[![ci](https://github.com/angrahed/tradescrapr/actions/workflows/ci.yml/badge.svg)](https://github.com/angrahed/tradescrapr/actions/workflows/ci.yml)

A resumable [UN Comtrade](https://comtradeplus.un.org) scraper built for
unattended operation, with two modes:

1. **Targeted scrapes** — expand a YAML spec into a persistent task queue that
   can run for days or weeks within the free tier's ~500 calls/day quota,
   surviving kills, reboots, and quota exhaustion.
2. **Monthly monitor** — a cron/launchd job that detects newly published (or
   re-published) monthly data via the free availability endpoint and scrapes
   exactly that, at full detail.

Plus a full-screen interactive job builder (just run `tradescrapr`) to pick
exactly what to download, and a live terminal dashboard (`tradescrapr ui`) to
watch it work.

**Key properties**

- *Resume-by-default*: every task is idempotent and checkpointed in SQLite;
  `kill -9`, reboot, or quota exhaustion never loses work — just run again.
- *Quota-aware*: a persistent daily-call ledger budgets 450 of the free tier's
  500 calls, sleeps until the reset, and resyncs if the server disagrees.
- *Cap-aware*: responses that hit the per-call record cap split automatically
  (flow → partner groups sized from the availability endpoint's record counts
  → HS-chapter batches) and the children are tracked as ordinary tasks.
- *Revision-aware*: when a reporter re-publishes a month, the monitor wipes and
  re-scrapes that partition and cancels any stale queued tasks for it.
- *Clean data*: hive-partitioned parquet with pinned dtypes (HS codes stay
  strings — no leading-zero corruption) and a provenance manifest per chunk.
- *Free and premium*: one `tier` switch; premium keys use whole-file bulk
  downloads instead of paged calls.

## Requirements

- Linux or macOS (Windows unsupported — the locking and dashboard are POSIX).
  CI runs the full suite on both.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (manages
  Python 3.12 and all dependencies; you never need pip or a system Python).
- A UN Comtrade API key — free, see next section.

## Install

```bash
git clone https://github.com/angrahed/tradescrapr.git
cd tradescrapr
uv sync                        # creates .venv, installs pinned dependencies
uv run tradescrapr --version   # sanity check
uv run tradescrapr refs refresh  # cache reporter/partner/HS reference data (no key needed)
```

## API key

### Getting a free key

1. Register (free) at <https://comtradeplus.un.org>.
2. Sign in to the developer portal at <https://comtradedeveloper.un.org> with
   the same account, go to **Products**, and subscribe to the free API product.
3. Your subscription key appears under your profile. That's the key you give
   tradescrapr.

### Providing the key

tradescrapr reads, in order of preference:

1. the **`COMTRADE_API_KEY`** environment variable — set it in your shell,
   crontab, systemd unit, or launchd plist and everything just works:

   ```bash
   export COMTRADE_API_KEY="your-key-here"
   ```

2. a **`.env` file** at the project root (`cp .env.example .env` and fill it
   in). A real environment variable wins over `.env` for the same name.

The legacy name `COMTRADE_KEY` is also accepted. The key is only ever sent as a
request header — it never appears in URLs, logs, manifests, or the state
database, and `.env` is gitignored.

### Public repo safety

Do not commit real API keys, `.env`, downloaded data, SQLite state, or logs.
The default `.gitignore` excludes `.env`, `data/`, `state/`, `logs/`, caches,
and the virtual environment. `.env.example` intentionally contains only the
placeholder `COMTRADE_API_KEY=your-key-here`.

### Free vs. premium

| | Free | Premium Individual | Premium Institutional |
|---|---|---|---|
| Cost | $0 | $2,000/yr | $6,000–12,000/yr |
| Calls/day | 500 (tradescrapr budgets 450) | 5,000 | unlimited |
| Records/call | 100,000 | 250,000 | 250,000 |
| Bulk files | — | ✓ | ✓ |

Free tier is comfortable for the ongoing monthly monitor (~1–2 days of quota
per new month) and fine for the default 2024+ backfill (~3–6 weeks). Deep
historical backfills are premium territory (days instead of months via bulk
files). Premium is sold through [shop.un.org](https://shop.un.org/databases) /
subscriptions@un.org; a free 15-business-day
[trial](https://uncomtrade.org/docs/premium-trial-subscription/) exists, and
institutions often already hold a site license — check before buying.

With a premium key, set `tier: premium` in `config.yaml`: the record cap rises,
the daily budget lifts, and the monitor switches to bulk-file downloads
automatically.

## Quickstart: the smoke test

Before trusting a long run, spend ~6 quota calls verifying everything end to end:

```bash
uv run tradescrapr plan jobs/smoke.yaml --dry-run   # 4 tasks, nothing written
uv run tradescrapr plan jobs/smoke.yaml
uv run tradescrapr run
uv run tradescrapr status                            # expect 4/4 done
```

You should find four parquet files with manifests under
`data/raw/comtrade/final/freq=A/period=2022/`.

## Interactive job builder

The quickest way to set up a download is the full-screen interactive form —
just start tradescrapr:

```bash
uv run tradescrapr        # in a terminal, opens the job builder
uv run tradescrapr new    # same thing, explicit
```

Pick reporters/partners/commodities from searchable lists (`space` toggles,
`*` selects all). In reporter, partner, and commodity-code pickers, `I` imports
the selection from a `.txt`, `.csv`, or `.json` list. Set periods and flows, and
watch the live task/quota estimate. `s` saves the spec to `jobs/<name>.yaml`,
plans the job, and then offers to start the worker or open the dashboard. The
saved YAML is a normal job spec — everything below applies to it.

### Import file formats

Picker imports replace the current selection. They validate every identifier
before applying anything, so a file with one unknown country or product code
leaves the picker unchanged.

Supported formats:

- `.txt`: identifiers separated by newlines, commas, semicolons, or whitespace.
- `.csv`: the first non-empty column from each row; a header such as `code`,
  `country_code`, `iso3`, `product_code`, or `hs_code` is ignored.
- `.json`: an array of identifiers, an array of objects with keys such as
  `code`, `id`, `iso3`, `cmdCode`, or `product_code`, or an object containing a
  list under `codes`, `reporters`, `partners`, `commodities`, or `products`.

Reporters and partners may be ISO3 or numeric Comtrade codes. Commodity imports
must match codes in the selected classification, and leading zeroes are kept.

## Targeted mode

Job specs live in `jobs/*.yaml`:

```yaml
name: eu3_hs6_annual
dataset: final            # final | tariffline
type: C                   # C goods, S services
freq: A                   # A annual, M monthly
classification: HS
reporters: [DEU, FRA, ITA]   # ISO3 / numeric codes, or "all"
partners: all
flows: [M, X]
commodities: {level: 6}   # or {codes: [870321, "8703", TOTAL]}
periods: "2015:2024"      # ranges/lists; monthly like "202001:202412"
breakdown: classic        # classic = totals over partner2/customs/transport-mode
                          # full  = every breakdown (~10x the rows)
```

```bash
uv run tradescrapr plan jobs/eu3_hs6_annual.yaml    # create job + tasks (idempotent)
uv run tradescrapr run                              # work the queue; running IS resuming
uv run tradescrapr status --verbose                 # progress, quota burn, dead tasks
uv run tradescrapr retry-dead                       # revive dead tasks after fixing causes
```

Planning is idempotent (the job id hashes the spec), and with `reporters: all`
only reporter-periods that actually exist are enqueued. One task = one
reporter × period × flow at the requested commodity detail; oversized responses
split automatically. `run` accepts `--job`, `--max-calls`, `--stop-after HOURS`,
and `--no-wait-quota` (exit instead of sleeping at quota exhaustion).

## Command reference

| Command | Purpose |
|---|---|
| `tradescrapr` / `tradescrapr new` | Open the interactive job builder |
| `tradescrapr refs refresh` | Refresh cached reporter, partner, and HS reference data |
| `tradescrapr plan jobs/<spec>.yaml` | Expand a job spec into persistent tasks |
| `tradescrapr plan jobs/<spec>.yaml --dry-run` | Preview task expansion without writing state |
| `tradescrapr run` | Work queued tasks, resuming any interrupted job |
| `tradescrapr status --verbose` | Show job/task progress and dead-task details |
| `tradescrapr retry-dead` | Requeue dead tasks after fixing the underlying issue |
| `tradescrapr monitor --dry-run` | Preview new/revised monthly releases |
| `tradescrapr monitor` | Enqueue and scrape new/revised monthly releases |
| `tradescrapr ui` | Open the live read-only dashboard |
| `tradescrapr da --period 202405 --reporter DEU` | Inspect Comtrade availability rows |

### Long unattended runs

```bash
nohup uv run tradescrapr run >> logs/nohup.log 2>&1 &
```

or, surviving reboots on Linux, `~/.config/systemd/user/tradescrapr-run.service`:

```ini
[Unit]
Description=tradescrapr targeted worker

[Service]
Type=exec
WorkingDirectory=%h/main/projects/tradescrapr
Environment=COMTRADE_API_KEY=your-key-here
ExecStart=%h/main/projects/tradescrapr/.venv/bin/tradescrapr run
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now tradescrapr-run.service
loginctl enable-linger $USER   # keep user services alive after logout
```

## Monitor mode

```bash
uv run tradescrapr monitor --dry-run    # show the new/revised release diff
uv run tradescrapr monitor              # scrape it
```

Each run diffs the free availability listing against the local `releases`
table: unseen reporter-periods are new; a newer `lastReleased` timestamp is a
revision (partition wiped and re-scraped, stale queued tasks cancelled). A
release is only marked done when every one of its tasks finished, so
interrupted runs resume on the next scheduled tick. Scope lives in the
`monitor:` block of `config.yaml` — `start_period` bounds the first-run
backfill.

Schedule it daily (releases trickle in all month). Linux crontab:

```cron
17 6 * * * cd $HOME/main/projects/tradescrapr && /usr/bin/flock -n state/monitor.cron.lock .venv/bin/tradescrapr monitor >> logs/cron.log 2>&1
```

(macOS scheduling below.)

## Dashboard

```bash
uv run tradescrapr ui            # live full-screen dashboard (q quits, r refreshes)
uv run tradescrapr ui --once     # one static snapshot (pipe-friendly)
```

An htop-style view: quota gauge with a 7-day burn sparkline and time-to-reset,
per-job progress bars with ETA, what the worker is fetching right now, the
monitor's release backlog by period, a live color-coded activity feed, and dead
tasks front and center. It is a **read-only observer** — run it in a second
tmux pane or SSH session while a worker is going; the header shows whether a
worker currently holds the lock.

## Data layout & provenance

```
data/raw/comtrade/{final|tariffline}/freq={A|M}/period={p}/reporter={code}/
    part-{task_id}[-{seq}].parquet
    part-{task_id}[-{seq}].parquet.manifest.json
```

Read it with `pl.scan_parquet("data/raw/comtrade/final/**/*.parquet")` — the
hive keys become columns. Every `*Code` column and `period` is pinned to
string (HS codes keep leading zeros); values are Float64. Each chunk's manifest
records the source URL, query parameters, row count, sha256, timestamp, and
citation — enough to audit or rebuild any partition.

## Configuration

`config.yaml` at the project root; every key optional. Precedence:
CLI flags > `TRADESCRAPR_*` env vars > `config.yaml` > defaults.

| Block | Highlights |
|---|---|
| `tier` | `free` (default) or `premium` |
| `api` | `rps`, `max_records`, `timeout_s` (defaults follow the tier) |
| `quota` | `daily_budget` (450 free), `reset_time`/`reset_tz` (00:00 UTC) |
| `retry` | transport retries, task max attempts, backoff, split depth |
| `planner` | `max_cmd_codes_per_call`, `combine_flows` |
| `monitor` | datasets, freq, `cmd_level`, `breakdown`, `start_period`, reporter filter |
| `paths` | data/state/logs locations |

## macOS

Everything works identically on macOS — locking, signals, atomic writes, and
the dashboard are plain POSIX, and CI runs the full suite on `macos-latest`.
Two platform notes:

- **Scheduling**: macOS has no `/usr/bin/flock` binary and launchd is the
  native scheduler. Either drop the `flock -n ...` prefix from the crontab line
  (safe — the app-level `state/worker.lock` already prevents overlapping runs),
  or use a LaunchAgent at `~/Library/LaunchAgents/org.tradescrapr.monitor.plist`:

  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
    <key>Label</key><string>org.tradescrapr.monitor</string>
    <key>WorkingDirectory</key><string>/Users/YOU/main/projects/tradescrapr</string>
    <key>EnvironmentVariables</key>
    <dict><key>COMTRADE_API_KEY</key><string>your-key-here</string></dict>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/YOU/main/projects/tradescrapr/.venv/bin/tradescrapr</string>
      <string>monitor</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>17</integer></dict>
    <key>StandardOutPath</key><string>/Users/YOU/main/projects/tradescrapr/logs/cron.log</string>
    <key>StandardErrorPath</key><string>/Users/YOU/main/projects/tradescrapr/logs/cron.log</string>
  </dict>
  </plist>
  ```

  Load with `launchctl load ~/Library/LaunchAgents/org.tradescrapr.monitor.plist`.
  (A `.env` file at the project root works too, since `WorkingDirectory` is set.)

- **Laptop sleep**: wrap long foreground runs in `caffeinate -i uv run
  tradescrapr run` so idle sleep doesn't pause the worker. Interrupted runs
  resume anyway; this just avoids the pause.

## Recovery cheatsheet

| Symptom | Action |
|---|---|
| VM rebooted / process killed | just `run` again — resume is the default |
| `status --verbose` shows dead tasks | read `last_error`; `retry-dead` after fixing |
| Quota confusion after key change | server 403s resync the ledger automatically |
| Fresh scrape of a partition wanted | re-`plan` an edited spec (new job id), or delete the job's rows and partition dir |
| Two workers by accident | impossible: `state/worker.lock` (flock) is exclusive |

Never run two workers against different checkouts sharing one `state/` — the
lock is per-file. `data/`, `state/`, `logs/`, and `.env` are gitignored; back up
`state/` alongside `data/` when archiving a scrape (it is the record of what
was fetched when).

## Development

```bash
uv run pytest              # full suite, no network needed
uv run ruff check src tests
uv run ruff format src tests
```

CI (GitHub Actions) runs lint + tests on Ubuntu and macOS for every push and PR.
