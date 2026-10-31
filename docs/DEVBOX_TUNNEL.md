# The Pond tunnel to a dev box (optional)

How a Pond on a Linux host reaches pondNT on a Windows dev box it cannot dial
into. The Windows machine opens the connection outward, so the remote Pond
can reach selected development services without exposing their ports publicly.
For local Visual Studio use, [pondNT setup](INSTALL.md) is enough; no tunnel
is needed.

These helpers ship in every release as `extras\pond-tunnel\` (this page as
their README). The tunnel has its own service identities, ports and process
receipts; it does not convert an existing local Ferry setup. Start at
[Set up](#set-up), verify [the connection](#check), and use
[Tear down](#tear-down) to remove only this route.

## Our example

The Pond family's [example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md):
the
Crocodile at `crocodile-url.example.test`, listening on `127.0.0.1:8503`, with
the Pond `pond`; you have an account there (`you` below). The Windows dev
box is `devbox`, its public address `203.0.113.7`, its key comment
`pondnt-devbox-2026`.

## How it works

The box dials out over SSH (the only way in) as the account `pondnt-tunnel`
and holds these forwards open:

| Forward | Direction | What travels |
|---|---|---|
| `-R 127.0.0.1:18520` | host to box | Pond calls to the service `devbox` (boring-pondnt-core) |
| `-R 127.0.0.1:18522` | host to box | Pond calls to `devbox-d365` (D365 with its xref tools) |
| `-R 127.0.0.1:18524` | host to box | Pond calls to `devbox-d365whsapp` ([warehouse-flow tests](D365WHSAPP.md)) |
| `-R 127.0.0.1:18526` | host to box | Pond calls to `devbox-d365web` ([browser automation](D365WEB.md)) |
| `-L 127.0.0.1:18503 -> 127.0.0.1:8503` | box to host | the box's servers check call tokens at the Crocodile |

`pondnt-tunnel` has no shell, no sudo and no groups; its key may do exactly
these forwards, only from the box's address. Closing the tunnel's window on
the box cuts the Pond off.

## Set up

Prerequisites: the extracted pondNT release, Windows OpenSSH client, a Linux
host with the Crocodile and SSH, and operator access to both machines. Run the
Windows helper as the intended elevated service account.

Create a dedicated `C:\temp\ci\ssh` directory before generating the key.
The launcher checks the whole `C:\temp\ci` tree: its owner and every allowed
ACL entry must be the current account, SYSTEM or Administrators, with no
reparse points. Provision those private permissions on this dedicated tree;
the helper refuses broader inherited access and does not silently repair ACLs.
`-CiRoot` selects another working folder when needed; use it consistently.

1. **Box: a key and the host's identity.** Generate a new key at an unused path:
   `ssh-keygen -t ed25519 -N "" -C pondnt-devbox-2026 -f C:\temp\ci\ssh\id_ed25519`.
   Also provision `C:\temp\ci\ssh\known_hosts` with the tunnel host's verified
   SSH host key. Confirm its fingerprint through the host operator or another
   trusted route; merely downloading a key does not verify it. Both files are
   required before the launcher starts the tunnel.
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
   Optional: `devbox-d365whsapp` (18524) and `devbox-d365web` (18526) the same way, e.g.
   `sudo boring-crocodile-admin service-add devbox-d365whsapp --url http://127.0.0.1:18524/mcp --ponds pond`;
   `claim-services` replaces the list, so name every service: `--services devbox devbox-d365 devbox-d365whsapp devbox-d365web`.
   **Another Pond as caller** (for example a Pond named `research`): the
   Crocodile grant and the backend's caller policy must both allow it.
   On the host, per service, the complete set of callers (plan, then `--apply --approve-digest`):
   `sudo boring-crocodile-admin service-grants devbox --callers pond,research`, and the same for
   devbox-d365, devbox-d365whsapp, devbox-d365web; then `claim-services` again with every service, so the
   receipt holds the new definitions. On the box, next to the credentials:
   `C:\temp\ci\devbox-callers.json` = `[{"subject": "research", "role": "writer"}]` (role `reader` or
   `writer`; `pond` is always a writer and is not listed), then restart the devbox services. Without the
   file the services accept `pond` only, as before.
4. **Credentials to the box, by the operator by hand** (never through the
   tunnel or a Pond): `C:\temp\ci\devbox-id`, `devbox-secret`,
   `devbox-d365-id`, `devbox-d365-secret`.
