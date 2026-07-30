# Remote operator access — any browser, any client OS

The admin UI is a browser app served on the pod host's loopback
(`127.0.0.1:5050`) and **never exposed to the network** (threat-model §1.1/§2:
SSH access to the box is the operator credential; loopback-as-authorization is
unchanged by remoteness). To operate a pod from another machine, you bring the
loopback port to your client over an SSH tunnel — or publish it to your
private tailnet with `tailscale serve`.

The operator's *client* machine is a separate axis from the pod *host*
(design-linux-port §1): any modern browser on **macOS, Windows, or Linux** is
a supported operator client. Evolve never requires owning Apple hardware on
the client end — a Windows laptop administering a Linux VPS pod is a fully
supported topology.

Replace `pod-admin-user@<pod-host>` below with your SSH login on the pod host
(any sudo-capable account on a Linux host; the admin account on a Mac).

## SSH tunnel, per client OS

**macOS / Linux** (Terminal):

```bash
ssh -N -L 5050:127.0.0.1:5050 pod-admin-user@<pod-host>
```

**Windows 10+** (PowerShell — the OpenSSH client ships with Windows, so the
command is identical):

```powershell
ssh -N -L 5050:127.0.0.1:5050 pod-admin-user@<pod-host>
```

Then open `http://127.0.0.1:5050` in your browser. Leave the `ssh` process
running for the length of your session (`-N` means it forwards ports and
nothing else). For a persistent auto-reconnecting tunnel from a macOS/Linux
client, `evolve-admin connect --host <pod-host>` installs one (see the
Quick Start, Step 1).

## Tailscale alternative

If the pod host and your client are on the same tailnet, run on the **pod
host**:

```bash
tailscale serve --bg 5050
```

This publishes the loopback UI at the HTTPS URL the command prints —
reachable from tailnet devices only, with the admin server still bound to
loopback. Do **not** use `tailscale funnel` (that is public-internet
exposure) and do not bind the server itself to a non-loopback address.

## Authentication

The tunnel gets your browser to the server; it does not log you in. Device
pairing is enforced by default (threat-model §6.1) — on first visit you'll
land on the pairing page; get a pairing code by running
`sudo evolve-admin pair` over SSH on the pod host.

## Pod-host platform notes

The pod *host* platform (macOS today; Ubuntu 24.04 feature-gated under
Phase 8.3) constrains which channels a pod can offer — not which clients can
operate it. The one channel affected: **iMessage requires a macOS pod host**
(an upstream OpenClaw constraint — the channel reads Messages.app's local
database), so Linux pods do not offer it in the Skills catalog. Upstream's
answer for iMessage on a Linux pod is an SSH-wrapped relay to an always-on
Mac; Evolve documents that option but does not build or support it — a second
host is outside the single-machine threat model (threat-model §7).
