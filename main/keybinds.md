# Keyboard shortcuts and remote keys

This file lists every key the project actually handles, checked against the code in October 2026.
Sources:
- `main/config/config.toml` (`[remote.keymap]`, `[remote.keymap_video]`, `[remote.keymap_video_focus]`);
- `main/server/remote.py` (built-in defaults, the EVENT SYNC and PLAYER layers);
- `main/dashboard/app.js` (the browser keys);
- `server/tv/tv.js` (the `/tv` page);
- `main/dashboard/remote.html` (the phone remote).

If you change a key, change it in those files. `config.toml` is the place for your own remapping.

## How a key travels

```
browser key / IR remote / phone button / HTTP
  → key name (KEY_…)
  → key layer on the server
  → whitelisted command
  → shared UI state
  → every screen
```

- **Browser keys:** the dashboard (`/`) turns a browser key into a key name (`KEYS` in `app.js`) and sends it over its WebSocket.
- **Other inputs:** IR bridges (`server/bridge/evdev_bridge.py`), the WD TV and `GET|POST /api/remote/key` send the same key names.
- **The server decides.** A key does the same thing whichever screen or device sent it, and every screen shows the result. For example, `2` on the PC also changes the view on the TV.

### Which key layer is used

The first layer that matches wins. Each layer only applies to the keys it lists; any other key falls through to the layer below.

| # | Layer | Active when |
|---|---|---|
| 1 | **PLAYER** | the `/tv` LIVE / REPLAYS panel is open (`player_menu`) |
| 2 | **EVENT SYNC** | the SYNC menu is open *and* its EVENT SYNC sub-menu is open |
| 3 | **video focus** (`[remote.keymap_video_focus]`) | a TV mode with video is shown *and* the video has the focus |
| 4 | **video** (`[remote.keymap_video]`) | a TV mode with video (RACE_VIEW / VIDEO_FOCUS) is shown, as the server sees it |
| 5 | **base** (`[remote.keymap]`) | always |

## 1. Dashboard keyboard (`/`, and the dashboard inside `/tv`): base layer

These keys work when the dashboard window has the keyboard focus. Letters work in upper and lower case. Keys pressed inside a text field are not shortcuts (see §5).

| Keyboard key | Key name | Command | What it does |
|---|---|---|---|
| ↑ / ↓ | `KEY_UP` / `KEY_DOWN` | `MOVE_UP` / `MOVE_DOWN` | select the driver above / below in the leaderboard |
| ← / → | `KEY_LEFT` / `KEY_RIGHT` | `CHANGE_VIEW:prev` / `:next` | previous / next view |
| PageUp / PageDown | `KEY_PAGEUP` / `KEY_PAGEDOWN` | `PREVIOUS_DRIVER` / `NEXT_DRIVER` | previous / next driver |
| Enter | `KEY_ENTER` | `OPEN_TELEMETRY` | telemetry of the selected driver |
| Esc, Backspace, browser Back | `KEY_ESC`, `KEY_BACK` | `CLOSE_PANEL` | back to the overview |
| `1` | `KEY_1` | `CHANGE_VIEW:overview` | view: map / overview |
| `2` | `KEY_2` | `CHANGE_VIEW:telemetry` | view: telemetry |
| `3` | `KEY_3` | `CHANGE_VIEW:strategy` | view: strategy (tyre stints) |
| `4` | `KEY_4` | `CHANGE_VIEW:racecontrol` | view: race control |
| `5` | `KEY_5` | `CHANGE_VIEW:weather` | view: weather |
| `I` | `KEY_I` | `OPEN_RACE_CONTROL` | race control |
| `H`, `?` | `KEY_H` | `TOGGLE_HELP` | help overlay (with the phone-remote QR code) |
| `A` | `KEY_A` | `TOGGLE_AUTO_CYCLE` | rotate the views automatically |
| Space, `T` | `KEY_SPACE`, `KEY_T` | `CYCLE_TV_MODE` | TV mode: dashboard → race view → video focus (README §9a) |
| `P`, media Play/Pause | `KEY_P`, `KEY_PLAYPAUSE` | `VIDEO_PLAY_PAUSE` | play / pause the dashboard's own video (§9a) |
| `V` | `KEY_V` | `VIDEO_FOCUS` | give the video the remote focus |
| `M` | `KEY_M` | `VIDEO_MUTE` | mute the dashboard's own video |
| `F` | `KEY_F` | `VIDEO_FULLSCREEN` | full screen for the dashboard's own video |
| `E` | `KEY_E` | `CYCLE_MODE` | data mode AUTO → LIVE → VOD (applied after 1.5 s without another press) |
| `B` | `KEY_B` | `PLAYER_MENU` | open the `/tv` player's LIVE / REPLAYS panel (see §3) |
| `W` | `KEY_W` | `TRACK_REPORT` | the track map is wrong (press twice within 8 s to rebuild it) |
| `U` | `KEY_U` | `WEATHER_REPORT` | weather report popup now |
| `G` | `KEY_G` | `PITLANE_DEBUG` | pit-lane reconstruction debug (a built-in default, not in `config.toml`) |
| `Y`, `D` | `KEY_Y`, `KEY_D` | `SYNC_MENU`, `SYNC_DEBUG` | open / close the SYNC menu (both keys do the same) |
| `S` | `KEY_S` | `SYNC_MARK` | the selected car (or the leader) crosses the line on the video now |
| `L` | `KEY_L` | `SYNC_START` | lights out / the session clock starts on the video now |
| `C` | `KEY_C` | `SYNC_CONFIRM` | the dashboard's lap / clock matches the TV graphics |
| `K` | `KEY_K` | `SYNC_PIN` | manual anchor: "the dashboard time is right now" |
| `X` | `KEY_X` | `SYNC_CLEAR` | forget all sync anchors |
| `O` | `KEY_O` | `SYNC_KEEP_OLD` | after a *possible sync drift* warning: keep the current sync |
| `N` | `KEY_N` | `SYNC_USE_NEW` | … or use the new anchor |
| `R` | `KEY_R` | `SYNC_RESYNC` | force a resync |
| `+`, `=` | `KEY_KPPLUS`, `KEY_EQUAL` | `SYNC_PLUS` | +0.25 s delay (the dashboard shows older data) |
| `-`, `_` | `KEY_MINUS` | `SYNC_MINUS` | −0.25 s delay |
| mouse click on a leaderboard row | — | `SELECT_DRIVER` | select that driver |

