## How to build with Emscripten

Please use docker with the supplied `Dockerfile` to build for emscripten.
It takes care of a few things:
- Use a version of emscripten we know works.
- Add LibLZMA library under contrib ports.

First, build the docker image by navigating in the folder this `README.md` is in, and executing:
```
  docker build -t emsdk-openttd .
```

Next, navigate back to the root folder of this project.

Now we build the host tools first:
```
  mkdir build-host
  docker run -it --rm -v $(pwd):$(pwd) -u $(id -u):$(id -g) --workdir $(pwd)/build-host emsdk-openttd cmake .. -DOPTION_TOOLS_ONLY=ON
  docker run -it --rm -v $(pwd):$(pwd) -u $(id -u):$(id -g) --workdir $(pwd)/build-host emsdk-openttd make -j$(nproc) tools
```

Finally, we build the actual game:
```
  mkdir build
  docker run -it --rm -v $(pwd):$(pwd) -u $(id -u):$(id -g) --workdir $(pwd)/build emsdk-openttd emcmake cmake .. -DHOST_BINARY_DIR=../build-host -DCMAKE_BUILD_TYPE=Release -DOPTION_USE_ASSERTS=OFF -DOPTION_INTEGRATION=OFF
```

For the local relay, copy an installed OpenGFX base graphics directory, including
its `.obg`, `.grf` and license files, into `build/baseset/` before linking. The
link preloads that directory into `openttd.data`; relay mode uses OpenGFX without
a separate content-service connection. Then build:

```
  docker run -it --rm -v $(pwd):$(pwd) -u $(id -u):$(id -g) --workdir $(pwd)/build emsdk-openttd emmake make -j$(nproc)
```

In the `build` folder you will now see `openttd.html`.

To run it locally, you would have to start a local webserver; something like:

```
  cd build
  python3 -m http.server
```

You can now play the game via http://127.0.0.1:8000/openttd.html .

## Fixed loopback native relay

For a matching native server, serve the isolated public build with the host's
existing aiohttp installation (tested with 3.13.2):

```
python3 tools/browser_client.py /absolute/path/to/public-build
```

Open http://127.0.0.1:18087/ (or http://localhost:18087/). The relay always binds
127.0.0.1 by default and connects only to 127.0.0.1:3979. Operator options `--port` and
`--native-port` select finite ports at startup; browser requests cannot change
them. The entry page offers **Observe**, **Run a company**, and **Bring your
agent**. Observation joins as a native spectator and blocks outbound gameplay,
company changes and remote-console commands. It creates no company.
Press **Enter** as an observer to send world chat. Observers cannot send
company or private chat. Human play offers native new-company creation or the
native Players window to
join an existing company, subject to the usual company permissions.

Pass `--world-id WORLD_EPOCH --api-port API_PORT` for company recovery and agent
prompts. Use the world's stable epoch, and change it when replacing that world.
The browser eagerly saves its native authentication key in IndexedDB before
connecting. A cookie remembers only the server-confirmed last company and world;
refreshing or returning with the same browser profile resumes that company.
**Switch mode / company** returns to all three options, including a new company
or an existing one. A removed or unavailable company requires an explicit new
choice. Cookies are selection hints; native public-key permissions still apply.
A different device/profile has its own identity and needs permission to join a
private company. An atomic per-origin IndexedDB record keeps the native key
consistent across tabs, including HTTP where Web Locks are unavailable. Existing
native keys migrate unchanged. When Web Locks are available, an exclusive lock
also prevents concurrent preference writes; otherwise preferences may be saved
in last-writer order without changing the shared native identity.

The agent option displays a configurable-goal prompt and a copy button for
`/llms.txt` or `/llms-capture.txt` at the configured API origin. It allocates no
company and loads no WASM; the agent claims its company through the public guide.
Enable `--agent-joins` on the game launcher for that public admission flow.

