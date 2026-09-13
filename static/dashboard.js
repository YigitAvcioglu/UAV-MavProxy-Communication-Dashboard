const $ = (id) => document.getElementById(id);
let lastState = null;
let config = null;
let devices = [];
let masters = [];
let outs = [];
let busy = new Set();
let deviceEditorDirty = false;

const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmt = (value, suffix = '', digits = 1) => {
  if (value === null || value === undefined) return '-';
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(digits)}${suffix}` : '-';
};
const makeLocalId = () => {
  try {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID().replaceAll('-', '').slice(0, 10);
    }
  } catch (_) {}
  return `dev_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
};

const jsonFetch = async (url, options = {}) => {
  const response = await fetch(url, {headers: {'Content-Type':'application/json'}, cache:'no-store', ...options});
  const body = await response.json();
  if (!response.ok || body.ok === false) throw new Error(body.error || body.message || `HTTP ${response.status}`);
  return body;
};

function setupTabs() {
  document.querySelectorAll('.tab-button').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.tab-button').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(x => x.classList.remove('active'));
    button.classList.add('active');
    $(button.dataset.tab).classList.add('active');
  }));
}

async function performLinkControl(linkId, action) {
  const key = `${linkId}:${action}`;
  if (busy.has(key)) return;
  busy.add(key);
  try { await jsonFetch(`/api/link/${encodeURIComponent(linkId)}/${action}`, {method:'POST', body:'{}'}); }
  catch (error) { alert(error.message); }
  finally { busy.delete(key); await refreshState(); }
}


function renderTxMode(state) {
  const tx = state.tx_selection || {};
  const mode = tx.mode || 'manual';
  const manual = mode === 'manual';
  $('manualTxMode').classList.toggle('mode-active', manual);
  $('autoTxMode').classList.toggle('mode-active', !manual);
  $('txModeStatus').textContent = manual ? 'MANUAL' : 'AUTOMATIC';
  $('txModeStatus').className = `process-status ${manual ? 'manual-mode' : 'auto-mode'}`;

  if (manual) {
    $('txModeDetail').textContent = 'You select the command TX link using the “Select Command TX” button on the link cards.';
  } else if (tx.last_error) {
    $('txModeDetail').textContent = `Automatic selection error: ${tx.last_error}`;
  } else if (tx.candidate) {
    $('txModeDetail').textContent =
      `${tx.candidate} aday · ${Number(tx.candidate_age_s || 0).toFixed(1)} sn · ` +
      `switch margin ${Number(tx.switch_margin_pct || 0).toFixed(1)} points`;
  } else {
    $('txModeDetail').textContent =
      `Monitoring the highest-quality UP link · margin threshold ${Number(tx.switch_margin_pct || 0).toFixed(1)} points · ` +
      `hold ${Number(tx.hold_s || 0).toFixed(1)} s`;
  }
}

async function setTxMode(mode) {
  try {
    const result = await jsonFetch('/api/tx-selection', {
      method: 'POST',
      body: JSON.stringify({mode}),
    });
    await refreshState();
    return result;
  } catch (error) {
    alert(error.message);
  }
}

