# PhoneType

[English](README_en.md) | [简体中文](README.md)

PhoneType relays text entered on a phone into the focused window on a Windows PC over the local
network. The phone acts as a keyboard: text typed or spoken on the phone is inserted at the caret
position in the PC's focused input field.

The phone page is a plain input surface — it has no visible editable box. A tap area raises the
phone's keyboard (including its built-in speech-to-text), and a read-only area shows the last text
that was sent, for verification.

## How it works

- The PC runs a local service that serves a static page over HTTP (port 8765) and accepts
  WebSocket connections (port 8766).
- The phone opens that page in a browser on the same network. It sends only the incremental
  changes of each edit to the PC.
- The PC service injects the text with the Windows `SendInput` API as Unicode keyboard events at
  the current caret, without selecting or replacing text already present in the field.

## Features

On the PC (Windows):

- Runs as a system-tray icon; no console window.
- Connection window listing the LAN addresses, with a QR code, a dropdown to choose which address
  the QR code encodes, and a copy button per address.
- Start-with-Windows toggle (adds or removes the entry under `HKCU\...\Run`). When launched with
  Windows it **starts quietly into the tray** and does not open the connection window; starting it
  manually (shortcut / Start Menu) still opens it. The entry is created with a `--startup`
  argument, and entries written by older versions are updated automatically on the next launch.
- Single instance: launching a second copy brings up the already-running window instead of opening
  another one or conflicting over ports.
- "About" window showing the version.

On the phone (any mobile browser):

- No app to install; open the page.
- Uses the phone's own keyboard and speech-to-text; recognized text is forwarded as plain text.
- Shortcut keys: arrows / Home / End / Delete / Tab / Esc / Enter, Ctrl combinations
  (select-all / copy / paste / cut / undo), and Backspace.
- Clear button: clears the PC's focused box (no Enter is sent).

Text insertion:

- Inserted at the caret via `SendInput`. Handles Chinese, English and newlines. The target
  application needs no modification.

## Architecture

```
phone browser (www/index.html) --WebSocket 8766--> PC service (server.py) --SendInput--> focused window
phone browser  <--HTTP 8765 serves the page--
```

## Install

### Windows (recommended — use the installer)

Download `PhoneTypeSetup.exe` from the releases page, run it, and click Install. The installer:

- installs to `%LOCALAPPDATA%\Programs\PhoneType` (per-user, **no administrator rights needed**)
- creates a Start Menu shortcut
- registers an entry in "Add or remove programs" so it can be uninstalled at any time

If PhoneType is currently running, the installer **closes it first** before continuing.

Run the installer again to:

- **Update** — the button becomes "Update to x.x.x" when the installed version is older
- **Reinstall** — overwrites the installation when versions match
- **Uninstall** — closes a running instance and removes the install directory, shortcuts and
  registry entries (including the start-with-Windows entry)

Command line:

```
PhoneTypeSetup.exe /SILENT            silent install or update
PhoneTypeSetup.exe /uninstall         uninstall (with confirmation)
PhoneTypeSetup.exe /uninstall /SILENT silent uninstall
```

Silent runs write a log to `%TEMP%\PhoneType_setup.log`.

### Portable (no install)

Download `PhoneType.exe` from the releases page and run it from any folder. It is also a
standalone executable with Python and dependencies bundled, but writes nothing to the registry and
creates no shortcuts — deleting the file removes it.

### Run from source

Requires Python 3.10+ on Windows.

```bash
pip install -r requirements.txt
python tray_app.py     # tray version (recommended)
# or
python server.py       # console version, prints the address to the terminal
```

## Usage

1. Start PhoneType on the PC. The connection window lists LAN addresses and a QR code.
2. On the phone, on the same Wi-Fi, open `http://<PC-IP>:8765/` or scan the QR code.
3. Click the target window on the PC so it has focus.
4. Tap the trigger area on the phone page to raise the keyboard, then type or speak. The text is
   inserted at the PC caret.

If the QR code does not connect, pick another address from the dropdown.

Enter mapping (dropdown at the top-right of the phone page, default `Enter`) selects which
key combination the PC receives when Enter is pressed on the phone keyboard:

- `Enter` (default): triggers send / submit.
- `Shift+Enter`: a newline in most chat apps, so a stray tap will not send the message.
- `Ctrl+Enter`
- `Alt+Enter`

## Input model

The phone page keeps a hidden input as the IME carrier rather than a visible editable box, and
sends only the delta of each edit: inserted text is typed at the PC caret, deletions are forwarded
as Backspace keystrokes. A full reset of the carrier sends nothing, so PC content is not deleted
unintentionally — use the clear button to clear the PC box.

Text already present in the PC's focused field is left intact; phone input continues from the
current caret.

## Limitations

- Windows only (input injection uses the Windows `SendInput` API).
- The PC and the phone must be on the same network.
- Transport is plaintext WebSocket over the LAN with no authentication. Anyone on the same network
  who knows the address can type into the PC, so only use it on a network you trust.
- Single direction: edits made on the PC are not reflected back to the phone (by design, to avoid
  UI Automation dependencies).
- Deletions from the phone are sent as Backspace at the caret and may not correspond to a
  selection on the PC. For complex edits, edit on the PC directly or use the clear button.

## Source layout

- `server.py` — HTTP + WebSocket service and Windows input injection.
- `tray_app.py` — system-tray host (reuses `server.py`); connection info / QR code, About,
  start-with-Windows.
- `www/index.html` — phone client.
- `installer.py` — the installer: install / update / uninstall, closing a running instance first.
- `scripts/make_icon.py` — regenerates the application icon (`assets/app.ico`).
- `build_exe.bat` — builds the main program `dist/PhoneType.exe`.
- `build_installer.bat` — builds the installer `dist/PhoneTypeSetup.exe`
  (run `build_exe.bat` first).
- `requirements.txt` — Python dependencies.

## Build the executables

```bat
build_exe.bat          :: produces dist\PhoneType.exe (PyInstaller, onefile, windowed)
build_installer.bat    :: produces dist\PhoneTypeSetup.exe (bundles the program above)
```

## License

License not specified; source provided as-is.