Build the browser and server from matching gameplay/network sources and version
metadata. The browser client uses ordinary multiplayer networking and does not
need the integration IPC server. If changing `pre.js` or `shell.html` after a
build, remove the generated `openttd.html` before rebuilding to force relinking;
these frontend inputs may not trigger the link automatically.

Only openttd.html, openttd.js, openttd.wasm and openttd.data from the build, plus
the two fixed entry assets and generated config, are served by default. With
`--api-port`, the same origin serves the five current public agent assets and
forwards `/v1/` requests to that one fixed loopback API. It passes caller
authorization without adding its metadata credential or browser cookies, keeps
capture metadata/SSE streaming, and bounds bodies, responses and connections.
The native engine and backend need no restart. Symlinks and unrelated paths are
rejected. The process supports four concurrent binary
WebSocket connections, bounds messages and writes, and releases connections on
failure. Exact local Host and Origin checks guard browser cross-site access;
they do not authenticate other local processes. Native play uses no API credential.
The Fullscreen button uses the browser Fullscreen API. SDL2 follows the viewport
resize and updates the native game surface and input coordinates. Native SDL
handles pointer, wheel and keyboard input.

The browser uses SDL's high-DPI framebuffer and the bundled OpenTTD TrueType
fonts through the Emscripten FreeType port. Canvas pixels follow display scaling,
including fractional scaling, window resizing and fullscreen. Pointer positions
are converted from SDL window coordinates into game pixels. The native Game
options retain their interface scale and sprite-font preferences; automatic
interface scaling adjusts to the framebuffer resolution.

For access across your own Tailscale machines, bind the browser listener directly
to its exact Tailscale IPv4 address. The game and API remain on loopback:

```
python3 tools/browser_client.py /absolute/path/to/public-build \
  --host 100.64.1.2 --port 18087 --native-port 3979 \
  --world-id WORLD_EPOCH --api-port 8080
```

Open `http://100.64.1.2:18087/` from another tailnet machine. Tailscale Serve is
not required. The HTTP copy button uses its synchronous browser fallback, and
identity persistence does not require a secure context. The public guides tell
agents to explicitly opt in with `OPENTTD_ALLOW_TAILSCALE_HTTP=1`; the portable
client permits only literal 100.64.0.0/10 addresses under that opt-in and bypasses
environment HTTP proxies so traffic uses the tailnet directly. Other remote
HTTP origins retain the normal HTTPS requirement. The guide bootstrap also
bypasses environment proxies for literal Tailscale HTTP downloads.

Exact configured Host and Origin checks remain in place. An optional HTTPS
reverse proxy can still use `--public-origin` and `--public-api-origin`, with WSS
on HTTPS. Direct binding never enables Tailscale Funnel or changes tailnet policy.

To display agent model and effort reports, optionally pair these operator options:

```
python3 tools/browser_client.py /absolute/path/to/public-build \
  --metadata-port 18085 --metadata-token-file /absolute/path/to/human.token
```

The **Model reports** panel lists submitted model and effort reports with their
company number. Reports are supplied by agents through `PUT /v1/me/model` and
are labels rather than verified runtime information. Slots without a report
are omitted; the panel does not indicate live agent presence. The panel refreshes on opening and every 15 seconds while
open; failures clear old labels and display unavailable. Closing it stops polling.

The relay holds the supplied credential on the server and uses it only for a
fixed `GET http://127.0.0.1:<metadata-port>/v1/participants`. Its public
`/model-roster` response contains only company ID, display name, participant kind
and optional model report. Credentials, identity IDs, run IDs and native handles
never reach the page. Requests are limited to three seconds and 64 KiB, and
redirects are rejected. Both options are required together. The chosen API must
support model reports; leave both options out for native play without the panel.

Run isolated relay tests without a game server:

```
python3 -m unittest discover -s tests/integration -p test_browser_client.py -v
node --test os/emscripten/model-roster.test.mjs
node --test os/emscripten/browser-entry.test.mjs
```
