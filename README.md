# Закъснява ли?

Static website tracking Sofia public transit reliability from live GTFS/GTFS-RT feeds.

See `SPEC.md` for the full specification and `docs/internal/RECON.md` for confirmed feed facts.

## Development

```bash
uv sync --dev                        # install Python deps
uv run pytest                        # run tests
uv run ruff check .                  # lint
uv run ruff format .                 # format
uv run bandit -r collector pipeline  # security scan

cd site && npm ci                    # install site deps
cd site && npm run build             # build site (requires public/data/*.json)
```

Pipeline scripts:

```bash
python pipeline/build_stop_events.py --date YYYY-MM-DD
python pipeline/compute_metrics.py --month YYYY-MM [--gtfs PATH]
```

## Architecture

Two scheduled processes on a VPS:

- **`collector/collector.py`** — polls GTFS-RT feeds every 20s, writes compressed snapshots
- **`ops/nightly.sh`** — triggered at 03:10 Europe/Sofia by systemd timer; runs pipeline and deploys site

See `SPEC.md §5` for repository layout and `SPEC.md §8` for observability.

---

## VPS Setup

Tested on Ubuntu 22.04 LTS. Minimum: 1 vCPU, 1 GB RAM, 40 GB disk.

### 1. System packages

```bash
sudo apt update
sudo apt install -y git nodejs npm curl
```

### 2. Create service user and directories

```bash
# Create service user with a home dir (needed for SSH keys)
sudo mkdir -p /var/lib/zakasnyava-li/home
sudo useradd -r -s /usr/sbin/nologin \
    -d /var/lib/zakasnyava-li/home zakasnyava

sudo mkdir -p /opt/zakasnyava-li \
    /var/lib/zakasnyava-li/data/derived \
    /var/lib/zakasnyava-li/data/gtfs \
    /etc/zakasnyava-li

sudo chown -R zakasnyava:zakasnyava \
    /opt/zakasnyava-li /var/lib/zakasnyava-li
```

### 3. Clone and install

```bash
sudo -u zakasnyava git clone https://github.com/coldsoul/zakasnyava-li.git \
    /opt/zakasnyava-li

# Install uv as a standalone binary for the service user
sudo -u zakasnyava -H sh -c \
    'curl -LsSf https://astral.sh/uv/install.sh | sh'

# uv sync creates .venv and installs all Python deps
sudo -u zakasnyava /var/lib/zakasnyava-li/home/.local/bin/uv sync \
    --project /opt/zakasnyava-li

cd site && sudo -u zakasnyava npm ci
```

### 4. SSH deploy key (GitHub Pages push)

The nightly pipeline pushes `site/dist/` to the `gh-pages` branch. The
`zakasnyava` user needs write access to the repo via an SSH deploy key.

**On the VPS:**

```bash
# Generate deploy key (no passphrase — runs unattended in systemd)
sudo -u zakasnyava ssh-keygen -t ed25519 \
    -f /var/lib/zakasnyava-li/home/.ssh/gh_deploy \
    -C "zakasnyava-li nightly deploy" \
    -N ""

# Configure SSH to use this key for github.com
sudo -u zakasnyava tee /var/lib/zakasnyava-li/home/.ssh/config > /dev/null <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile /var/lib/zakasnyava-li/home/.ssh/gh_deploy
  StrictHostKeyChecking yes
EOF
sudo chmod 600 /var/lib/zakasnyava-li/home/.ssh/config

# Print the public key — you'll paste this into GitHub next
sudo cat /var/lib/zakasnyava-li/home/.ssh/gh_deploy.pub
```

**On GitHub** — `Settings → Deploy keys → Add deploy key`:
- Title: `vps-nightly-deploy`
- Key: paste the public key printed above
- Check **Allow write access** ✓
- Click **Add key**

**Back on the VPS** — seed GitHub's host key, switch remote to SSH, and verify:

```bash
# Seed GitHub's ED25519 host key (StrictHostKeyChecking refuses unknown hosts)
sudo -u zakasnyava ssh-keyscan -t ed25519 github.com \
    >> /var/lib/zakasnyava-li/home/.ssh/known_hosts

# Verify fingerprint matches GitHub's published key:
# SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
ssh-keygen -lf /var/lib/zakasnyava-li/home/.ssh/known_hosts

# Switch from HTTPS to SSH remote
sudo -u zakasnyava git -C /opt/zakasnyava-li remote set-url origin \
    git@github.com:coldsoul/zakasnyava-li.git

# Test — should print "Hi coldsoul/zakasnyava-li! You've successfully authenticated"
sudo -u zakasnyava ssh -T git@github.com
```

