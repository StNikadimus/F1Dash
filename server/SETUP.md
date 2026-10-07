# Complete server setup — from a fresh Linux install to a working F1 dashboard server

This guide starts from a computer with **only a fresh Linux installed** and ends with all of this
running on it:

- the F1 dashboard backend (F1 TV live timing, LIVE/VOD, AUTO SYNC, recordings, phone remote),
  started automatically at every boot;
- the **external disk** mounted at `/mnt/f1disk`, holding the VOYO stream recordings;
- the **server VOYO player**, which opens VOYO by itself around every F1 session and records it.
  Your PC does not need to be on.

Do the steps **in order**. Every command is meant to be copied exactly. The only exceptions are the
values in `<ANGLE BRACKETS>`, which you replace with your own.

The guide is written for **Ubuntu Server 24.04 LTS**. Debian 12 works the same way. Other
distributions need other package commands.

---

## 0. What you need

| Thing | Why |
|---|---|
| The server, with Ubuntu Server 24.04 LTS (or Debian 12) installed, 64-bit (x86_64 / amd64) | Google Chrome for the VOYO player exists only for amd64 |
| The server connected to your home network with a **network cable** (recommended) | a stable stream and recording |
| The **external hard disk** (USB) | the VOYO recordings (a race at 1080p is about 3–6 GB) |
| Your **GitHub account**, with access to `StNikadimus/F1Dash` | to download (`git clone`) and update (`git pull`) the code |
| Your **F1 TV** subscription login | live timing data |
| Your **VOYO** subscription login | the video stream |
| Your **Windows PC** on the same network | for the one-time sign-ins (SSH + VNC). Windows 10/11 already has `ssh` |
| A **VNC viewer** on the PC, e.g. [TigerVNC Viewer](https://github.com/TigerVNC/tigervnc/releases) or RealVNC Viewer | to see the server's invisible screen once, to sign in to VOYO |

Words used below:

- **`<ADMIN>`**: the user name you created when you installed Linux (the one you log in with).
- **`<SERVER-IP>`**: the server's address on your network, e.g. `192.168.1.50` (step 2.3 shows it).
- **`f1`**: a separate user that the dashboard runs as. You create it in step 3.

---

## P. Only if the server runs on Proxmox (e.g. the IdeaPad Y700): create the container first

On a Proxmox host the F1 server runs in its own **LXC container** (Ubuntu 24.04), not directly on
Proxmox. This chapter gets you from Proxmox to a container you can SSH into; **then do steps 1–15
inside the container**. Where a step works differently in a container, it has a "Proxmox:" note.

Why a container and not a VM:
- less overhead (with only 2 cores free, every bit counts);
- the **Intel GPU (Quick Sync)** can simply be shared with the container, so it encodes the
  recording and the 2 cores stay free. The laptop's NVIDIA GeForce 940M (GM108) has **no video
  encoder (NVENC)** and doesn't help here.

All commands in this chapter run **on the Proxmox host** (`root@pve`), in its Shell or over SSH.

### P.1 Proxmox version and the Intel GPU node

```bash
pveversion                      # must be pve-manager/8.1 or newer (device passthrough for containers)
ls -l /dev/dri/by-path/
```

The line `pci-0000:00:02.0-render -> ../renderD12X` is the **Intel HD 530** (`00:02.0` is the Intel
GPU in `lspci`). Note that `renderD12X` (e.g. `renderD128`). The other `renderD` belongs to the
NVIDIA card; leave it alone.

If `pveversion` is older than 8.1, run `apt update && apt full-upgrade -y` on the host and reboot.

### P.2 Download the Ubuntu template and create the container

```bash
pveam update
pveam available --section system | grep ubuntu-24.04      # e.g. ubuntu-24.04-standard_24.04-2_amd64.tar.zst
pveam download local <the ubuntu-24.04-standard file name from the line above>
pvesm status                                              # storage names: usually local-lvm (or local / local-zfs)
```

Create the container. `120` is the container number (any free one), `local-lvm` the storage from
`pvesm status`, and 16 GB its system disk (the recordings go to the USB disk, not here):

```bash
pct create 120 local:vztmpl/<the template file name> \
  --hostname f1-server --cores 2 --memory 4096 --swap 2048 \
  --rootfs local-lvm:16 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=1 --onboot 1 --password
```

It asks for a root password for the container; choose one.

- `nesting=1` is needed: Chrome's sandbox uses it.
- `--onboot 1` starts the container with Proxmox.

### P.3 Give the container the Intel GPU

```bash
pct start 120
pct exec 120 -- getent group render        # e.g. "render:x:993:" -> the number is <RENDER-GID>
pct stop 120
pct set 120 --dev0 /dev/dri/renderD12X,gid=<RENDER-GID>,mode=0660     # renderD12X from P.1
pct start 120
pct exec 120 -- ls -l /dev/dri/            # shows renderD12X with group "render"
```

(In the web UI this is *Container → Resources → Add → Device Passthrough*.)

### P.4 An admin user in the container, SSH, and its IP

```bash
pct enter 120                    # you are now inside the container (prompt root@f1-server)
adduser <ADMIN>                  # choose a user name + password
usermod -aG sudo <ADMIN>
apt update && apt install -y openssh-server && systemctl enable --now ssh
ip -4 addr show eth0 | grep inet # the container's <SERVER-IP>
exit                             # back to the Proxmox host
```

From now on, `<SERVER-IP>` is **the container's** address, not the Proxmox one. Continue with
**step 1** (you can skip its first part and go straight to `ssh <ADMIN>@<SERVER-IP>` from your PC).

Proxmox notes for the later steps:
- **2.2 clock:** a container uses the **host's** clock. `timedatectl set-ntp` doesn't work inside
  it; that's fine. Instead check that the host is synchronized: `chronyc tracking` on the Proxmox
  host should show `Leap status: Normal`. Set the time zone inside the container with
  `sudo timedatectl set-timezone Europe/Ljubljana`, or with `sudo ln -sf
  /usr/share/zoneinfo/Europe/Ljubljana /etc/localtime` if that fails.
- **2.3 fixed IP:** reserve the container's MAC address in your router (`pct config 120` on the
  host shows `hwaddr=...`), or set it on the host:
  `pct set 120 --net0 name=eth0,bridge=vmbr0,ip=<SERVER-IP>/24,gw=<ROUTER-IP>`.
