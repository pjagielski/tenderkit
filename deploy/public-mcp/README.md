# Shared read-only MCP server

`setup.sh` is an example of serving one tenderkit database to many MCP clients with `tenderkit mcp --read-only --http`. It is how [mcp.tenderkit.pl](https://mcp.tenderkit.pl/mcp) runs. It is not part of the Python package, and it is not a supported installer. Read it before you run it.

## What it assumes

- An Ubuntu host with Python 3.11 or newer, `git`, nginx and certbot. Port 80 serves the certbot webroot (`WEBROOT`, default `/var/www/html`).
- A DNS name that already points to the host (`DOMAIN`).
- Root access to run it once. The services themselves run as an unprivileged user.

## What it sets up

- The system user `tenderkit` with `/srv/tenderkit`, a virtual environment and `tenderkit[mcp]` installed from a git ref (`REF`, default `master`).
- `tenderkit-mcp.service` serves the database read-only on `127.0.0.1:$PORT` (default 8810). It is sandboxed and limited to 512 MB of memory and 60% of one CPU.
- `tenderkit-fetch.timer` fetches every 8 hours all BZP and TED notices with results from the last 3 days, without the full source records (`--no-raw`).
- `tenderkit-backfill.service` runs once over the last `BACKFILL_DAYS` (default 90), newest window first. BZP uses 14-day windows. TED uses 5-day windows, because the TED adapter reads award details for at most 1000 result notices per window.
- An nginx site for `DOMAIN`:
  - Let's Encrypt certificate;
  - at most 60 requests a minute per IP, with a burst of 30;
  - 5 connections per IP;
  - a 64 KB request body;
  - `/` redirects to tenderkit.pl.

## Run

```sh
sudo DOMAIN=mcp.example.org REF=<commit> bash setup.sh
```

Set `SSH_KEYS_FROM=<user>` to copy that user's `authorized_keys` to `tenderkit`, so you can maintain it without root.

The script can be run again with a new `REF`. It reinstalls tenderkit, rewrites its scripts and units, and reloads nginx. It starts the backfill only while the database does not exist yet.

A full 90-day database of compact records takes about 0.4 GB. It grows by about 0.13 GB a month, because old notices are not removed.

## Maintain

- Logs:
  ```sh
  journalctl -u tenderkit-mcp -u tenderkit-fetch -u tenderkit-backfill
  ```
- Coverage, the windows and failures of each fetch: the `tenderkit://coverage` resource, or the `tenderkit status` command run as `tenderkit`.
- Upgrade without root, as `tenderkit`:
  1. Run `pip install --force-reinstall --no-deps "tenderkit @ git+https://github.com/pjagielski/tenderkit@<commit>"` in `/srv/tenderkit/venv`.
  2. Stop the server's main process (`systemctl show -p MainPID tenderkit-mcp`). systemd starts it again with the new code.
