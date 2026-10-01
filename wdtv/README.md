# WD TV Live remote → dashboard

## What was found (research summary)

| Question | Finding |
|---|---|
| Does the WD TV Live remote work with Linux at all? | **Yes.** It is a plain NEC-protocol IR remote. With any Linux IR receiver (rc-core) it produces normal input events. LibreELEC users documented scancodes such as `0x847905 KEY_UP`, `0x847900 KEY_DOWN`, `0x847907 KEY_LEFT`, `0x847909 KEY_RIGHT`, `0x847908 KEY_OK`, `0x84791b KEY_BACK`. |
| Does the WD TV Live itself expose its IR receiver as Linux input events? | **Not documented / not confirmed.** The box (Sigma Designs SMP86xx, kernel 2.6.22) decodes IR in a proprietary driver used by the `DMAOSD` media player process. |
| What does WDLXTV provide? | Telnet/SSH root access and `/tmp/ir_injection` — a **write-only** file to *inject* key presses *into* the WD TV (`echo u > /tmp/ir_injection`). This is the opposite direction of what the dashboard needs. WDTVExt JavaScript plugins receive `onPageKey` events *inside* DMAOSD, but no documented way exists to forward them to the network. |

**Conclusion:** using the WD TV's *own* IR receiver is only possible if your
particular box exposes a readable key source. This cannot be assumed, so it is
not faked: run the probe below. Whatever the result, the **remote itself** can
always be used with option C.

## Option A/B – test your WD TV (exact steps)

1. Install WDLXTV on the WD TV Live (see the WDLXTV wiki) and enable telnet or SSH.
2. Copy `probe_ir.sh` (and `wdtv_ir_bridge.sh`) to a USB stick, plug it into the WD TV.
3. Log in: `telnet <wdtv-ip>` (user `root`).
4. Copy the script to RAM and run it:

   ```sh
   cp /tmp/media/usb/*/probe_ir.sh /tmp/ 2>/dev/null || find / -name probe_ir.sh 2>/dev/null
   sh /tmp/probe_ir.sh
   ```

5. When it says *"Listening … PRESS SOME REMOTE BUTTONS NOW"*, press UP/DOWN/OK a few times.
6. Read the **VERDICT** at the end:

   * **"generates Linux INPUT EVENTS on /dev/input/eventN"** → option A:
     ```sh
     sh /tmp/wdtv_ir_bridge.sh --device /dev/input/eventN --server http://<dashboard-ip>:8080
     ```
     Reading an evdev device is non-destructive: the WD TV keeps working normally.
   * **"readable as RAW records on /dev/ir"** → option B (experimental):
     ```sh
     sh /tmp/wdtv_ir_bridge.sh --device /dev/ir --format raw --record 4 --learn
     ```
     Press each button, note the printed hex code, write a map file
     (`<hexcode> KEY_UP` per line) and run with `--map /conf/f1keys.map --server …`.
     Try `--record 8` if the codes look shifted. **Warning:** if the probe
     reported that the WD TV menu stopped reacting while listening, `/dev/ir` has
     a single reader queue – the bridge would take key presses away from the WD
     TV. Do not use option B in that case.
   * **"No readable IR key source found"** → the firmware does not expose the
     remote. Use option C.

To start the bridge automatically, add the command (with `&`) to your WDLXTV
user start-up script if your WDLXTV build provides one; otherwise start it from
telnet when needed.

Important: in options A/B the WD TV still reacts to the same button presses
(e.g. UP also moves its own menu). Keep the WD TV on a screen where that does
no harm, or use buttons the WD TV ignores on its current screen.

## Option C – recommended: IR receiver on the dashboard server (or a Raspberry Pi)

Uses the same WD TV remote, independent of the WD TV firmware.

1. Hardware: any rc-core supported USB IR receiver, or a TSOP38238 on a
   Raspberry Pi GPIO (`dtoverlay=gpio-ir,gpio_pin=18` in `/boot/firmware/config.txt`).
2. Check the receiver and the remote:
   ```sh
   sudo apt install ir-keytable evtest
   sudo ir-keytable                          # lists rc0 and its /dev/input/eventN
   sudo ir-keytable -c -p nec -t             # press buttons: "scancode = 0x847905" lines appear
   ```
3. Load the keymap and verify Linux input events:
   ```sh
   sudo ir-keytable -c -p nec -w bridge/keymaps/wdtv_live.toml
   sudo evtest /dev/input/eventN             # EV_KEY KEY_UP etc. must appear
   ```
   Learn missing buttons (INFO, PLAY/PAUSE, 1-5) with `ir-keytable -t` and add them to the TOML.
4. Run the bridge:
   ```sh
   pip install evdev
   python3 bridge/evdev_bridge.py --list
   python3 bridge/evdev_bridge.py --test --device /dev/input/eventN
   python3 bridge/evdev_bridge.py --server http://127.0.0.1:8080 --device /dev/input/eventN
   ```
5. Permanent: `deploy/f1dash-ir-bridge.service`.

Any other source can use the same whitelisted API:
`GET /api/remote/key?key=KEY_UP`, `POST /api/remote/key {"key":"KEY_UP"}`,
`POST /api/remote/command {"command":"CHANGE_VIEW","arg":"strategy"}`.
