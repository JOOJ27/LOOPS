# LOOPS

LOOPS is an Omegle-style video chat that runs in a terminal. It turns camera frames into ASCII art or colored terminal cells, then pairs you with another person for a one-to-one conversation. Start it with one command:

```sh
loops
```

The client connects to the matchmaking service, waits for a partner, and then shares video, audio, and text chat over a WebSocket connection.

## Features

- Terminal video using ASCII shading or xterm-256 colors.
- Live microphone and speaker audio, with echo cancellation.
- Text chat and commands to mute, skip, or change the video style.
- Automatic reconnection while the hosted service is starting up.
- Installable Linux and macOS packages, plus a standalone Windows executable.

## Architecture

The repository keeps the terminal client and server in separate branches. The checked-out `client` branch contains the terminal client and its packaging scripts; the `server` branch contains `Server.py` and the server requirements. The client connects to the hosted service at `wss://loops-9j0l.onrender.com`.

The Render service runs the WebSocket server from the `server` branch. There is no Render blueprint in that branch, so the dashboard's saved build and start settings are not represented in the repository. The Python entry point is `Server.py`; it binds to `0.0.0.0` and reads the port from `PORT` (default `8765`).

### Hosted service and pairing flow

The server keeps two in-memory collections: a FIFO `waiting` deque for clients without a partner, and a `partners` dictionary mapping each paired WebSocket to the other. When a client connects, `pair_client` removes the first eligible waiting client from the deque and records both directions in the dictionary. If nobody is waiting, the new connection is appended to the deque.

After pairing, the server sends both clients a `system` message with `"message": "Partner found!"`. A client with no match receives `"Waiting for someone else..."`. The server broadcasts `server_stats` containing `people` (waiting clients plus paired clients) and `pairs` (half the number of partner-map entries) after connections, skips, and disconnects.

The server handles `/skip` as a `skip` message. It removes the current pair, tells the former partner that the other person left, then tries to match that former partner with another person who is waiting. It then searches for a new match for the skipping client while excluding the just-left partner, which prevents an immediate rematch when others are available. Either client is put back in the waiting deque when no eligible match is available.

The server accepts `chat`, `video`, and `audio` JSON messages only from clients that currently have a partner. It forwards each accepted JSON message to the partner without decoding or transforming the media payload. When a client disconnects, the handler removes it from the waiting deque or partner map, notifies its former partner, and tries to match that partner with another waiting client. Pairing state lives only in process memory; a server restart clears the waiting queue and active pairs.

The client retries its initial connection indefinitely if the service is unavailable, using a 20-second connection timeout and a 3-second delay between attempts. This lets it wait while a sleeping Render service starts up.

To run the server locally, check out the `server` branch, install its requirements, and start the entry point:

```sh
git switch server
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python Server.py
```

The server uses `PORT` when it is set by the hosting environment and otherwise listens on port `8765`.

### Async tasks and threads

Both sides use `asyncio`, but only the client needs device callback threads. On the server, each WebSocket connection is handled by an async coroutine on one event loop. The server does not create worker threads: network reads, partner sends, pairing notifications, and statistics broadcasts are awaited as coroutines. Pair state is held in the process-wide deque and dictionary described above.

On the client, the terminal interface and the `asyncio` event loop run together on the main thread. The event loop starts separate tasks for receiving messages, capturing/sending video, and capturing/sending audio. The UI polls the keyboard without blocking and yields control regularly, so the network tasks can continue while the user types or the screen redraws.

Some device operations use callback threads:

- OpenCV camera capture and frame conversion run through `asyncio.to_thread`, so a slow camera read does not freeze the event loop.
- `sounddevice` invokes microphone and speaker callbacks on audio threads. The microphone callback uses `loop.call_soon_threadsafe` to put encoded audio onto an `asyncio.Queue`; the async task drains the queue and sends it over the WebSocket.
- The speaker callback reads audio chunks from a thread-safe playback buffer. A lock also protects the echo-reference buffer shared between audio callbacks.

If the WebSocket's outgoing buffer grows beyond 32 KB, the client skips a video frame. Audio and chat are allowed to continue, since a delayed video frame is less useful than delayed sound or messages.

### JSON messages

Each WebSocket application message is a JSON object with a `type` field. The client sends these message shapes:

