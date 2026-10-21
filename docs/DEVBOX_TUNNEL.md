# The Pond tunnel to a dev box (optional)

How a Pond on a Linux host reaches pondNT on a Windows dev box it cannot dial
into. Not how pondNT is normally used: developers run the servers for Visual
Studio ([INSTALL](INSTALL.md)). The tunnel is how a Pond tests and uses pondNT
from outside. These helpers ship in every release as `extras\pond-tunnel\`
(this page as its README); nothing runs until an operator starts it.

## Our example

The example site of the Pond family (boring-pond `docs/example-site.md`): the
Crocodile at `crocodile-url.example.test`, listening on `127.0.0.1:8503`, with
the Pond `pond`; you have an account there (`you` below). The Windows dev
box is `devbox`, its public address `203.0.113.7`, its key comment
`pondnt-devbox-2026`.

## How it works

The box dials out over SSH (the only way in) as the account `pondnt-tunnel`
and holds three forwards open:

| Forward | Direction | What travels |
|---|---|---|
| `-R 127.0.0.1:18520` | host to box | Pond calls to the service `devbox` (boring-pondnt-core) |
| `-R 127.0.0.1:18522` | host to box | Pond calls to `devbox-d365` (D365 with its xref tools) |
| `-L 127.0.0.1:18503 -> 127.0.0.1:8503` | box to host | the box's servers check call tokens at the Crocodile |

`pondnt-tunnel` has no shell, no sudo and no groups; its key may do exactly
these forwards, only from the box's address. Closing the tunnel's window on
the box cuts the Pond off.

## Set up

1. **Box: a key** (in `C:\temp\ci`, the helpers' working folder):
   `ssh-keygen -t ed25519 -N "" -C pondnt-devbox-2026 -f C:\temp\ci\ssh\id_ed25519`.
2. **Host: the account.** Put the public key into your
   `authorized_keys` once, then, from `extras/pond-tunnel` as root:

       sudo bash tunnel-account.sh --source-keys /home/you/.ssh/authorized_keys \
           --comment pondnt-devbox-2026 --box 203.0.113.7 --control-plane 127.0.0.1:8503

   It creates `pondnt-tunnel`, moves exactly that key line there with the
   forwarding-only options, and keeps a private receipt in
   `/var/lib/boring-pondnt-tunnel`. Unrelated lines stay. An existing account
   is adopted only with `--adopt-account`, after its identity and sole key
   are checked; an account without a receipt is never claimed by name.
3. **Host: the services**, for the Pond:

       sudo boring-crocodile-admin service-add devbox --url http://127.0.0.1:18520/mcp --ponds pond
       sudo boring-crocodile-admin service-add devbox-d365 --url http://127.0.0.1:18522/mcp --ponds pond

   Each prints its client id and secret once. To let teardown remove them,
   adopt their exact definitions into the receipt:
   `sudo python3 tunnel_host.py claim-services --services devbox devbox-d365`.
   The helper runs the installed `<HOME>/current/bin/boring-crocodile-admin`
   (HOME from `/etc/boring-pond/homes.json`, else `/opt/boring-crocodile`);
   `--admin <absolute path>` names another. A missing, non-executable or
   not root-owned executable is refused before anything runs.
4. **Credentials to the box, by the operator by hand** (never through the
   tunnel or a Pond): `C:\temp\ci\devbox-id`, `devbox-secret`,
   `devbox-d365-id`, `devbox-d365-secret`.
5. **Box: start**, in an elevated PowerShell, from the extracted release:

       .\devbox-start.ps1 -Release <unpacked-release> -TunnelHost pondnt-tunnel@crocodile-url.example.test -ControlPlane 127.0.0.1:8503

   It opens the tunnel, then starts every server whose credentials are there
   (`-Only devbox-d365` for one). It records each process in
   `devbox-processes.json`; stop and restart check PID, start time, executable
   and command line, and leave unrecorded processes alone.

## Check

- Host: `ss -tln | grep -E ':1852[02]'` shows the two listeners while the
  tunnel is up, none when it is down.
- Host: `sudo sshd -T -C user=pondnt-tunnel,host=x,addr=203.0.113.7` shows the
  allowed forwards.
- In `pond`: `service_list` shows `devbox` and `devbox-d365`; a read-only call
  answers.
- Logging in as `pondnt-tunnel` with a shell or a command fails.

## Tear down

Close the tunnel window on the box first (`.\devbox-start.ps1 -Stop` stops the
recorded servers). Then on the host:

    sudo bash devbox-teardown.sh [--restore-source]

It removes only what the receipt owns: the adopted service registrations
(each checked again inside the removal; a changed one stays), the account and
its key; `--restore-source` puts the key line back into your
`authorized_keys`. A failed step leaves its receipt pending: look at the
process, account, key or service it names, then run the same command again.
An uncertain Windows start or stop is never replayed.

On the box (unless it is simply deleted): `devbox-defender.ps1 -Undo` (its own
receipt, `devbox-defender-record.json`; see INSTALL), then delete `C:\temp\ci`
and the source index folder. Defender does not record who added an exclusion,
so check the live list before undoing if someone else may share it.
