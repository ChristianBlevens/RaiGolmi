<!-- purpose: the owner's checklist for setting RaiGolmi up on a PC that must run it unattended for weeks, keeper included
not-here: installing for everyday use (README.md § Install it); what each setting does (README.md § The catalog and settings, § Phone alerts, § The keeper)
shape: bounded
audited: 0 2026-10-15
-->
# Running RaiGolmi unattended

What to do on a PC that will run RaiGolmi for weeks with nobody who can fix it. When it's set up,
the only thing anyone should need to do is turn the PC back on if it goes off.

## Windows, once

1. In the BIOS/UEFI: virtualization on, and **power on after AC loss** ("Restore on AC power
   loss: Power On").
2. **Automatic sign-in**: `netplwiz`, untick *Users must enter a user name and password*
   (or Sysinternals Autologon). Without it, nothing starts after a restart.
3. **Never sleep**: Settings → System → Power → *Sleep: Never* when plugged in. The screen may turn off.
4. **Windows Update**: set *active hours* to cover the whole day you can, or pause updates for
   the time away. A restart is survived, but each one costs the run a few minutes.
5. **Docker Desktop**: install it, then in its settings turn on *Start Docker Desktop when you
   sign in to your computer*. Sign in to Docker if it asks.

## RaiGolmi

6. Get the repo (`git clone https://github.com/ChristianBlevens/RaiGolmi`), close the RaiGolmi window
   if it is open, and run `setup.bat`.
   **Moving from another PC:** copy the old project folder's `disk\` across first (the disk
   holds everything the agents did); if the launcher asks where the disk is, point it at
   `disk\raigolmi.qcow2`.
7. On a fresh disk, do the three sign-ins the bottom terminal walks you through (README § The
   first start).
8. In the window's top-left **Settings**:
   - *Start RaiGolmi when I sign in to Windows*: on
   - *Keep Windows awake while RaiGolmi runs*: on
   - *Venus (Vulkan)*: on if the run needs the GPU
   - *Phone alerts*: your ntfy topic, then *Send a test alert* and check the phone
   - *Keeper*: on (the first time takes a few minutes while its image builds)
   - *Heartbeat*: at healthchecks.io (free account) create a check named *RaiGolmi PC is off:
     please turn it on, or restart it (Start, Power, Restart)*, Period 5 minutes, Grace 1 hour
     (the window's own restarts take under 10 minutes; a Windows-update reboot and sign-in far
     longer). Under Integrations add ntfy with your topic. Paste the check's ping URL here.
9. **Settings → Open the keeper's terminal**, and in it `gh auth login` (GitHub.com, HTTPS,
   log in with a browser). Claude needs no sign-in there: the keeper uses the machine's own.
   On an account other than `ChristianBlevens`, fork `ChristianBlevens/RaiGolmi` (and
   `raigolmi-packages`) first if the keeper is to publish its fixes; without a fork it keeps
   them in the container.
10. Close the RaiGolmi window and open it again, so the machine lets the keeper in.
11. Check, a few minutes later: `keeper\outbox\keeper.json` says `healthy`, and
    `keeper\outbox\keeper.log` shows its checks passing.

## Before leaving

12. Sign in to claude.ai again on the machine just before leaving (`rai claude-login --login` in its
    terminal). That sign-in lasts a fixed 14 days and is not renewed: after it ends the work goes on, but
    answering tabs from the phone stops. The phone gets a warning 3 days before.
13. Start the run in the machine tab, and leave the window open.
14. Tell whoever is around: **if the PC is off, turn it on. Nothing else.** The phone alert says
    what is wrong in plain words and whether there is anything to do.
