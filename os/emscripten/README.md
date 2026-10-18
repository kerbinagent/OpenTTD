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
them. The public bootstrap joins native company 0 (`#1`). Use a native server
with a matching protocol revision and a human-open company.

Build the browser and server from matching gameplay/network sources and version
metadata. The browser client uses ordinary multiplayer networking and does not
need the integration IPC server. If changing `pre.js` or `shell.html` after a
build, remove the generated `openttd.html` before rebuilding to force relinking;
these frontend inputs may not trigger the link automatically.

Only openttd.html, openttd.js, openttd.wasm and openttd.data are served; symlinks
and all other paths are rejected. The process supports four concurrent binary
WebSocket connections, bounds messages and writes, and releases connections on
failure. Exact local Host and Origin checks guard browser cross-site access;
they do not authenticate other local processes. No API credentials are used.
The Fullscreen button invokes the exported Emscripten `requestFullscreen`
helper with canvas resizing enabled; keep `requestFullscreen` in the build's
`EXPORTED_RUNTIME_METHODS`. Native SDL handles pointer, wheel and keyboard input.

Run isolated relay tests without a game server:

```
python3 -m unittest discover -s tests/integration -p test_browser_client.py -v
```