- **2.4 firewall:** `ufw` often can't be enabled inside an unprivileged container (iptables errors).
  If `sudo ufw enable` fails, skip it. On a home network that's fine; or use Proxmox's own firewall
  (*Container 120 → Firewall*) with rules for 22 and 8080.
- **4 disk:** the USB disk is mounted on the **Proxmox host** and handed to the container (step 4.4).

---

## 1. Sit at the server once: SSH

If you can already log in to the server from your PC with `ssh`, skip to step 2.

At the server's keyboard, log in as `<ADMIN>` and run:

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
ip -4 addr show | grep inet
```

The last command shows lines like `inet 192.168.1.50/24 ...`. The address that is **not**
`127.0.0.1` is your `<SERVER-IP>`.

From now on you can work from your PC. Open **PowerShell** on Windows and run:

```powershell
ssh <ADMIN>@<SERVER-IP>
```

The first time, answer `yes`, then type your Linux password. Every command below is typed into this
SSH window, unless a step says "on your PC".

---

## 2. Base system

### 2.1 Updates and the basic tools

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv python3-pip curl wget ca-certificates gnupg \
                    ufw openssl nano parted ntfs-3g exfatprogs
python3 --version          # must be 3.11 or newer (Ubuntu 24.04: 3.12, Debian 12: 3.11)
```

If the update installed a new kernel, restart now with `sudo reboot`, wait a minute, then
`ssh <ADMIN>@<SERVER-IP>` again.

### 2.2 Time zone and exact clock