| Type | Fields | Purpose |
| --- | --- | --- |
| `video` | `frame`, optional `colors` and `cols` | New terminal-rendered camera frame. `frame` is newline-separated text. `colors` is a base64-encoded, zlib-compressed matrix of xterm color indexes; `cols` gives its width. |
| `audio` | `data` | Base64-encoded mono PCM audio samples. |
| `chat` | `text` | A text message. |
| `skip` | — | Request to leave the current pair and find another person. |

The client handles incoming `video`, `audio`, and `chat` messages, plus:

| Type | Fields | Purpose |
| --- | --- | --- |
| `system` | `message` | Pairing and status updates, including partner found or left. |
| `server_stats` | `people`, `pairs` | Counts displayed on the waiting screen. |

The server sends only the `system` and `server_stats` messages itself. For paired clients, it relays `video`, `audio`, and `chat` JSON objects unchanged to the other WebSocket.

The client validates received color data before using it, including checking that the decompressed matrix has the expected dimensions.

### Camera and terminal video

`Camera.py` captures the default camera with OpenCV and mirrors the image horizontally. It scales each frame to fit the terminal's video panel while accounting for the different width-to-height ratio of a terminal character cell.

For the monochrome image, the client applies a light unsharp mask, converts the frame to grayscale, resizes it with area interpolation, and maps brightness to an ASCII ramp. The ramp can be switched to block characters with `/type`.

When color is enabled and the terminal supports at least 256 colors, the client resizes the original, unsharpened BGR frame and maps each pixel to the closest xterm-256 color. It uses the 6×6×6 color cube for saturated colors and the grayscale ramp for low-saturation pixels. In color mode, terminal cells are painted with those colors; if 256-color pairs are unavailable, the client falls back to monochrome ASCII.

Video is sent only while the client is paired. The target rate is 30 frames per second, and the client can skip a frame when the connection is congested.

### Audio and echo cancellation

The audio stream uses 16 kHz, mono, signed 16-bit PCM on the network. The default chunk size is 320 samples, or 20 ms. The client checks available input and output devices and tries suitable sample-rate and channel combinations when a device does not accept 16 kHz mono directly. It resamples locally as needed; downsampling applies a low-pass filter to reduce aliasing.

The microphone callback converts captured audio to the network format, passes it through `pywebrtc-audio` echo cancellation, and queues it for the async sender. The echo canceller receives a reference made from the audio actually sent to the speaker. That reference is kept in a bounded, lock-protected buffer and read by the microphone callback.

Incoming audio is decoded and queued for playback. The player adapts its startup buffer to playback stability, drops old chunks if latency grows too much, and fades short gaps to reduce clicks. Audio-device failures are reported in the status panel; the client can continue with video and chat if an input or output device is unavailable.

### Terminal interface and controls

`curses` draws the status area, local and partner video, chat history, and message input. The client uses non-blocking keyboard input and refreshes the layout when the terminal is resized. Chat history can be scrolled with Page Up and Page Down.

Enter sends a chat message. The following commands are entered in the message box:

| Command | Action |
| --- | --- |
| `/type` | Toggle between ASCII shading and block characters. |
| `/color` | Toggle color video, when the terminal supports 256 colors. |
| `/mic` | Mute or unmute the microphone. |
| `/skip` | Leave the current pair and wait for another person. |
| `/sair` | Exit LOOPS. |

The call layout needs a terminal at least 60 columns wide and 20 rows high. Connection and audio diagnostics are written to `~/.loops/loops.log`.

## Installation

### Linux

Run this command in a terminal. The installer detects Linux and the machine architecture, downloads the latest release, and installs LOOPS for the current user:

```sh
curl -fsSL https://raw.githubusercontent.com/JOOJ27/LOOPS/client/install.sh | sh
```

The launcher is installed at `~/.local/bin/loops`. If that directory is on your `PATH`, start the app with:

```sh
loops
```

Otherwise, run `~/.local/bin/loops` directly. The installer also adds an application-menu entry and, when available, a desktop shortcut.

### macOS

Run this command in Terminal. The installer detects Intel or Apple Silicon, downloads the corresponding latest release, and installs LOOPS for the current user:

```sh
curl -fsSL https://raw.githubusercontent.com/JOOJ27/LOOPS/client/install.sh | sh
```

