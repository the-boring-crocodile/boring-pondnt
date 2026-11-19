# The Pond tunnel (optional)

The tunnel gives approved Pond callers access to servers on a disposable
D365 development box. Keep it away from production and grant core access
only to fully trusted callers. Even read access can expose credentials.
The Windows launcher and its private state are separate from regular
pondNT setup and Visual Studio.

## First setup of this tunnel

Use the Windows account that will run the tunnel. Create a private
`%LOCALAPPDATA%\pond-tunnel\credentials` directory with access restricted
to that account and trusted administrators/SYSTEM. From the elevated desktop
PowerShell, generate a key at an unused path:

    ssh-keygen -t ed25519 -C pondnt-devbox -f "$env:LOCALAPPDATA\pond-tunnel\credentials\ssh-key"

With the OpenSSH launcher in batch mode, the dedicated key must be usable
without prompting. Keep its private file on Windows and send only the public
`.pub` file to the Linux administrator. Obtain and verify the Linux SSH
host key through a trusted channel and write it to
`credentials\known_hosts` in the private directory.

The administrator installs the box in one command from the Linux guide.
Its private Windows handoff contains `settings.json` and service credentials.
Transfer them privately into `%LOCALAPPDATA%\pond-tunnel` alongside the
desktop SSH files. The handoff supplies the Crocodile's public HTTPS URL and
selects each server's name and matching loopback port on both hosts. Windows
needs to reach that HTTPS URL for token checks and registration. Choose unused
ports on both hosts. [Linux install and removal](https://github.com/the-boring-crocodile/boring-services/blob/main/tunnel/README.md)

### Windows: choose servers and their own D365 state

Keep the server names and ports from the Linux handoff. For a selected
warehouse or data server, fill in `settings_file` with its own configuration
path. Prepare application secrets as the same Windows person who will run
this window. See [D365WHSAPP](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/D365WHSAPP.md)
and [D365DATA](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/D365DATA.md).

For the browser server, add the `d365web.web` object from
`settings.example.json` with this box's AOS URL. Choose an unused debug port
and retain the tunnel's own Edge profile under `servers\<box>-d365web\edge`.
Start the tunnel, make a read-only `web_state` call to open Edge, and sign in
once in that profile. Later browser calls reuse the sign-in.

## Start and stop

From an elevated PowerShell on the Windows desktop, in the unpacked pondNT
release:

    .\extras\pond-tunnel\pond-tunnel.cmd

The window starts the selected servers once. SSH reconnects after a network
drop while the servers stay running. An occupied port or a server exit is
reported in the window. Close the window to stop SSH and its servers, including
children contained in the Windows Job Object. To remove access, close the
window and follow the Linux component's uninstall procedure.
[Linux install and removal](https://github.com/the-boring-crocodile/boring-services/blob/main/tunnel/README.md)

## Change caller access

On the Linux host, give the research Pond reader access to the D365 specialist
while keeping the existing writer:

    sudo /opt/boring-crocodile/current/bin/boring-crocodile-admin service-grants devbox-d365 --callers pond=writer,research=reader

Review the complete grant list. Apply the reviewed map and type `APPLY`.

    sudo /opt/boring-crocodile/current/bin/boring-crocodile-admin service-grants devbox-d365 --callers pond=writer,research=reader --apply

From the research Pond, make one harmless authenticated read against
`devbox-d365`. Windows reads the role from introspection, and grant changes
take effect within the token cache lifetime.

## Move from the previous tunnel

Close the previous window or stop its tasks and processes using the previous
release. Keep its release, private settings and server data for rollback.
Install the new Linux component and use its handoff for the new Windows
settings. Carry over only the desktop application settings and state needed
by the selected servers. The retired `control_plane` and `control_port`
fields are replaced by `crocodile_url`. Crocodile service grants replace the
Windows `access` list.