The dashboard compares F1 timestamps with the server clock, so the clock must be exact (NTP).

```bash
sudo timedatectl set-timezone Europe/Ljubljana
sudo timedatectl set-ntp true
timedatectl
```

Check that it shows `System clock synchronized: yes` and `NTP service: active`. It can take a
minute; run `timedatectl` again if it still says `no`. If `NTP service` shows `n/a`, run
`sudo apt install -y systemd-timesyncd` and repeat the three commands.

### 2.3 A fixed IP address

Your PC, TV and phone reach the server through its IP address, so that address must never change.

- **Easiest, in your router:** open the router's web page, find "DHCP reservation" / "static
  lease" / "address reservation", and reserve the current `<SERVER-IP>` for the server's MAC
  address. You can see the MAC with `ip link` (the `link/ether xx:xx:...` line of your network
  card).
- **Or on the server (netplan):** only if you know your network.
  `ls /etc/netplan/` shows the file. Set `dhcp4: false`, `addresses: [<SERVER-IP>/24]`,
  `routes: [{to: default, via: <ROUTER-IP>}]` and `nameservers: {addresses: [<ROUTER-IP>]}`.
  Then run `sudo netplan apply`.

### 2.4 Firewall

```bash
sudo ufw allow OpenSSH            # keep SSH working!
sudo ufw allow 8080/tcp           # the dashboard (TV, PC, phone)
sudo ufw enable                   # answer y
sudo ufw status
```

Do **not** open port 5900 (VNC). VNC is only used through an SSH tunnel.

---

## 3. The `f1` service user

The dashboard runs as its own user, `f1`, not as you. Nobody can log in as `f1`
(`--shell /usr/sbin/nologin`); it only runs the dashboard.

```bash
sudo useradd --system --create-home --home-dir /home/f1 --shell /usr/sbin/nologin f1
sudo install -d -o f1 -g f1 -m 750 /var/lib/f1-dashboard     # its data: sign-ins, sync state, F1 recordings
sudo install -d -o f1 -g f1 /opt/f1-dashboard                # the code goes here (step 6)
for g in render video; do getent group $g >/dev/null && sudo usermod -aG $g f1; done   # the Intel GPU (Quick Sync)
id f1
```

Note the number after `uid=` (e.g. `uid=999(f1)`). The Proxmox disk step needs it.

---

## 4. The external disk at `/mnt/f1disk`

The configuration (`server/config/server.toml`) already says: VOYO recordings go to
`/mnt/f1disk/voyo_streams`, and **only while a disk is mounted at `/mnt/f1disk`**. If the disk is
missing, nothing is written to the system disk; the server just waits and starts recording when the
disk is back.

### 4.1 Find the disk