Start it from a terminal with:

```sh
loops
```

If `~/.local/bin` is not on your `PATH`, run `~/.local/bin/loops`. The installer also creates `~/Applications/LOOPS.app`.

### Windows

In PowerShell, download the standalone x64 executable:

```powershell
curl.exe -L "https://github.com/JOOJ27/LOOPS/releases/latest/download/loops-windows-x64.exe" -o "$HOME\Downloads\loops-windows-x64.exe"
```

Run it from PowerShell with:

```powershell
& "$HOME\Downloads\loops-windows-x64.exe"
```

The Windows release is a portable executable; it does not have a separate setup wizard. A terminal is required for the interface. The packaged launcher opens a console if started without one.

## Install from source

LOOPS requires Python 3.10 or newer. Installing the project also installs the dependencies declared in `pyproject.toml`, including NumPy, OpenCV, `sounddevice`, `pywebrtc-audio`, and `websockets`.

### Linux and macOS

From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
loops
```

On Linux, audio support requires PortAudio. For Ubuntu or Debian, install it with:

```sh
sudo apt install libportaudio2
```

### Windows PowerShell

From the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
loops
```

The project declares `windows-curses` for Windows. If PowerShell blocks virtual-environment activation, use the environment's executables directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\loops.exe
```

## Uninstallation

### Linux

Run in a shell:

```sh
rm -rf "$HOME/.local/opt/loops" "$HOME/.local/bin/loops" "$HOME/.local/share/applications/loops.desktop"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || printf '%s\n' "$HOME/Desktop")"
rm -f "$DESKTOP_DIR/LOOPS.desktop"
```

To also remove the client log:

```sh
rm -rf "$HOME/.loops"
```

### macOS

Run in Terminal:

```sh
rm -rf "$HOME/.local/opt/loops" "$HOME/.local/bin/loops" "$HOME/Applications/LOOPS.app"
```

To also remove the client log:

```sh
rm -rf "$HOME/.loops"
```

### Windows PowerShell

Remove the downloaded portable executable:

```powershell
Remove-Item "$HOME\Downloads\loops-windows-x64.exe" -Force
```

To also remove the client log:

```powershell
Remove-Item "$HOME\.loops" -Recurse -Force -ErrorAction SilentlyContinue
```

If you moved the executable to another folder, remove it from that location instead.

## Packaging and releases

`packaging/build.py` uses PyInstaller to make a one-file console executable. It must run on the target operating system because PyInstaller does not cross-compile. To build on the current machine, install the project and PyInstaller, then run:

```sh
python -m pip install --upgrade pip pyinstaller .
python packaging/build.py
```

On Linux, install the PortAudio runtime library before building:

```sh
sudo apt install libportaudio2
```

The build writes release files under `dist/`:

| Platform | Package |
| --- | --- |
| Linux x64 / ARM64 | `loops-linux-<arch>.tar.gz` with executable, installer, desktop entry, and icon. |
| macOS x64 / ARM64 | `loops-macos-<arch>.zip` with executable, setup script, and app icon. |
| Windows x64 | `loops-windows-x64.exe`. |

The GitHub Actions workflow builds Linux x64 and ARM64, macOS Intel and Apple Silicon, and Windows x64 artifacts. Pushing a version tag such as `v0.9.0` builds and publishes a GitHub Release; manually running the workflow builds artifacts without creating a tagged release.

## Project files

| File | Purpose |
| --- | --- |
| `Client.py` | Connection lifecycle, WebSocket JSON protocol, async tasks, audio handling, and `curses` interface. |
| `Camera.py` | Camera capture, ASCII conversion, and xterm-256 color mapping. |
| `Textbox.py` | Non-blocking message input. |
| `Messagebox.py` | Chat history and terminal rendering. |
| `Config.py` | Server endpoint, frame rate, terminal layout, and audio constants. |
| `Animation.py` | Waiting-screen animation frames. |
| `packaging/` | Native executable build, launchers, installer, desktop entry, and icons. |
| `.github/workflows/build.yml` | Cross-platform CI builds and tagged release publishing. |
| `Server.py` (`server` branch) | WebSocket endpoint, in-memory matchmaking, message relay, and client statistics. |
| `requirements.txt` (`server` branch) | Server's WebSocket dependency. |