function renderLinks(state) {
  renderTxMode(state);
  const manualMode = (state.tx_selection?.mode || 'manual') === 'manual';
  $('activeTx').textContent = state.active_tx_name || 'NONE';
  $('activeSource').textContent = `Source: ${state.active_tx_source || '-'}`;
  $('rxUsed').textContent = `RX: ${(state.rx_used || []).join(', ') || 'NONE'}`;
  if (state.control_error) {
    $('systemMessage').textContent = state.control_error; $('systemMessage').style.color = '#fca5a5';
  } else if (state.stats_age_s === null || state.stats_age_s > 3) {
    $('systemMessage').textContent = 'Waiting for link telemetry; check the linkstats module.'; $('systemMessage').style.color = '#fbbf24';
  } else {
    $('systemMessage').textContent = `System running · linkstats ${state.stats_age_s.toFixed(1)} s ago`; $('systemMessage').style.color = '#86efac';
  }
  const enabledCount = state.links.filter(x => x.enabled).length;
  $('linkGrid').innerHTML = state.links.map(link => `
    <article class="link-card ${link.active_tx ? 'active' : ''}" style="border-top-color:${link.color_hex}">
      <div class="link-head"><h3>${esc(link.name)}</h3><span class="badge" style="background:${link.color_hex}">${esc(link.state)}</span></div>
      <div class="quality-number" style="color:${link.color_hex}">${Math.round(link.quality_pct)}%</div>
      <div class="progress"><div style="width:${link.quality_pct}%;background:${link.color_hex}"></div></div>
      <div class="metrics">
        <div class="metric"><span>Loss</span><strong>${fmt(link.loss_pct, '%')}</strong></div>
        <div class="metric"><span>Latency</span><strong>${fmt(link.delay_ms, ' ms', 0)}</strong></div>
        <div class="metric"><span>Telemetri</span><strong>${fmt(link.pkt_rate, ' pkt/s')}</strong></div>
        <div class="metric"><span>Relative Rate</span><strong>${fmt(link.rate_pct, '%', 0)}</strong></div>
        <div class="metric wide"><span>Signal</span><strong>${esc(link.signal || '-')}</strong></div>
      </div>
      <div class="tx-indicator">${link.active_tx ? '● COMMANDS TO THE UAV ARE SENT THROUGH THIS LINK' : ''}</div>
      <div class="actions">
        <button ${link.enabled ? 'disabled' : ''} onclick="window.linkAction('${encodeURIComponent(link.id)}','on')">ON</button>
        <button ${!link.enabled || enabledCount <= 1 ? 'disabled' : ''} onclick="window.linkAction('${encodeURIComponent(link.id)}','off')">OFF</button>
        <button ${!manualMode || !link.enabled || !link.up || link.active_tx ? 'disabled' : ''} onclick="window.linkAction('${encodeURIComponent(link.id)}','select')">Select Command TX</button>
      </div>
    </article>`).join('');
}

function addDeviceRow(device = {}) {
  collectDevicesFromDom();
  devices.push({
    id: device.id || makeLocalId(),
    name: device.name || 'New Device',
    host: device.host || '',
    enabled: device.enabled !== false,
  });
  deviceEditorDirty = true;
  renderDevices(lastState, true);
}

function removeDevice(index) {
  collectDevicesFromDom();
  devices.splice(index, 1);
  deviceEditorDirty = true;
  renderDevices(lastState, true);
}

function markDevicesDirty() {
  deviceEditorDirty = true;
}

function collectDevicesFromDom() {
  devices.forEach((device, deviceIndex) => {
    const enabled = document.querySelector(
      `[data-device-index="${deviceIndex}"][data-device-field="enabled"]`
    );
    const name = document.querySelector(
      `[data-device-index="${deviceIndex}"][data-device-field="name"]`
    );
    const host = document.querySelector(
      `[data-device-index="${deviceIndex}"][data-device-field="host"]`
    );

    if (enabled) device.enabled = enabled.checked;
    if (name) device.name = name.value.trim();
    if (host) device.host = host.value.trim();

    delete device.ports;
  });
}

function renderDevices(state, force = false) {
  if (deviceEditorDirty && !force) return;

  const resultMap = new Map(
    (state?.devices || []).map((item) => [item.id, item])
  );

  $('deviceRows').innerHTML = devices.map((device, deviceIndex) => {
    const result = resultMap.get(device.id) || {};
    const online = result.online;
    const pingStatus =
      online === true ? 'VAR' :
      online === false ? 'YOK' :
      'WAITING';
    const pingColor =
      online === true ? '#16a34a' :
      online === false ? '#dc2626' :
      '#64748b';

    return `
      <tr data-device-index="${deviceIndex}">
        <td>
          <input type="checkbox"
                 data-device-index="${deviceIndex}"
                 data-device-field="enabled"
                 ${device.enabled ? 'checked' : ''}
                 onchange="window.markDevicesDirty()">
        </td>

        <td>
          <input data-device-index="${deviceIndex}"
                 data-device-field="name"
                 value="${esc(device.name)}"
                 oninput="window.markDevicesDirty()">
        </td>

        <td>
          <input data-device-index="${deviceIndex}"
                 data-device-field="host"
                 value="${esc(device.host)}"
                 oninput="window.markDevicesDirty()">
        </td>

        <td>
          <span class="dot" style="background:${pingColor}"></span>
          ${pingStatus}
        </td>

        <td>${fmt(result.latency_ms, ' ms')}</td>

        <td>
          ${result.age_s == null
            ? '-'
            : `${result.age_s.toFixed(1)} s ago`}
        </td>

        <td>
          <button class="danger compact"
                  onclick="window.removeDevice(${deviceIndex})">
            Delete Device
          </button>
        </td>
      </tr>
    `;
  }).join('');

  if (state?.network_scan_running) {
    $('networkMessage').textContent = 'Pinging IP addresses...';
  } else if (state?.network_last_scan_age_s != null) {
    $('networkMessage').textContent =
      `Last manual check ${state.network_last_scan_age_s.toFixed(1)} seconds ago.`;
  } else {
    $('networkMessage').textContent =
      'No manual check has been run yet; continuous ping is disabled.';
  }

  $('scanNetwork').disabled = Boolean(state?.network_scan_running);
}

