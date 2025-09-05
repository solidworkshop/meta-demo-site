async function postJSON(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return r.json();
}
async function getJSON(url){
  const r = await fetch(url);
  return r.json();
}
function setActive(cardEl, active){
  if(!cardEl) return;
  cardEl.classList.toggle('active', !!active);
}
function setDot(dotEl, active){
  if(!dotEl) return;
  dotEl.classList.toggle('active', !!active);
}
function setBadge(badgeEl, on){
  if (!badgeEl) return;
  badgeEl.textContent = on ? 'ON' : 'OFF';
  badgeEl.classList.toggle('on', !!on);
}
function packControls(prefix){
  return {
    currency: document.getElementById(prefix+'Currency')?.value || 'Auto',
    cost_pct_min: +document.getElementById(prefix+'CostMin')?.value || 20,
    cost_pct_max: +document.getElementById(prefix+'CostMax')?.value || 60,
    pltv: +document.getElementById(prefix+'PLTV')?.value || 0,
    delay_ms: +document.getElementById(prefix+'Delay')?.value || 0,
    match_rate_degrade_pct: +document.getElementById(prefix+'Degrade')?.value || 0,
    bad_nulls: {
      price: !!document.getElementById(prefix+'NullPrice')?.checked,
      currency: !!document.getElementById(prefix+'NullCurrency')?.checked,
      event_id: !!document.getElementById(prefix+'NullEventId')?.checked,
    }
  };
}

// Banner test button
document.getElementById('btnTestCapi')?.addEventListener('click', async ()=>{
  const res = await postJSON('/api/capi/test', {});
  const box = document.getElementById('capiErrorBox');
  if (box) box.textContent = JSON.stringify(res, null, 2);
});

// Manual sender
document.getElementById('sendManual')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget; btn.disabled = true;
  const channel = document.getElementById('manualChannel').value;
  const eventName = document.getElementById('manualEvent').value;
  const sku = document.getElementById('manualSKU').value || null;
  const controls = {
    currency: document.getElementById('manualCurrency')?.value || 'Auto',
    cost_pct_min: +document.getElementById('manualCostMin')?.value || 20,
    cost_pct_max: +document.getElementById('manualCostMax')?.value || 60,
    pltv: +document.getElementById('manualPLTV')?.value || 0,
    delay_ms: +document.getElementById('manualDelay')?.value || 0,
    match_rate_degrade_pct: +document.getElementById('manualDegrade')?.value || 0,
    bad_nulls: {
      price: !!document.getElementById('manualNullPrice')?.checked,
      currency: !!document.getElementById('manualNullCurrency')?.checked,
      event_id: !!document.getElementById('manualNullEventId')?.checked,
    }
  };
  const res = await postJSON('/api/manual/send', {channel, event: eventName, sku, controls});
  btn.disabled = false;
});

// Pixel (browser)
const cardPixel = document.getElementById('cardPixel');
const dotPixel = document.getElementById('dotPixel');
const badgePixel = document.getElementById('badgePixel');
const statusPixel = document.getElementById('statusPixel');
const countPixel = document.getElementById('countPixel');
const togglePixel = document.getElementById('togglePixel');
document.getElementById('resetPixel')?.addEventListener('click', async ()=>{
  await postJSON('/api/pixel_auto/reset_count', {});
  countPixel.textContent = '0';
});
let pixelTimer = null;

async function updatePixelUI(running){
  setActive(cardPixel, running);
  setDot(dotPixel, running);
  setBadge(badgePixel, running);
  statusPixel.textContent = running ? 'Running…' : 'Stopped';
}
async function startPixelLoop(){
  const ms = Math.max(200, +document.getElementById('pixelInterval').value || 2000);
  if (pixelTimer) clearInterval(pixelTimer);
  await postJSON('/api/pixel_auto/reset_count', {});
  countPixel.textContent = '0';
  pixelTimer = setInterval(async ()=>{
    const controls = packControls('pixel');
    await postJSON('/api/manual/send', {channel: 'pixel', event:'Purchase', controls});
    await postJSON('/api/pixel_auto/increment', {});
    countPixel.textContent = String((parseInt(countPixel.textContent,10)||0) + 1);
  }, ms);
  await postJSON('/api/pixel_auto/set', {running: true, interval_ms: ms, ...packControls('pixel')});
  togglePixel.checked = true;
  updatePixelUI(true);
}
async function stopPixelLoop(){
  if (pixelTimer) clearInterval(pixelTimer);
  pixelTimer = null;
  await postJSON('/api/pixel_auto/set', {running: false});
  togglePixel.checked = false;
  updatePixelUI(false);
}
togglePixel?.addEventListener('change', async ()=>{
  if (togglePixel.checked) await startPixelLoop();
  else await stopPixelLoop();
});

// CAPI (server)
const cardServer = document.getElementById('cardServer');
const dotServer = document.getElementById('dotServer');
const badgeServer = document.getElementById('badgeServer');
const statusServer = document.getElementById('statusServer');
const countServer = document.getElementById('countServer');
const toggleServer = document.getElementById('toggleServer');
document.getElementById('btnTestCapi2')?.addEventListener('click', async ()=>{
  const res = await postJSON('/api/capi/test', {});
  const box = document.getElementById('capiErrorBox');
  if (box) box.textContent = JSON.stringify(res, null, 2);
});
document.getElementById('resetServer')?.addEventListener('click', async ()=>{
  await postJSON('/api/server_auto/reset_count', {});
  countServer.textContent = '0';
});

async function updateServerUI(running){
  setActive(cardServer, running);
  setDot(dotServer, running);
  setBadge(badgeServer, running);
  statusServer.textContent = running ? 'Running…' : 'Stopped';
}
toggleServer?.addEventListener('change', async ()=>{
  if (toggleServer.checked){
    const body = { interval_ms: Math.max(200, +document.getElementById('serverInterval').value || 2000), ...packControls('server') };
    await postJSON('/api/server_auto/reset_count', {});
    countServer.textContent = '0';
    const res = await postJSON('/api/server_auto/start', body);
    if (res.ok){ updateServerUI(true); } else { toggleServer.checked = false; }
  }else{
    const res = await postJSON('/api/server_auto/stop', {});
    if (res.ok){ updateServerUI(false); } else { toggleServer.checked = true; }
  }
});

// Restore from server
(async function init(){
  try{
    const status = await getJSON('/api/status');
    if (status.ok){
      // Pixel
      const pa = status.pixel_auto || {};
      countPixel.textContent = String(pa.count || 0);
      togglePixel.checked = !!pa.running;
      if (pa.running){
        document.getElementById('pixelInterval').value = pa.interval_ms || 2000;
        await startPixelLoop();
        // keep count after reset by setting it back
        countPixel.textContent = String(pa.count || 0);
      }else{
        await stopPixelLoop();
      }
      // Server
      const sa = status.server_auto || {};
      countServer.textContent = String(sa.count || 0);
      toggleServer.checked = !!sa.running;
      updateServerUI(!!sa.running);
      // Last capi error (if any)
      const box = document.getElementById('capiErrorBox');
      if (status.last_capi_error && box){
        box.textContent = JSON.stringify(status.last_capi_error, null, 2);
      }
    }
  }catch(e){
    // ignore
  }
})();