## 2. Video layers (server TV mode RACE_VIEW or VIDEO_FOCUS)

These keys replace the base keys while the server's TV mode shows video. Any key not listed keeps its base meaning.

| Key | Video shown (`[remote.keymap_video]`) | Video focused (`[remote.keymap_video_focus]`) |
|---|---|---|
| OK, SELECT (remote) | `VIDEO_FOCUS`: focus the video | `VIDEO_PLAY_PAUSE` |
| Enter (keyboard) | still `OPEN_TELEMETRY` | `VIDEO_PLAY_PAUSE` |
| BACK, Esc, EXIT, Backspace | `VIDEO_UNFOCUS`: leave the focus / VIDEO_FOCUS | `VIDEO_UNFOCUS` |
| ← / → | (base) previous / next view | `VIDEO_SEEK` −10 s / +10 s (only when the stream is seekable) |
| ↑ / ↓ | (base) select a driver | `VIDEO_VOLUME` up / down |

The `VIDEO_*` commands control only the dashboard's **own** video element. In window or embed mode they show a hint: VOYO's player cannot be controlled from the dashboard. They do not control the `/tv` page's live or replay video; the `/tv` player is driven by its own keys and the PLAYER layer (§3, §4).

## 3. PLAYER layer: the `/tv` LIVE / REPLAYS panel

You can open the panel with:
- `B` on the dashboard or the `/tv` page;
- **LIST** or **EPG** on the IR remote (`KEY_LIST`, `KEY_EPG`);
- the **LIVE / REPLAYS** button on `/remote` or on the `/tv` controls.

While it is open, these keys drive the panel on every input device:

| Key | Command | In the panel |
|---|---|---|
| ↑ / ↓ | `PLAYER_NAV:up` / `:down` | move the cursor |
| ← / → | `PLAYER_NAV:left` / `:right` | depends on the row (see below) |
| OK, Enter, SELECT | `PLAYER_OK` | on LIVE: switch to live; on a recording: play it; on transport: play / pause; on volume: mute / unmute |
| BACK, Esc, EXIT, Backspace | `PLAYER_BACK` | close the panel |
| Play/Pause, PLAY, PAUSE, `P`, Space | `PLAYER_PLAY_PAUSE` | play / pause the `/tv` video |
| `B`, LIST, EPG, MENU | `PLAYER_MENU:close` | close the panel |

What ← / → do depends on the row the cursor is on:

| Row | ← / → |
|---|---|
| transport | seek −10 s / +10 s |
| volume | −10 % / +10 % |
| recordings list | move a page (5 rows) at a time |

## 4. The `/tv` page itself (outside the dashboard frame)