5. **Box: start**, in an elevated PowerShell, from the extracted release:

       .\devbox-start.ps1 -Release <unpacked-release> -TunnelHost pondnt-tunnel@crocodile-url.example.test -ControlPlane 127.0.0.1:8503

   Run the script from `extras\pond-tunnel\`; `-Release` names the release
   root containing `python\python.exe`. It starts the tunnel process, then
   each server with provisioned credentials and any required configuration.
   `-Only devbox-d365` restarts just that server and leaves the tunnel alone.

   The helper records each process in `devbox-processes.json`; stop and restart
   check PID, start time, executable and command line, leaving unrecorded
   processes alone. "Started and recorded" is process evidence, not a working
   network or authenticated service call: complete [Check](#check).

6. **Optional: reconnect after sleep or a dropped network.** The SSH client sends a
   keepalive every 30 s and ends after three missed answers, so a dead
   session (the box or its host slept, the network dropped) ends within about
   90 s. A separately configured repeating task can dial again: every two minutes it runs
   `devbox-start.ps1 -EnsureTunnel` with the same tunnel arguments, which
   starts the tunnel only when its recorded process has ended and leaves a
   running tunnel and every server alone. The helper does not create scheduled
   tasks. If the operator has already configured a tunnel task named
   `\pondNT-tunnel\devbox tunnel`, the following example creates a companion
   task using its account and arguments. Inspect that existing task first;
   this example depends on it and on a working Task Scheduler:

       $old = Get-ScheduledTask -TaskPath '\pondNT-tunnel\' -TaskName 'devbox tunnel'; $a = New-ScheduledTaskAction -Execute powershell.exe -Argument ($old.Actions[0].Arguments + ' -EnsureTunnel'); $t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2); $s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable; Register-ScheduledTask -TaskPath '\pondNT-tunnel\' -TaskName 'devbox tunnel ensure' -Action $a -Trigger $t -Settings $s -Principal $old.Principal

   Only the tunnel's own arguments may be on the old task's line; `-Only`
   and `-Stop` are refused with `-EnsureTunnel`. Remove it with
   `Unregister-ScheduledTask -TaskPath '\pondNT-tunnel\' -TaskName 'devbox tunnel ensure'`.

## Check

- Host: `ss -tln | grep -E ':1852[02]'` shows the two listeners while the
  tunnel is up, none when it is down.
- Host: `sudo sshd -T -C user=pondnt-tunnel,host=x,addr=203.0.113.7` shows the
  allowed forwards.
- In `pond`: `service_list` shows `devbox` and `devbox-d365`; a read-only call
  answers.
- If reconnect scheduling was configured: test a sleep/network interruption.
  The keepalive and two-minute schedule normally allow another connection
  attempt within about four minutes. Confirm an authenticated read succeeds;
  a scheduled attempt does not guarantee that the network or service is ready.
- Logging in as `pondnt-tunnel` with a shell or a command fails.

## Tear down

First disable or remove any operator-created reconnect/start tasks for this
tunnel, so they cannot reopen it during removal. On the box,
`.\devbox-start.ps1 -Stop` stops the recorded tunnel and servers; pass the same
`-CiRoot` if you used a different working folder. Then on the host:

    sudo bash devbox-teardown.sh [--restore-source]

It removes only what the receipt owns: the adopted service registrations
(each checked again inside the removal; a changed one stays), the account and
its key; `--restore-source` puts the key line back into your
`authorized_keys`. A failed step leaves its receipt pending: look at the
process, account, key or service it names, then run the same command again.
An uncertain Windows start or stop is never replayed.

If this installation used `devbox-defender.ps1`, its `-Undo` uses the separate
`devbox-defender-record.json` receipt; see [INSTALL](INSTALL.md). Defender does
not record who added an exclusion, so inspect the live list before undoing one
that another installation may share.

Retain the process and teardown receipts until removal is confirmed. Then
review the dedicated working folder for disposal. A source index, local
Visual Studio configuration or browser profile may belong to another route;
removing this tunnel is not permission to delete those shared resources.

## Adding d365whsapp / d365web to an existing tunnel

The box's key line must permit all four configured listen ports first: with
`ExitOnForwardFailure`, a tunnel that asks for 18524/18526 without that permission
fails as a whole.

1. Host, as root, from this release's `extras/pond-tunnel`: run the setup step
   again with exactly the original inputs (`--source-keys`, `--comment`,
   `--box`, `--control-plane`). It republishes the key line, now with
   `permitlisten="127.0.0.1:18524"` and `"127.0.0.1:18526"`. Check:
   `sudo grep -c 18526 /home/pondnt-tunnel/.ssh/authorized_keys` prints 1.
2. Host: `service-add` per service as above and `claim-services` with every name.
3. Box: `<service>-id`, `<service>-secret` and the service's `d365whsapp.json` / `d365web.json` into `C:\temp\ci`;
   a service without its .json is not started.
4. Box: restart the tunnel task with this release's `devbox-start.ps1`.
