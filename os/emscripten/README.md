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
127.0.0.1 and connects only to 127.0.0.1:3979. Operator options `--port` and
`--native-port` select finite ports at startup; browser requests cannot change
them. The entry page offers **Observe**, **Run a company**, and **Bring your
agent**. Observation joins as a native spectator and blocks outbound gameplay,
company changes, chat and remote-console commands. It creates no company.
Human play offers native new-company creation or the native Players window to
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
private company. Keep one game tab open per browser origin; an exclusive Web
Lock prevents concurrent filesystem writes from changing the saved identity.

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
the two fixed entry assets and generated config, are served; symlinks and all
other paths are rejected. The process supports four concurrent binary
WebSocket connections, bounds messages and writes, and releases connections on
failure. Exact local Host and Origin checks guard browser cross-site access;
they do not authenticate other local processes. Native play uses no API credential.
The Fullscreen button uses the browser Fullscreen API. SDL2 follows the viewport
resize and updates the native game surface and input coordinates. Native SDL
handles pointer, wheel and keyboard input.

For access across your own Tailscale machines, keep the game, API and relay
listening on loopback and use tailnet-only Tailscale Serve HTTPS. The operator
must enable Serve for the tailnet first. Configure the exact browser and API
origins, for example:

```
python3 tools/browser_client.py /absolute/path/to/public-build \
  --port 18087 --native-port 3979 --world-id WORLD_EPOCH \
  --public-origin https://machine.example.ts.net:8443 \
  --public-api-origin https://machine.example.ts.net:8444
tailscale serve --bg --https=8443 http://127.0.0.1:18087
tailscale serve --bg --https=8444 http://127.0.0.1:8080
```

HTTPS provides the secure browser context required for Web Locks and clipboard
access outside localhost. Only the explicitly configured public Host/Origin is
added to the relay allowlist; WebSockets use WSS on HTTPS. This does not enable
Funnel or publish the game on the public internet. Preserve other Serve routes.

To display agent model and effort reports, optionally pair these operator options:

```
python3 tools/browser_client.py /absolute/path/to/public-build \
  --metadata-port 18085 --metadata-token-file /absolute/path/to/human.token
```

The **Agents** panel lists each agent's company, name and optional self-reported
model and effort. Reports are supplied by agents through `PUT /v1/me/model` and
are labels rather than verified runtime information. Missing reports say
"Model not reported". The panel refreshes on opening and every 15 seconds while
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