`/tv` shows the dashboard in a frame and the server's video on top of it. Keys reach the `/tv` page while the page, not the dashboard frame, has the focus. After a click inside the dashboard area, the frame has the focus and the §1 keys apply instead.

| Key | What it does on `/tv` | Where it is handled |
|---|---|---|
| `1` / `2` / `3` | layout RACE VIEW / VIDEO / DASHBOARD (this TV only, remembered) | locally |
| `M` | sound on / off for the `/tv` video | locally |
| `F` | browser full screen | locally |
| Space, `P`, media Play/Pause | play / pause the `/tv` video (live or replay) | locally (**changed**, see §6) |
| ↑ ↓ ← →, Enter, Esc, Backspace, browser Back | sent to the server as the remote keys (`KEY_UP`, …, `KEY_ENTER`, `KEY_ESC`, `KEY_BACK`); the PLAYER layer when the panel is open, else the dashboard | through the dashboard frame's connection |
| `B`, ContextMenu key | `KEY_B`: open / close the LIVE / REPLAYS panel | through the dashboard frame's connection |

Mouse or touch on `/tv` shows the controls for 4 seconds:
- **row 1:** RACE VIEW `1`, VIDEO `2`, DASHBOARD `3`;
- **row 2:** LIVE / REPLAYS `B`, SOUND `M`, FULLSCREEN `F`, LOG OUT, RECORDER;
- **row 3:** a status label (ON AIR / OFF AIR / REPLAY / NO CONNECTION) that cannot be clicked.

Before the TV is approved (the approval screen), no key does anything.

## 5. Local keys that never go to the server

| Where | Key | What it does |
|---|---|---|
| dashboard, SYNC menu open | Esc | closes the SYNC menu (`SYNC_MENU:close`), even while its EVENT SYNC sub-menu is open |
| dashboard, a field of the SYNC menu | Enter | applies that field (countdown / exact time); Esc leaves the field |
| dashboard, "choose circuit" list | Enter / Esc | use the chosen circuit / close the list |
| dashboard, any other input field | any key | the field loses the focus first, so a leftover focus does not swallow the shortcuts |
| team radio TEAM / DRIVER / SEARCH fields | any key | stays in the field (no shortcut fires while you type) |
| team radio seek slider | any key | the slider loses the focus like any other field, so the arrows act as the remote keys, not as seek |

### EVENT SYNC sub-menu (SYNC menu → EVENT SYNC)

While the sub-menu is open:

| Key | Command |
|---|---|
| ↑ / ↓ | `SYNC_EVENT_PREV` / `SYNC_EVENT_NEXT` |
| OK, Enter | `SYNC_EVENT_SET` |
| BACK, Backspace | `SYNC_EVENT_MENU:close` |
| Esc | from the remote, IR or HTTP: `SYNC_EVENT_MENU:close`; from the keyboard: the local Esc above closes the whole SYNC menu |

## 6. IR remote, phone remote, other pages

### IR / WD TV remote keys

The remote sends key names that a keyboard has no key for:

| Remote key | Command |
|---|---|
| OK, SELECT | as Enter (§1–3) |
| BACK, EXIT | as Esc |
| INFO | `OPEN_RACE_CONTROL` |
| HELP | `TOGGLE_HELP` |
| PLAY, PAUSE, PLAYPAUSE | `VIDEO_PLAY_PAUSE` |
| NEXT, PREVIOUS | `NEXT_DRIVER`, `PREVIOUS_DRIVER` |
| MENU | `CYCLE_MODE` |
| CH+, CH− | `SYNC_PLUS`, `SYNC_MINUS` |
| RED | `SYNC_MARK` |
| GREEN | `SYNC_RESYNC` |
| YELLOW | SYNC menu |
| BLUE | `SYNC_START` |
| LIST, EPG | `PLAYER_MENU` |
| keypad −, keypad + | `SYNC_MINUS`, `SYNC_PLUS` |

Scancodes: `server/bridge/keymaps/wdtv_live.toml` and `server/wdtv/README.md`. Only six buttons have published
scancodes there: ▲ ▼ ◀ ▶ OK BACK. Every other remote button works only after you learn its scancode
(`ir-keytable -t`) and map it to the key name in this table.

### Phone remote (`/remote`)

`/remote` has **no keyboard shortcuts**. Its buttons send the same key names and commands as the other inputs:

| Group | Buttons |
|---|---|
| D-pad | ▲ ▼ ◀ ▶ OK BACK HELP |
| Drivers | DRIVER ◀ / ▶ |
| Views | MAP / DATA / TYRES / RACE / WEATHER (= `1`–`5`) |
| Weather | WEATHER REPORT |
| TV layout | STATS / VOYO / VIDEO (`SET_TV_MODE`) and LIVE / REPLAYS (`PLAYER_MENU`) |
| Mode | AUTO / LIVE / VOD |
| VOYO sync | SYNC −, MARK, SYNC +, START, RESYNC, MENU |
| Advanced sync | MARK STREAM START, RESET STREAM START, EVENT SYNC |
| Video | ⏯, MUTE, AUTO CYCLE |

### Other pages

`/disk` (the recorder admin page) has no keyboard shortcuts.

## 7. Duplicates, conflicts and known limits

**Duplicates (intentional).** Several keys give the same command so that the keyboard, the IR remote and media keys all work:
- Enter / OK / SELECT;
- Esc / Backspace / BACK / EXIT;
- `I` / INFO;
- `H` / `?` / HELP;
- `P` / Play / Pause / PlayPause;
- Space / `T`;
- PageUp / PREVIOUS and PageDown / NEXT;
- `S` / RED, `L` / BLUE, `R` / GREEN;
- `Y` / `D` / YELLOW;
- `E` / MENU;
- `+` / `=` / CH+ / keypad + and `-` / `_` / CH− / keypad −;
- `B` / LIST / EPG.

**Conflicts (different meaning in different places):**

| Key | Conflict |
|---|---|
| `1` `2` `3` | On the `/tv` page they pick the `/tv` layout. Inside the dashboard (also inside the `/tv` frame) they switch the view (overview / telemetry / strategy) on **every** screen. |
| `M`, `F` | On the `/tv` page: the `/tv` video's sound and the browser's full screen. In the dashboard: `VIDEO_MUTE` / `VIDEO_FULLSCREEN` for the dashboard's own video. |
| Space, `P` | On the `/tv` page: play / pause of that page's video. In the dashboard: Space cycles the TV mode for **every** screen and `P` is the dashboard video's play / pause. While the PLAYER panel is open, both are play / pause everywhere. |
| Enter vs OK | In a video TV mode, OK focuses the video but keyboard Enter still opens telemetry (on purpose, so the PC keyboard keeps telemetry). |
| OK / BACK with video | The video layers follow the **server's** TV mode, not a single screen's layout. A `/tv` page in VIDEO layout while the server is in FULL_DASHBOARD gets the base meaning: OK opens telemetry. |
| MENU | `CYCLE_MODE` normally, but closes the LIVE / REPLAYS panel while it is open. |
| Esc in EVENT SYNC | Keyboard Esc closes the whole SYNC menu; BACK, Backspace and the remote's Esc close only the EVENT SYNC sub-menu. |
| PLAYER panel | Its open state is shared, like every remote state. While it is open, the arrows / OK / BACK / Space / `P` of **all** inputs drive the panel, even on a PC that shows no `/tv` page. Close it with `B` or BACK. |
| SYNC menu and PLAYER panel | Never open at the same time: opening one closes the other. |

**Broken or missing bindings found:**
- No key is unmapped in the browser: every key in `KEYS` (`app.js`) and in the `/tv` page has a command.
- `KEY_KPMINUS` exists in `config.toml`, but the browser sends `KEY_MINUS` for the keypad minus too. Only an IR or evdev keypad sends `KEY_KPMINUS`. Harmless.
- The help overlay (`H`) lists only the keys it has a label for. It does **not** show:
  - PageUp / PageDown;
  - the media keys;
  - the IR-only keys;
  - the video, EVENT SYNC and PLAYER layers.

  This file is the complete list.
- No "intake" view exists. The five views are overview (map), telemetry, strategy (tyre stints), race control and weather.

## 8. Changes (October 2026)

- **New:** `B` (keyboard), LIST / EPG (IR remote), the `/remote` LIVE / REPLAYS button and the `/tv` LIVE / REPLAYS button open the `/tv` player panel. The PLAYER layer (§3) is new with it.
- **New:** on the `/tv` page the arrows, Enter, Esc, Backspace, browser Back, `B` and ContextMenu are sent the remote's way (§4). Before, the `/tv` page handled only `1` `2` `3` `M` `F`.
- **Changed:** on the `/tv` page, Space, `P` and media Play/Pause play / pause **that page's** video. They are not sent to the server, so Space on the TV no longer switches the TV mode of every other screen. In the dashboard, Space and `P` are unchanged.
- **Help overlay:** now also lists `B` (LIVE / REPLAYS panel).
- No existing shortcut was removed.