async function saveDevices(showAlert = true) {
  collectDevicesFromDom();
  const body = await jsonFetch('/api/network/devices', {
    method: 'POST',
    body: JSON.stringify({devices}),
  });

  devices = body.devices;
  deviceEditorDirty = false;

  await refreshState();
  renderDevices(lastState, true);

  if (showAlert) {
    alert(`${devices.length} devices saved.`);
  }

  return body;
}

async function scanNetwork() {
  try {
    await saveDevices(false);
    await jsonFetch('/api/network/scan', {
      method: 'POST',
      body: '{}',
    });
  } catch (error) {
    alert(error.message);
  }

  await refreshState();
}

function addMasterRow(value = {}) {
  masters.push({enabled:value.enabled !== false, type:value.type || 'udp', label:value.label || 'LINK', host:value.host || '0.0.0.0', port:Number(value.port || 0), path:value.path || '', baud:Number(value.baud || 57600)});
  renderMasters();
}
function removeMaster(index) { masters.splice(index,1); renderMasters(); }
function collectMasters() {
  document.querySelectorAll('#masterRows tr').forEach((row,i) => {
    masters[i] = {
      enabled:row.querySelector('[data-field=enabled]').checked,
      type:row.querySelector('[data-field=type]').value,
      label:row.querySelector('[data-field=label]').value.trim(),
      host:row.querySelector('[data-field=host]').value.trim(),
      port:Number(row.querySelector('[data-field=port]').value || 0),
      path:row.querySelector('[data-field=path]').value.trim(),
      baud:Number(row.querySelector('[data-field=baud]').value || 57600),
    };
  });
}
function renderMasters() {
  $('masterRows').innerHTML = masters.map((m,i) => `<tr>
    <td><input type="checkbox" data-field="enabled" ${m.enabled ? 'checked' : ''}></td>
    <td><select data-field="type"><option value="udp" ${m.type==='udp'?'selected':''}>udp</option><option value="serial" ${m.type==='serial'?'selected':''}>serial</option></select></td>
    <td><input data-field="label" value="${esc(m.label)}"></td><td><input data-field="host" value="${esc(m.host)}"></td>
    <td><input data-field="port" type="number" min="0" max="65535" value="${m.port}"></td><td><input data-field="path" value="${esc(m.path)}" placeholder="/dev/serial/by-id/..."></td>
    <td><input data-field="baud" type="number" value="${m.baud}"></td><td><button class="danger compact" onclick="removeMaster(${i})">Delete</button></td>
  </tr>`).join('');
}
function addOutRow(value = {}) { outs.push({enabled:value.enabled !== false, host:value.host || '127.0.0.1', port:Number(value.port || 14550)}); renderOuts(); }
function removeOut(index) { outs.splice(index,1); renderOuts(); }
function collectOuts() {
  document.querySelectorAll('#outRows tr').forEach((row,i) => {
    outs[i] = {enabled:row.querySelector('[data-field=enabled]').checked, host:row.querySelector('[data-field=host]').value.trim(), port:Number(row.querySelector('[data-field=port]').value || 14550)};
  });
}
function renderOuts() {
  $('outRows').innerHTML = outs.map((o,i) => `<tr><td><input type="checkbox" data-field="enabled" ${o.enabled?'checked':''}></td><td><input data-field="host" value="${esc(o.host)}"></td><td><input data-field="port" type="number" min="1" max="65535" value="${o.port}"></td><td><button class="danger compact" onclick="removeOut(${i})">Delete</button></td></tr>`).join('');
}
async function generateMavproxy() {
  collectMasters(); collectOuts();
  const payload = {aircraft:$('builderAircraft').value, baudrate:Number($('builderBaud').value || 57600), extra_args:$('builderExtra').value, masters, outs, save_command:true};
  try {
    const body = await jsonFetch('/api/mavproxy/builder', {method:'POST', body:JSON.stringify(payload)});
    masters = body.builder.masters; outs = body.builder.outs; $('mavproxyCommand').value = body.command; renderMasters(); renderOuts(); alert('Command generated and saved. Restart MAVProxy to apply link descriptor changes.');
  } catch (error) { alert(error.message); }
}