Plug the disk in, then run:

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT,MODEL
```

Find your disk by its **size** and **model**, e.g. `sdb  931.5G ... My Passport`. Its partition is
usually `sdb1`. In the commands below, `sdX` means your disk (e.g. `sdb`) and `sdX1` its partition
(e.g. `sdb1`).

> ⚠ Double-check the name. `sda` / `nvme0n1` is usually the disk Linux is installed on. Never
> format that one.

### 4.2 Choose one option

**Option A: an empty disk, or one whose contents you don't need (recommended, ext4).**
This **erases everything** on the disk:

```bash
sudo umount /dev/sdX1 2>/dev/null; sudo umount /media/*/* 2>/dev/null
sudo parted /dev/sdX --script mklabel gpt mkpart f1disk ext4 0% 100%
sudo mkfs.ext4 -F -L f1disk /dev/sdX1
```

**Option B: keep the disk as it is (NTFS or exFAT, e.g. so Windows can still read it).**
Don't format anything; just note the type that `lsblk` showed in the `FSTYPE` column (`ntfs` or
`exfat`).

### 4.3 Mount it permanently (also after every reboot)

```bash
sudo mkdir -p /mnt/f1disk
sudo blkid /dev/sdX1          # copy the UUID="...." value (without quotes)
sudo nano /etc/fstab
```

Add **one** line at the end of the file. Use the one that matches your disk and replace `<UUID>`:

```
# Option A (ext4):
UUID=<UUID>  /mnt/f1disk  ext4   defaults,nofail,x-systemd.device-timeout=10s  0  2
# Option B, NTFS:
UUID=<UUID>  /mnt/f1disk  ntfs3  defaults,nofail,uid=f1,gid=f1,x-systemd.device-timeout=10s  0  0
# Option B, exFAT:
UUID=<UUID>  /mnt/f1disk  exfat  defaults,nofail,uid=f1,gid=f1,x-systemd.device-timeout=10s  0  0
```

Save with **Ctrl+O, Enter**, and close with **Ctrl+X**.

`nofail` means the server still boots normally when the disk is unplugged.

```bash
sudo systemctl daemon-reload
sudo mount -a
findmnt /mnt/f1disk            # must show the disk (SOURCE /dev/sdX1)
df -h /mnt/f1disk              # its size and free space
# only for ext4 (option A): the disk belongs to the f1 user
sudo chown f1:f1 /mnt/f1disk
# check that f1 can write to it:
sudo -u f1 touch /mnt/f1disk/.test && sudo -u f1 rm /mnt/f1disk/.test && echo "disk OK"
```

It must print `disk OK`. Then mark the disk as **the** recording disk. The server only writes to
`/mnt/f1disk` while this file is there, so an unplugged disk can never fill the system disk:

```bash
sudo -u f1 touch /mnt/f1disk/.f1disk
```

### 4.4 Proxmox: mount the disk on the host and give it to the container

On Proxmox, **don't** do 4.1–4.3 in the container. Do them **on the Proxmox host** instead: plug the
disk into the laptop, then run `lsblk`, format if you like (option A), `mkdir -p /mnt/f1disk`, add
the fstab line, and `mount -a`. The host is Debian; the commands are the same, without `sudo`. Two
things are different, because the container is "unprivileged": the container's user `f1` (uid
`<UID>` from step 3, e.g. 999) is user **`100000 + <UID>`** (e.g. `100999`) on the host.

- **ext4 (option A), on the host:** `chown 100999:100999 /mnt/f1disk` (use your number).
- **NTFS / exFAT (option B), on the host:** use `uid=100999,gid=100999` in the fstab line instead
  of `uid=f1,gid=f1`.

Then, still on the host, mark the disk and give it to the container:

```bash
touch /mnt/f1disk/.f1disk && chown 100999:100999 /mnt/f1disk/.f1disk
pct set 120 -mp0 /mnt/f1disk,mp=/mnt/f1disk,backup=0
pct reboot 120
```

`backup=0` keeps Proxmox backups from copying the recordings. Inside the container, check with
`ls -la /mnt/f1disk` (it must show `.f1disk`) and the write test from 4.3
(`sudo -u f1 touch /mnt/f1disk/.test && sudo -u f1 rm /mnt/f1disk/.test && echo "disk OK"`).

> Why `.f1disk` matters here: inside the container `/mnt/f1disk` always looks mounted, even when the
> USB disk is unplugged from the host (then it is just the host's empty folder). The server checks
> for `.f1disk` and writes nothing while it is missing.

---

## 5. GitHub access, so the server can download (and later update) the code

The server gets a **deploy key**: an SSH key that can only **read** this one repository. That is
safer than putting your GitHub password or a personal token on the server.

### 5.1 Make the key (as the `f1` user, who will own the code)

```bash
sudo -H -u f1 ssh-keygen -t ed25519 -C "f1-dashboard-server" -f /home/f1/.ssh/id_ed25519 -N ""
sudo cat /home/f1/.ssh/id_ed25519.pub
```

Copy the whole line it prints (`ssh-ed25519 AAAA... f1-dashboard-server`).

### 5.2 Add the key to the repository on GitHub (on your PC, in the browser)

1. Open https://github.com/StNikadimus/F1Dash and click **Settings**. You must be the owner or
   have admin rights.
2. In the left menu, choose **Deploy keys**, then **Add deploy key**.
3. Title: `f1-dashboard-server`. Key: paste the line from 5.1.
4. Leave **Allow write access** **unchecked**. The server only needs to read.
5. Click **Add key**. GitHub may ask for your password or 2FA.

### 5.3 Test the connection

```bash
sudo -H -u f1 ssh -T git@github.com
```

Answer `yes` to the "authenticity of host" question. You should see:
`Hi StNikadimus/F1Dash! You've successfully authenticated, but GitHub does not provide shell access.`

That message is the success message.

> **Alternative, HTTPS with a token** (only if you can't use deploy keys):
> 1. On GitHub, go to your picture → **Settings → Developer settings → Personal access tokens →
>    Fine-grained tokens → Generate new token**.
> 2. Repository access: *Only select repositories* → `F1Dash`. Permissions: **Contents: Read-only**.
>    Copy the token.
> 3. On the server, run `sudo -H -u f1 git config --global credential.helper store`, then clone
>    with `https://github.com/StNikadimus/F1Dash.git`. When asked, enter your GitHub user name and
>    the **token** as the password; it is remembered after that.

---

## 6. Download the code to `/opt/f1-dashboard`

```bash
sudo -H -u f1 git clone git@github.com:StNikadimus/F1Dash.git /opt/f1-dashboard
ls /opt/f1-dashboard            # main  server  pc variant  README.md  launch.bat
```

This step is optional and only removes a "dubious ownership" warning when you type git commands in
that folder yourself:

```bash
git config --global --add safe.directory /opt/f1-dashboard
```

---

## 7. Settings (`server/.env`)

```bash
sudo -u f1 cp /opt/f1-dashboard/server/.env.example /opt/f1-dashboard/server/.env
openssl rand -hex 16            # prints a random secret - copy it
sudo -u f1 nano /opt/f1-dashboard/server/.env
```

Fill in:

```
F1TV_TOKEN=
F1DASH_SOURCE_MODE=auto
F1DASH_REMOTE_TOKEN=<the random secret you just copied>
TZ=Europe/Ljubljana
F1DASH_DATA_DIR=/var/lib/f1-dashboard
```

Add the last line yourself; it isn't in the example file. It makes every command below (and the
services) use the same data folder.

Leave `F1TV_TOKEN` empty: you sign in to F1 TV in step 10. Save with Ctrl+O, Enter, Ctrl+X, then
protect the file:

```bash
sudo chmod 600 /opt/f1-dashboard/server/.env
```

**Write the `F1DASH_REMOTE_TOKEN` down.** The PC launcher and the phone remote need it.

The recording disk and the server VOYO player are already set in
`/opt/f1-dashboard/server/config/server.toml` (`/mnt/f1disk/voyo_streams`, server player
`enabled = true`). Nothing to change there.

---

## 8. First start by hand (a test)

This creates the Python environment (`/opt/f1-dashboard/.venv`) and checks that everything starts.

```bash
sudo -H -u f1 /opt/f1-dashboard/server/launch.sh --test
```

The first start takes a minute (installing the Python packages). Wait for
`Uvicorn running on http://0.0.0.0:8080`, and look for the recording line:

- `VOYO stream recordings: /mnt/f1disk/voyo_streams (free ... GB)`: the disk works.
- `VOYO stream recording DISABLED: no disk mounted at /mnt/f1disk`: go back to step 4.

On your PC, open **http://`<SERVER-IP>`:8080** in a browser. You should see the dashboard with the
simulator (TEST mode).

Press **Ctrl+C** in the SSH window to stop it.

---

## 9. Start the dashboard automatically (systemd service)

```bash
sudo cp /opt/f1-dashboard/server/systemd/f1-dashboard.service /etc/systemd/system/
```

If you use the external disk, the dashboard should wait for it at boot. Open the unit file:

```bash
sudo nano /etc/systemd/system/f1-dashboard.service
```

Change the line `#RequiresMountsFor=/mnt/f1disk` to `RequiresMountsFor=/mnt/f1disk` (remove the
`#`). Without the `#` it is active.

> Only do this if the disk is always plugged in: with that line the dashboard does **not** start
> while the disk is missing. Leave the line commented out if you sometimes unplug the disk. The
> dashboard then runs anyway and simply records again once the disk is back.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now f1-dashboard
systemctl status f1-dashboard --no-pager        # "active (running)"
journalctl -u f1-dashboard -n 50 --no-pager     # the log
```

Open **http://`<SERVER-IP>`:8080** on the PC again. It now runs in AUTO mode (LIVE during an F1
session, otherwise VOD), and it starts by itself at every boot.

---

## 10. F1 TV sign-in (once)

For security, the sign-in page answers only on the server itself. You reach it through an SSH
tunnel from your PC.

1. **On your PC:** close any dashboard running on the PC itself (local port 8080 must be free).
   Then, in a **new** PowerShell window, run:
   ```powershell
   ssh -L 8080:127.0.0.1:8080 <ADMIN>@<SERVER-IP>
   ```
   Keep this window open.
2. **On your PC**, open **http://127.0.0.1:8080/f1tv/login** in the browser and sign in with your
   F1 TV account.
3. Back in the server SSH window, check:
   ```bash
   sudo -H -u f1 /opt/f1-dashboard/server/launch.sh --f1-status
   sudo systemctl restart f1-dashboard
   ```
   It should say you are signed in (it never prints the token). Then close the tunnel window.

The sign-in is stored in `/var/lib/f1-dashboard/auth/f1tv_auth.json`. Repeat this step if it ever
says "expired".

---

## 11. The server VOYO player (records VOYO without your PC)

### 11.1 Install Chrome, the virtual screen, sound, ffmpeg and VNC

```bash
sudo /opt/f1-dashboard/server/setup-voyo-player.sh
```

At the end it must print a Chrome version and **`Widevine: ok`**. Widevine is the DRM module VOYO's
player needs; Google Chrome has it.

It also checks **Intel Quick Sync**. You want a line like `VAProfileH264Main : VAEntrypointEncSlice`;
then the recording is encoded by the GPU (`capture_encoder = "vaapi"` in
`server/config/server.toml`, already set). If it prints `not found with the default driver`, run
`LIBVA_DRIVER_NAME=i965 vainfo | grep -i h264`. If that shows `EncSlice`, add
`LIBVA_DRIVER_NAME=i965` to `server/.env`. Without Quick Sync, recording falls back to the CPU by
itself; the log says so.

### 11.2 Sign in to VOYO (once)

The player must not be running while you do this. It isn't installed yet, so the first time this is
fine. Later, stop it first with `sudo systemctl stop f1-voyo-player`.

```bash
sudo -H -u f1 /opt/f1-dashboard/server/voyo-player.sh login
```

It prints a VNC **password**. Then:

1. **On your PC**, in a new PowerShell window, run the following and keep the window open:
   ```powershell
   ssh -L 5900:127.0.0.1:5900 <ADMIN>@<SERVER-IP>
   ```
2. **On your PC**, open the VNC viewer, connect to **`127.0.0.1:5900`**, and enter the password.
3. You now see the server's (invisible) screen with Chrome. Sign in to **VOYO**, then open the
   **F1 live channel / stream page**, the same one you open on the PC to watch F1.
4. Back in the server SSH window, press **Ctrl+C**. It prints `Saved as the stream page: https://...`.
   From now on, that page is opened for every session.

Close the VNC viewer and the tunnel window.

### 11.3 Test: record 3 minutes now

```bash
sudo -H -u f1 /opt/f1-dashboard/server/voyo-player.sh test --minutes 3
```

Good output ends with something like:

`recording <id>: closed, 180 s, 3 video segment(s), 120.0 MB`

You can check the files:

```bash
ls /mnt/f1disk/voyo_streams/*/capture/
```

and copy one `.mp4` to your PC (on the PC: `scp <ADMIN>@<SERVER-IP>:/mnt/f1disk/voyo_streams/<id>/capture/<file>.mp4 .`)
to watch it.

If it says **"no video on the page"**: the page needs a click VOYO wants (e.g. a channel tile or a
"play" button), or you are not signed in. Repeat 11.2, make sure the stream is **playing** in the
VNC view when you press Ctrl+C, and test again. If the picture is black or Chrome reports a DRM
error, VOYO doesn't allow playback in Chrome on Linux. In that case, send me the output.

### 11.4 Install the player service

```bash
sudo -H -u f1 /opt/f1-dashboard/server/voyo-player.sh status   # Chrome + Widevine ok, stream page set, next sessions
sudo cp /opt/f1-dashboard/server/systemd/f1-voyo-player.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now f1-voyo-player
journalctl -u f1-voyo-player -n 30 --no-pager           # "next: <GP> <session> - opens ..."
```

From now on, the server opens VOYO 15 minutes before every F1 session (practice, qualifying,
sprint, race), records it to the disk, and closes it 30 minutes after the end. It stays open longer
if the session is still running. The recordings appear at
**http://`<SERVER-IP>`:8080/api/voyo/recordings**.

---

## 12. Your Windows PC (optional, for watching)

The PC is only needed when you want to watch VOYO with the dashboard on your screen or TV.

1. Set the same remote token on the PC once. In PowerShell:
   ```powershell
   setx F1DASH_REMOTE_TOKEN "<the same secret as in server/.env>"
   ```
   Close and reopen PowerShell / Explorer afterwards.
2. Start it from the repository folder on the PC:
   ```
   launch.bat server http://<SERVER-IP>:8080
   ```
   The PC shows the dashboard and VOYO (AirParrot `capture3` stays the default) and uses the
   server's backend.

## 13. Phone remote

Open **http://`<SERVER-IP>`:8080/remote?token=`<your remote token>`** on the phone (same Wi-Fi),
then use "Add to home screen".

---

## 14. Everyday use

| Task | Command |
|---|---|
| Update to the newest version | `sudo -H -u f1 git -C /opt/f1-dashboard pull && sudo systemctl restart f1-dashboard f1-voyo-player` |
| Dashboard log (live) | `journalctl -u f1-dashboard -f` |
| VOYO player log (live) | `journalctl -u f1-voyo-player -f` |
| Restart | `sudo systemctl restart f1-dashboard` (and/or `f1-voyo-player`) |
| Stop / start | `sudo systemctl stop f1-dashboard` / `sudo systemctl start f1-dashboard` |
| Is the disk there? How full? | `findmnt /mnt/f1disk && df -h /mnt/f1disk` |
| List of VOYO recordings | http://`<SERVER-IP>`:8080/api/voyo/recordings |
| Next sessions the player will record | `sudo -H -u f1 /opt/f1-dashboard/server/voyo-player.sh status` |
| F1 TV sign-in state | `sudo -H -u f1 /opt/f1-dashboard/server/launch.sh --f1-status` |
| System updates (monthly) | `sudo apt update && sudo apt full-upgrade -y && sudo reboot` |

**Unplugging the disk safely:**

```bash
sudo systemctl stop f1-voyo-player
sudo umount /mnt/f1disk
```

Then unplug. Plug it back in and run `sudo mount -a`, followed by
`sudo systemctl start f1-voyo-player`.

**Where things are:**

| What | Where |
|---|---|
| Code | `/opt/f1-dashboard` |
| Settings | `/opt/f1-dashboard/server/.env`, `/opt/f1-dashboard/server/config/server.toml` |
| F1 TV sign-in, sync state, F1 timing recordings, caches | `/var/lib/f1-dashboard` |
| VOYO browser profile (VOYO sign-in) | `/var/lib/f1-dashboard/browser-profiles/voyo-server` |
| VOYO stream recordings (+ video) | `/mnt/f1disk/voyo_streams/<stream id>/` |

Videos are deleted automatically after the number of days set by `keep_*_days` in
`main/config/config.toml` (practice 7, qualifying/sprint 14, race 30). The data files of a
recording are kept.

---

## 15. Troubleshooting

| Problem | Fix |
|---|---|
| `ssh: connect ... refused` | step 1 (openssh-server), or a wrong IP |
| `Permission denied (publickey)` on `git clone` / `git pull` | step 5: the deploy key isn't added on GitHub, or you ran git without `sudo -H -u f1` |
| `Python 3.11 or newer is required` | `sudo apt install -y python3.12 python3.12-venv`, then `PYTHON=python3.12` in `server/.env` |
| Dashboard not reachable from the PC | `systemctl status f1-dashboard`; `sudo ufw status` must show `8080/tcp ALLOW` |
| `VOYO stream recording DISABLED: no disk mounted at /mnt/f1disk` | `sudo mount -a`, `findmnt /mnt/f1disk` (step 4) |
| `... is not writable` | `sudo chown f1:f1 /mnt/f1disk` (ext4), or `uid=f1,gid=f1` in fstab (NTFS/exFAT) |
| `Widevine: NOT FOUND` | Google Chrome is missing: run `setup-voyo-player.sh` again |
| Log: `Intel Quick Sync (vaapi) failed` / `no Intel GPU render node` | Proxmox step P.3 (`dev0`, the right `renderD`), `id f1` must list `render`, and `LIBVA_DRIVER_NAME=i965` (step 11.1) |
| Recording stutters, `htop` at 100 % CPU | `resolution = "1280x720"` under `[voyo.server_player]` in `server/config/server.toml`, then `sudo systemctl restart f1-voyo-player` |
| `... /mnt/f1disk/.f1disk is missing` | the disk isn't mounted (on the Proxmox host: `findmnt /mnt/f1disk`), or the marker is missing (step 4.3 / 4.4) |
| Chrome: `No usable sandbox` (Proxmox) | `pct set 120 --features nesting=1` on the host, then `pct reboot 120` |
| VOYO player: `no stream_url and no page learned yet` | step 11.2 |
| VOYO player: `no video on the page yet ... signed in?` | VOYO signed you out: step 11.2 again |
| Recording has no sound | `setup-voyo-player.sh` printed the PipeWire note: run `sudo apt install pulseaudio`, then restart `f1-voyo-player` |
| `System clock synchronized: no` | step 2.2 |
| F1 TV: "sign-in expired" | step 10 |

When something doesn't work, copy the last 50 lines of the log
(`journalctl -u f1-dashboard -n 50 --no-pager` or `journalctl -u f1-voyo-player -n 50 --no-pager`)
and send them to me.

---

## Checklist

- [ ] P (Proxmox only): container 120 with 2 cores / 4 GB, `nesting=1`, Intel `renderD` passed, disk `mp0`
- [ ] 1: SSH from the PC works
- [ ] 2: updates, `timedatectl` says synchronized, fixed IP, firewall (22 + 8080)
- [ ] 3: user `f1`, `/var/lib/f1-dashboard` and `/opt/f1-dashboard` exist
- [ ] 4: `findmnt /mnt/f1disk` shows the disk; the fstab line uses `nofail`; "disk OK"; `.f1disk` exists
- [ ] 5: `ssh -T git@github.com` (as f1) greets `StNikadimus/F1Dash`
- [ ] 6: code in `/opt/f1-dashboard`
- [ ] 7: `server/.env` with the remote token (written down) and `F1DASH_DATA_DIR`
- [ ] 8: test start shows `VOYO stream recordings: /mnt/f1disk/voyo_streams`
- [ ] 9: `f1-dashboard` service enabled and running
- [ ] 10: F1 TV signed in
- [ ] 11: Chrome + Widevine ok, VOYO signed in, the 3-minute test recorded video, `f1-voyo-player` enabled
- [ ] 12/13: PC launcher and phone remote use the token
