Module.arguments.push('-mnull', '-snull', '-vsdl');
/* This public bootstrap is provided only by the fixed loopback relay. */
var browserRelay = window.OPENTTD_BROWSER_RELAY;
var localRelay = browserRelay &&
    ((location.protocol === 'http:' && (location.hostname === '127.0.0.1' || location.hostname === 'localhost')) ||
     (location.origin === browserRelay.publicOrigin && (location.protocol === 'http:' || location.protocol === 'https:'))) &&
    Number.isInteger(browserRelay.nativePort) &&
    browserRelay.nativePort > 0 && browserRelay.nativePort <= 65535 &&
    Number.isInteger(browserRelay.company) &&
    ((browserRelay.company >= 1 && browserRelay.company <= 15) || browserRelay.company === 254 || browserRelay.company === 255) &&
    browserRelay.websocketPath === '/native';
if (localRelay) {
    Module.arguments.push('-n', '127.0.0.1:' + browserRelay.nativePort + '#' + browserRelay.company,
        '-c', '/home/web_user/.openttd/openttd.cfg', '-I', 'OpenGFX');
}
Module['websocket'] = { url: function(host, port, proto) {
    if (localRelay) {
        return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host +
            (host === '127.0.0.1' && Number(port) === browserRelay.nativePort && proto === 'tcp'
                ? browserRelay.websocketPath : '/denied-native-destination');
    }
    /* openttd.org hosts a WebSocket proxy for the content service. */
    if (host == "content.openttd.org" && port == 3978 && proto == "tcp") {
        return "wss://bananas-server.openttd.org/";
    }

    /* Everything else just tries to make a default WebSocket connection.
     * If you run your own server you can setup your own WebSocket proxy in
     * front of it and let people connect to your server via the proxy. You
     * are best to add another "if" statement as above for this. */

    if (location.protocol === 'https:') {
        /* Insecure WebSockets do not work over HTTPS, so we force
         * secure ones. */
        return 'wss://';
    } else {
        /* Use the default provided by Emscripten. */
        return null;
    }
} };

Module.preRun.push(function() {
    personal_dir = '/home/web_user/.openttd';
    content_download_dir = personal_dir + '/content_download'

    /* Because of the "-c" above, all user-data is stored in /user_data. */
    FS.mkdir(personal_dir);
    FS.mount(IDBFS, {}, personal_dir);

    Module.addRunDependency('syncfs');
    FS.syncfs(true, function (err) {
        if (localRelay) {
            if (err) { window.openttd_browser_storage_failed(); return; }
            var candidate;
            try { candidate = window.OpenTTDBrowser.prepareIdentity(FS, window.crypto, personal_dir); }
            catch (error) { window.openttd_browser_storage_failed(); return; }
            window.OpenTTDBrowser.persistIdentity(window.indexedDB, candidate).then(function(key) {
                try { window.OpenTTDBrowser.prepareIdentity(FS, window.crypto, personal_dir, key); }
                catch (error) { window.openttd_browser_storage_failed(); return; }
                FS.syncfs(false, function (saveError) {
                    if (saveError) { window.openttd_browser_storage_failed(); return; }
                    Module.removeRunDependency('syncfs');
                });
            }, window.openttd_browser_storage_failed);
        } else {
            Module.removeRunDependency('syncfs');
        }
    });

    window.openttd_syncfs_shown_warning = false;
    window.openttd_syncfs = function(callback) {
        /* Copy the virtual FS to the persistent storage. */
        FS.syncfs(false, function (err) {
            /* On first time, warn the user about the volatile behaviour of
             * persistent storage. */
            if (!window.openttd_syncfs_shown_warning) {
                window.openttd_syncfs_shown_warning = true;
                Module.onWarningFs();
            }

            if (callback) callback();
        });
    }

    window.openttd_exit = function() {
        window.openttd_syncfs(Module.onExit);
    }

    window.openttd_abort = function() {
        window.openttd_syncfs(Module.onAbort);
    }

    window.openttd_bootstrap = function(current, total) {
        Module.onBootstrap(current, total);
    }

    window.openttd_bootstrap_failed = function() {
        Module.onBootstrapFailed();
    }

    window.openttd_bootstrap_reload = function() {
        window.openttd_syncfs(function() {
            Module.onBootstrapReload();
            setTimeout(function() {
                location.reload();
            }, 1000);
        });
    }

    window.openttd_server_list = function() {
        add_server = Module.cwrap("em_openttd_add_server", null, ["string"]);

        /* Add servers that support WebSocket here. Examples:
         *  add_server("localhost");
         *  add_server("localhost:3979");
         *  add_server("127.0.0.1:3979");
         *  add_server("[::1]:3979");
         */
    }

    var leftButtonDown = false;
    document.addEventListener("mousedown", e => {
        if (e.button == 0) {
            leftButtonDown = true;
        }
    });
    document.addEventListener("mouseup", e => {
        if (e.button == 0) {
            leftButtonDown = false;
        }
    });
    window.openttd_open_url = function(url, url_len) {
        const url_string = UTF8ToString(url, url_len);
        function openWindow() {
            document.removeEventListener("mouseup", openWindow);
            window.open(url_string, '_blank');
        }
        /* Trying to open the URL while the mouse is down results in the button getting stuck, so wait for the
         * mouse to be released before opening it. However, when OpenTTD is lagging, the mouse can get released
         * before the button click even registers, so check for that, and open the URL immediately if that's the
         * case. */
        if (leftButtonDown) {
            document.addEventListener("mouseup", openWindow);
        } else {
            openWindow();
        }
    }

    /* https://github.com/emscripten-core/emscripten/pull/12995 implements this
    * properly. Till that time, we use a polyfill. */
   SOCKFS.websocket_sock_ops.createPeer_ = SOCKFS.websocket_sock_ops.createPeer;
   SOCKFS.websocket_sock_ops.createPeer = function(sock, addr, port)
   {
       let func = Module['websocket']['url'];
       Module['websocket']['url'] = func(addr, port, (sock.type == 2) ? 'udp' : 'tcp');
       try {
           return SOCKFS.websocket_sock_ops.createPeer_(sock, addr, port);
       } finally {
           Module['websocket']['url'] = func;
       }
   }
});
