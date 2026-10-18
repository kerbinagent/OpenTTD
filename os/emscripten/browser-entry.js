/* Native browser entry. Cookies are selection hints; native keys authorize play. */
(function(root) {
  'use strict';
  function iniSetting(text, section, key) {
    var lines = text.split('\n'), inside = false, found = false, insert = lines.length;
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].length > 1022) throw new Error('Configuration line exceeds native parser bounds');
      var trimmed = lines[i].replace(/^[ \t\r\f\v]+|[ \t\r\f\v]+$/g, '');
      var group = trimmed.match(/^\[([^\]]*)\]$/);
      if (trimmed.startsWith('[') && !group) throw new Error('Malformed native configuration group');
      if (group) {
        if (inside) { insert = i; break; }
        if (!found && group[1] === section) { inside = found = true; insert = i + 1; }
      } else if (inside) {
        insert = i + 1;
        if (!trimmed || /^[;#]/.test(trimmed)) continue;
        var item = trimmed.startsWith('"') ? trimmed.match(/^"([^"]*)"?(.*)$/) : trimmed.match(/^([^=\t ]*)(.*)$/);
        if (item && item[1] === key) {
          var value = item[2].replace(/^[=\t ]+/, '');
          if (value.startsWith('"')) { value = value.slice(1); if (value.endsWith('"')) value = value.slice(0, -1); }
          return { lines: lines, index: i, value: value };
        }
      }
    }
    return { lines: lines, index: -1, insert: insert, found: found };
  }
  function setIni(text, section, key, value) {
    var item = iniSetting(text, section, key), line = key + ' = ' + value;
    if (item.index >= 0) item.lines[item.index] = line;
    else if (item.found) item.lines.splice(item.insert, 0, line);
    else item.lines.push('[' + section + ']', line, '');
    return item.lines.join('\n');
  }
  function prepareIdentity(fs, crypto, directory, canonical) {
    function read(path) { try { return fs.readFile(path, {encoding: 'utf8'}); } catch (error) { if (error.errno !== 44) throw error; return ''; } }
    var configPath = directory + '/openttd.cfg', config = read(configPath);
    if (!config) { config = '[version]\nini_version = 8\n'; fs.writeFile(configPath, config); }
    var version = iniSetting(config, 'version', 'ini_version').value;
    var prefix = (version || '').match(/^\d+/), number = prefix ? Number(prefix[0]) : 0;
    var modern = number <= 4294967295 && number % 65536 >= 1;
    var secretPath = modern ? directory + '/secrets.cfg' : configPath;
    var content = modern ? read(secretPath) : config;
    var secret = iniSetting(content, 'network', 'client_secret_key').value;
    if (canonical !== undefined && !/^[0-9a-fA-F]{64}$/.test(canonical)) throw new Error('Invalid browser identity');
    if (canonical !== undefined || !/^[0-9a-fA-F]{64}$/.test(secret || '')) {
      secret = canonical === undefined ? Array.from(crypto.getRandomValues(new Uint8Array(32)), function(b) { return b.toString(16).padStart(2, '0'); }).join('') : canonical;
      fs.writeFile(secretPath, setIni(content, 'network', 'client_secret_key', secret));
    }
    return secret;
  }
  function persistIdentity(indexedDB, candidate) {
    return new Promise(function(resolve, reject) {
      var open = indexedDB.open('openttd-browser-identity', 1), settled = false;
      function fail() { settled = true; reject(new Error('Browser identity storage unavailable')); }
      open.onerror = open.onblocked = fail;
      open.onupgradeneeded = function() { open.result.createObjectStore('identity'); };
      open.onsuccess = function() {
        var db = open.result;
        if (settled) { db.close(); return; }
        db.onversionchange = function() { db.close(); };
        var transaction, store, chosen;
        try { transaction = db.transaction('identity', 'readwrite'); store = transaction.objectStore('identity'); }
        catch (_) { db.close(); fail(); return; }
        transaction.onabort = transaction.onerror = function() { db.close(); fail(); };
        transaction.oncomplete = function() { db.close(); resolve(chosen); };
        var get = store.get('native-key');
        get.onsuccess = function() {
          chosen = get.result === undefined ? candidate : get.result;
          if (typeof chosen !== 'string' || !/^[0-9a-fA-F]{64}$/.test(chosen)) { transaction.abort(); return; }
          if (get.result === undefined) store.add(chosen, 'native-key');
        };
      };
    });
  }
  function cookieName(port) { return 'openttd_company_' + port; }
  function remembered(cookie, port, world) {
    if (!world) return null;
    try {
      var prefix = cookieName(port) + '=', raw = cookie.split(';').map(function(s) { return s.trim(); }).find(function(s) { return s.startsWith(prefix); });
      var saved = JSON.parse(decodeURIComponent((raw || '').slice(prefix.length)));
      return saved.world === world && Number.isInteger(saved.company) && saved.company >= 0 && saved.company < 15 ? saved.company : null;
    } catch (_) { return null; }
  }
  function init(config, load) {
    var welcome = document.getElementById('welcome'), status = document.getElementById('welcome-status');
    var human = document.getElementById('human-options'), agent = document.getElementById('agent-options');
    var label = document.getElementById('session-label'), home = document.getElementById('game-home');
    var port = location.port, saved = remembered(document.cookie, port, config.worldId), started = false;
    var cookieSecurity = location.protocol === 'https:' ? '; Secure' : '';
    function show(message) { status.textContent = message; }
    function forget() { document.cookie = cookieName(port) + '=; Path=/; Max-Age=0; SameSite=Strict' + cookieSecurity; }
    function fail(message) {
      welcome.hidden = false; show(message);
      document.getElementById('retry-entry').hidden = false;
      ['choose-observe', 'new-company', 'existing-company', 'resume-company'].forEach(function(id) { document.getElementById(id).disabled = true; });
    }
    document.getElementById('retry-entry').addEventListener('click', function() { location.hash = 'choose'; location.reload(); });
    root.openttd_browser_join_failed = function() { forget(); fail('That company is no longer available. Return to start to create a company or choose another existing company.'); };
    home.addEventListener('click', function() { location.hash = 'choose'; location.reload(); });
    root.openttd_browser_storage_failed = function() { fail('Your browser could not save the game identity. Allow site storage and reload to connect safely.'); };
    async function start(mode, company) {
      if (started) return;
      started = true;
      show('Connecting…');
      try {
        async function launch() {
          if (mode === 'human' && location.hash === '#choose') history.replaceState(null, '', location.pathname + location.search);
          Object.defineProperty(root, 'OPENTTD_BROWSER_SESSION', {value: Object.freeze({readOnly: mode === 'observe'}), configurable: false, writable: false});
          config.company = company;
          var waitingForNewCompany = company === 254;
          root.openttd_browser_company = function(assigned) {
            if (mode === 'observe') { label.textContent = 'Observer · Enter to chat'; return; }
            if (Number.isInteger(assigned) && assigned >= 0 && assigned < 15) {
              waitingForNewCompany = false;
              label.textContent = 'Playing · Company #' + (assigned + 1);
              if (config.worldId) document.cookie = cookieName(port) + '=' + encodeURIComponent(JSON.stringify({world: config.worldId, company: assigned})) + '; Path=/; Max-Age=31536000; SameSite=Strict' + cookieSecurity;
            } else if (!waitingForNewCompany) {
              forget();
              label.textContent = 'Choose a company in the Players window';
            }
          };
          welcome.hidden = true;
          home.hidden = label.hidden = false;
          label.textContent = mode === 'observe' ? 'Observer · Enter to chat' : company === 254 ? 'Creating your company…' : 'Connecting to your company…';
          load();
          await new Promise(function() {}); // Hold the origin lock until the page closes.
        }
        if (navigator.locks) {
          await navigator.locks.request('openttd-native-client', {ifAvailable: true}, async function(lock) {
            if (!lock) { started = false; show('A game is already open in another tab. Close that tab, then try again.'); return; }
            await launch();
          });
        } else {
          await launch();
        }
      } catch (error) { fail('Could not start the game. Reload to try again.'); console.error(error); }
    }
    document.getElementById('choose-observe').addEventListener('click', function() { void start('observe', 255); });
    document.getElementById('choose-human').addEventListener('click', function() { human.hidden = false; agent.hidden = true; show(''); });
    document.getElementById('new-company').addEventListener('click', function() { void start('human', 254); });
    document.getElementById('existing-company').addEventListener('click', function() { void start('human', 255); });
    var resume = document.getElementById('resume-company');
    resume.hidden = saved === null;
    if (saved !== null) {
      resume.textContent = 'Continue Company #' + (saved + 1);
      resume.addEventListener('click', function() { void start('human', saved + 1); });
    }
    var prompt = document.getElementById('agent-prompt'), mode = document.getElementById('agent-mode'), goal = document.getElementById('agent-goal');
    function updatePrompt() { prompt.value = config.apiOrigin ? 'Read ' + config.apiOrigin + (mode.value === 'capture' ? '/llms-capture.txt' : '/llms.txt') + ' and start playing. Your goal: ' + goal.value : 'Agent joining is not configured for this server.'; }
    mode.addEventListener('change', updatePrompt); goal.addEventListener('input', updatePrompt);
    document.getElementById('choose-agent').addEventListener('click', function() { agent.hidden = false; human.hidden = true; updatePrompt(); show(''); });
    document.getElementById('copy-agent-prompt').addEventListener('click', async function() {
      try {
        if (navigator.clipboard) await navigator.clipboard.writeText(prompt.value);
        else { prompt.focus(); prompt.select(); if (!document.execCommand('copy')) throw new Error('Copy unavailable'); }
        show('Prompt copied. Paste it into your agent harness to join the game.');
      }
      catch (_) { prompt.focus(); prompt.select(); show('Select and copy the prompt, then paste it into your agent harness.'); }
    });
    if (saved !== null && location.hash !== '#choose') void start('human', saved + 1);
  }
  root.OpenTTDBrowser = {init: init, prepareIdentity: prepareIdentity, persistIdentity: persistIdentity, iniSetting: iniSetting, remembered: remembered};
})(typeof window === 'undefined' ? globalThis : window);