### 5. Secrets file

Create `/etc/zakasnyava-li/secrets.env` (not committed to git, mode 640):

```bash
sudo tee /etc/zakasnyava-li/secrets.env > /dev/null <<'EOF'
# Dead man's switch — ping on every successful nightly run
# Free check at https://healthchecks.io or self-hosted Uptime Kuma
DEADMAN_URL=https://hc-ping.com/YOUR-UUID-HERE

# ntfy.sh topic for failure notifications (used by notify@.service)
NTFY_URL=https://ntfy.sh/YOUR-TOPIC-HERE
EOF
sudo chmod 640 /etc/zakasnyava-li/secrets.env
sudo chown root:zakasnyava /etc/zakasnyava-li/secrets.env
```

### 6. Install systemd units

```bash
sudo cp /opt/zakasnyava-li/ops/systemd/*.service /etc/systemd/system/
sudo cp /opt/zakasnyava-li/ops/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Collector — starts immediately, restarts on failure
sudo systemctl enable --now collector.service

# Nightly pipeline — controlled by the timer
sudo systemctl enable --now nightly.timer

# Verify timer is scheduled
systemctl list-timers nightly.timer
```

### 7. Verify installation

```bash
# Collector is running
sudo systemctl status collector.service

# Timer shows next scheduled run
systemctl list-timers --all | grep nightly

# Test nightly script manually (suppresses dead man's switch ping)
sudo -u zakasnyava DEADMAN_URL="" /opt/zakasnyava-li/ops/nightly.sh

# Check prom metrics were written
cat /var/lib/node_exporter/textfile_collector/nightly.prom

# Verify gh-pages branch was pushed
sudo -u zakasnyava git -C /opt/zakasnyava-li ls-remote origin gh-pages
```

### 8. Disk monitoring

Add a systemd alert for disk usage ≥ 90 %:

```bash
sudo tee /etc/systemd/system/disk-alert.service > /dev/null <<'EOF'
[Unit]
Description=Disk usage alert

[Service]
Type=oneshot
EnvironmentFile=-/etc/zakasnyava-li/secrets.env
ExecStart=/usr/bin/bash -c '\
    PCT=$(df /var/lib/zakasnyava-li --output=pcent | tail -1 | tr -d " %"); \
    [ "$PCT" -ge 90 ] && \
    curl -fsS -d "Disk at ${PCT}%% on $(hostname)" "$NTFY_URL" || true'
EOF

sudo tee /etc/systemd/system/disk-alert.timer > /dev/null <<'EOF'
[Unit]
Description=Disk usage alert timer

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload && sudo systemctl enable --now disk-alert.timer
```

### 9. Prometheus metrics (optional)

Skip this section if not running Prometheus.

Install and configure node_exporter to expose nightly pipeline metrics:

```bash
sudo apt install -y prometheus-node-exporter

sudo mkdir -p /var/lib/node_exporter/textfile_collector
sudo chown zakasnyava:zakasnyava /var/lib/node_exporter/textfile_collector

sudo systemctl edit prometheus-node-exporter
```

Add to the override:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus-node-exporter \
    --collector.textfile.directory=/var/lib/node_exporter/textfile_collector
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart prometheus-node-exporter
```

Alert rules — add to `alerts.yml`:

```yaml
groups:
  - name: zakasnyava
    rules:
      - alert: NightlyPipelineStale
        expr: time() - nightly_last_success_timestamp_seconds > 100000
        for: 5m
        annotations:
          summary: "Nightly pipeline has not succeeded in 28+ hours"

      - alert: CollectorFeedStale
        expr: >
          (time() - collector_last_feed_header_timestamp_seconds > 600)
          and on() (hour() >= 5 < 24)
        for: 5m
        annotations:
          summary: "GTFS-RT feed not updated 10+ min during service hours"
```

### 10. Updating

```bash
cd /opt/zakasnyava-li
sudo -u zakasnyava git pull
sudo -u zakasnyava .venv/bin/uv sync
cd site && sudo -u zakasnyava npm ci
sudo systemctl restart collector.service
# nightly runs on next timer tick — no restart needed
```

### Secrets never committed

`ops/secrets.env` is in `.gitignore`. Live secrets live only in
`/etc/zakasnyava-li/secrets.env` on the VPS.