function gcsForwardPayload() {
  return {
    enabled: $('gcsForwardEnabled').checked,
    host: $('gcsForwardHost').value.trim(),
    port: Number($('gcsForwardPort').value),
    period_s: Number($('gcsForwardPeriod').value),
  };
}

function renderGcsForward(result, updateFields = true) {
  const cfg = result.config || result;
  if (cfg && updateFields) {
    $('gcsForwardEnabled').checked = Boolean(cfg.enabled);
    $('gcsForwardHost').value = cfg.host || '';
    $('gcsForwardPort').value = cfg.port || 14660;
    $('gcsForwardPeriod').value = cfg.period_s || 1.0;
  }

  const status = result.status || {};
  const label = $('gcsForwardStatus');
  if (status.last_error) {
    label.textContent = `ERROR · ${status.target || '-'} · ${status.last_error}`;
    label.className = 'process-status stopped';
  } else if (status.last_sent_age_s !== null && status.last_sent_age_s !== undefined) {
    label.textContent = `${status.target || '-'} · ${Number(status.last_sent_age_s).toFixed(1)} s ago`;
    label.className = 'process-status running';
  } else if (cfg && cfg.enabled) {
    label.textContent = `${cfg.host}:${cfg.port} · ENABLED`;
    label.className = 'process-status running';
  } else {
    label.textContent = `${(cfg && cfg.host) || '-'}:${(cfg && cfg.port) || '-'} · DISABLED`;
    label.className = 'process-status stopped';
  }
}

async function refreshGcsForward() {
  try {
    const result = await jsonFetch('/api/gcs-forward');
    renderGcsForward(result, false);
  } catch (_) {}
}

async function saveGcsForward(showAlert = true) {
  const result = await jsonFetch('/api/gcs-forward', {
    method: 'POST',
    body: JSON.stringify(gcsForwardPayload()),
  });
  renderGcsForward(result);
  if (showAlert) alert(`GCS JSON target applied immediately: ${result.config.host}:${result.config.port}`);
  return result;
}

async function testGcsForward() {
  try {
    await saveGcsForward(false);
    const result = await jsonFetch('/api/gcs-forward/test', {method:'POST', body:'{}'});
    alert(`Test JSON sent: ${result.target} (${result.bytes} bayt)`);
    await refreshGcsForward();
  } catch (error) {
    alert(`Test could not be sent: ${error.message}`);
  }
}

async function saveProcess(name) {
  const prefix = name === 'mavproxy' ? 'mavproxy' : 'router';
  return jsonFetch(`/api/process/${name}/save`, {method:'POST', body:JSON.stringify({command:$(`${prefix}Command`).value, cwd:$(`${prefix}Cwd`).value})});
}
async function startProcess(name) {
  try { await saveProcess(name); await jsonFetch(`/api/process/${name}/start`, {method:'POST', body:'{}'}); }
  catch (error) { alert(error.message); }
  await refreshProcesses();
}
async function stopProcess(name) {
  try { await jsonFetch(`/api/process/${name}/stop`, {method:'POST', body:'{}'}); }
  catch (error) { alert(error.message); }
  await refreshProcesses();
}
async function sendMavproxyInput() {
  const text = $('mavproxyInput').value.trim(); if (!text) return;
  try { await jsonFetch('/api/process/mavproxy/input', {method:'POST', body:JSON.stringify({text})}); $('mavproxyInput').value=''; }
  catch (error) { alert(error.message); }
}
function renderProcess(prefix, process) {
  const status = $(`${prefix}Status`);
  status.textContent = process.running ? `RUNNING · PID ${process.pid} · ${process.uptime_s.toFixed(1)} sn` : `STOPPED · exit code ${process.exit_code ?? '-'}`;
  status.className = `process-status ${process.running ? 'running' : 'stopped'}`;
  const log = $(`${prefix}Log`); const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30; log.textContent = process.output || ''; if (atBottom) log.scrollTop = log.scrollHeight;
}
async function refreshProcesses() {
  try {
    const [mavproxy, router] = await Promise.all([jsonFetch('/api/process/mavproxy?tail=700'), jsonFetch('/api/process/mavlink_router?tail=700')]);
    renderProcess('mavproxy', mavproxy.process); renderProcess('router', router.process);
  } catch (_) {}
}

