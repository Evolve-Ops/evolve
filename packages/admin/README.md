# evolve-admin

Admin CLI and local web UI for the Evolve network. Runs as **admin** — the privileged user that can deploy to all bot home directories and manage launchd jobs.

## Install

```bash
# From admin:
cd /path/to/evolve/packages/admin
pip3 install -e .
```

## Running the Web UI

### Persistent (recommended)

Install the admin server as a macOS launchd service — starts at login, restarts on crash, no terminal required:

```bash
evolve-admin service install       # install + start
evolve-admin service status        # check running state / PID
evolve-admin service logs          # tail the server log
evolve-admin service restart       # restart (also available as a button in the UI)
evolve-admin service stop
evolve-admin service uninstall
```

The service runs as the current user and logs to `~/.evolve/logs/admin-server.log`.

### Manual (one-off)

```bash
evolve-admin serve                 # http://127.0.0.1:5050
evolve-admin serve --open          # opens browser automatically
evolve-admin serve --host 0.0.0.0  # all interfaces (Tailscale etc.)
```

## SSH Tunnel (laptop access)

The server binds to `127.0.0.1` only. To access from your laptop, use the **Setup Wizard** in the UI (Maintenance → Setup tab) — it generates a downloadable `.command` script that installs a persistent autossh tunnel as a launchd agent on your laptop.

One-shot (manual):
```bash
ssh -L 5050:localhost:5050 <user>@mini
```

## Browser Shortcut

Type `evolve` in your browser address bar instead of `http://localhost:5050`:

- **Chrome/Arc:** Settings → Search Engines → Add → Keyword: `evolve`, URL: `http://localhost:5050`
- **Firefox:** Bookmark the page, right-click → Properties → Keyword: `evolve`
- **Safari:** File → Add to Dock…

## CLI Reference

```bash
# Network health
evolve-admin status

# Deploy / manage bots
evolve-admin deploy --bot bot2 --role member
evolve-admin deploy --all
evolve-admin deploy --bot bot2 --dry-run

# Lifecycle (see `evolve-admin lifecycle --help` for the full taxonomy)
sudo evolve-admin detach-bot bot5    # stop Evolve daemons; bot keeps running
sudo evolve-admin retire-bot bot5    # graceful: archive + stop daemons + remove
sudo evolve-admin delete-bot bot5    # irreversible full removal

# Shared directory
evolve-admin setup-shared

# Config
evolve-admin config show
evolve-admin config set-primary bot1
evolve-admin config set-alert --chat-id YOUR_TELEGRAM_CHAT_ID

# Custom network config path
evolve-admin --network /path/to/network.json status
```

## Security

- Binds to `127.0.0.1` only — never expose externally
- Runs as the admin user; has access to all bot workspaces via shared directory
- No authentication layer — access control is at the OS level
- Access from a laptop requires an explicit SSH tunnel (see above)
