# Edge Daemon Service Templates

Copy-ready templates for running `botex edge --edge-daemon` as a supervised
service. The daemon holds a per-host singleton lock, so exactly one instance
should run per user account; both templates below use restart-on-failure
supervision and a graceful stop window longer than the daemon's 10 s drain
timeout.

## Linux (headless / VPS): systemd user unit

Create `~/.config/systemd/user/botex-edge.service`:

```ini
[Unit]
Description=BoteX edge host daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/botex edge --edge-daemon --server wss://<cloud-host>/api/edge/ws
Restart=on-failure
RestartSec=5
# > 10 s so the daemon's SIGTERM drain is never killed mid-flight.
TimeoutStopSec=15
# Optional: keep provider keys out of the process environment entirely.
# The daemon itself needs no provider credentials.
Environment=BOTEX_DISABLE_DOTENV=1

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now botex-edge
journalctl --user -u botex-edge -f   # follow logs
```

Notes:
- `%h` expands to the invoking user's home directory; adjust `ExecStart` if
  `botex` is installed elsewhere (`command -v botex`).
- For a headless VPS where the unit must survive logout, run
  `sudo loginctl enable-linger <user>` once.
- On SIGTERM (systemd stop) the daemon drains in-flight work for up to 10 s
  and exits 0; `TimeoutStopSec=15` leaves headroom.

## Windows 11: Task Scheduler (restart on failure)

Task Scheduler XML — save as `botex-edge.xml` and import, or use the
`Register-ScheduledTask` snippet below. The task runs at user logon, restarts
on failure, and stops gracefully on logoff/shutdown.

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>255</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c botex edge --edge-daemon --server wss://&lt;cloud-host&gt;/api/edge/ws</Arguments>
    </Exec>
  </Actions>
</Task>
```

PowerShell import (per-user, no admin needed):

```powershell
Register-ScheduledTask `
  -TaskName 'BoteX Edge Daemon' `
  -Xml (Get-Content -Raw "$HOME\botex-edge.xml") `
  -TaskPath '\'
```

Or register inline without an XML file:

```powershell
$action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c botex edge --edge-daemon --server wss://<cloud-host>/api/edge/ws'
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$settings  = New-ScheduledTaskSettingsSet `
  -RestartCount 255 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName 'BoteX Edge Daemon' `
  -Action $action -Trigger $trigger -Settings $settings
```

Notes:
- Interactive `Ctrl+C` (`SIGINT`) triggers the 10 s graceful drain. Task Scheduler
  hard-stop actions rely on `WindowsJobGuard` (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`)
  to reap child harness process trees cleanly when the daemon exits.
- Only one instance per user account may hold the daemon lock; a second
  `botex edge --edge-daemon` invocation fails closed with a
  `lock_contention` error instead of racing the running daemon.