async function refreshState() {
  try {
    const state = await jsonFetch('/api/state'); lastState = state; renderLinks(state); renderDevices(state);
  } catch (error) { $('systemMessage').textContent = `Interface data could not be retrieved: ${error.message}`; $('systemMessage').style.color = '#fca5a5'; }
}
async function loadConfig() {
  config = await jsonFetch('/api/config');
  devices = structuredClone(config.network_checks.devices || []);
  masters = structuredClone(config.mavproxy_builder.masters || []);
  outs = structuredClone(config.mavproxy_builder.outs || []);
  $('mavproxyCommand').value = config.processes.mavproxy.command || '';
  $('mavproxyCwd').value = config.processes.mavproxy.cwd || '';
  $('routerCommand').value = config.processes.mavlink_router.command || '';
  $('routerCwd').value = config.processes.mavlink_router.cwd || '';
  $('builderAircraft').value = config.mavproxy_builder.aircraft || 'AIRCRAFT';
  $('builderBaud').value = config.mavproxy_builder.baudrate || 57600;
  $('builderExtra').value = config.mavproxy_builder.extra_args || '';
  renderGcsForward({config: config.gcs_forward_udp || {}});
  renderMasters(); renderOuts(); renderDevices(lastState, true);
}

function bindEvents() {
  $('saveGcsForward').onclick = () => saveGcsForward().catch(e => alert(e.message));
  $('testGcsForward').onclick = testGcsForward;
  $('manualTxMode').onclick = () => setTxMode('manual');
  $('autoTxMode').onclick = () => setTxMode('auto');
  $('scanNetwork').onclick = scanNetwork;
  $('addDevice').onclick = () => addDeviceRow();
  $('saveDevices').onclick = () => saveDevices(true).catch(e => alert(e.message));
  $('addMaster').onclick = () => addMasterRow({baud:Number($('builderBaud').value || 57600)}); $('addOut').onclick = () => addOutRow(); $('generateMavproxy').onclick = generateMavproxy;
  $('saveMavproxy').onclick = () => saveProcess('mavproxy').then(() => alert('MAVProxy command saved.')).catch(e => alert(e.message));
  $('startMavproxy').onclick = () => startProcess('mavproxy'); $('stopMavproxy').onclick = () => stopProcess('mavproxy'); $('sendMavproxyInput').onclick = sendMavproxyInput;
  $('mavproxyInput').addEventListener('keydown', e => {if (e.key === 'Enter') sendMavproxyInput();});
  $('saveRouter').onclick = () => saveProcess('mavlink_router').then(() => alert('mavlink-router command saved.')).catch(e => alert(e.message));
  $('startRouter').onclick = () => startProcess('mavlink_router'); $('stopRouter').onclick = () => stopProcess('mavlink_router');
}

window.linkAction = (encodedId, action) => performLinkControl(decodeURIComponent(encodedId), action);
window.removeDevice = removeDevice; window.removeMaster = removeMaster; window.removeOut = removeOut; window.markDevicesDirty = markDevicesDirty;
(async function init(){ setupTabs(); bindEvents(); await loadConfig(); await refreshState(); await refreshProcesses(); await refreshGcsForward(); setInterval(refreshState,1000); setInterval(refreshProcesses,1000); setInterval(refreshGcsForward,2000); })();
