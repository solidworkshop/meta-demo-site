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
  if(active) cardEl.classList.add('active');
  else cardEl.classList.remove('active');
}
function setDot(dotEl, active){
  if(!dotEl) return;
  if(active) dotEl.classList.add('active');
  else dotEl.classList.remove('active');
}

// Master switches
const pixelEnabled = document.getElementById('pixelEnabled');
const capiEnabled = document.getElementById('capiEnabled');
document.getElementById('saveMaster')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const res = await postJSON('/api/master', {pixel_enabled: pixelEnabled.checked, capi_enabled: capiEnabled.checked});
  btn.classList.toggle('success', !!res.ok);
  setTimeout(()=>btn.classList.remove('success'), 900);
});

// Catalog size
document.getElementById('saveCatalogSize')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const size = parseInt(document.getElementById('catalogSize').value || '24', 10);
  const res = await postJSON('/api/catalog/size', {size});
  btn.classList.toggle('success', !!res.ok);
  setTimeout(()=>btn.classList.remove('success'), 900);
});

// Manual send
document.getElementById('sendManual')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
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
  btn.classList.toggle('success', !!res.ok);
  setTimeout(()=>btn.classList.remove('success'), 900);
});

// Pixel Auto (browser) with toolbar UI
const cardPixel = document.getElementById('cardPixel');
const dotPixel = document.getElementById('dotPixel');
const statusPixel = document.getElementById('statusPixel');
const countPixel = document.getElementById('countPixel');
const togglePixel = document.getElementById('togglePixel');
let pixelTimer = null;

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

async function updatePixelUI(running){
  setActive(cardPixel, running);
  setDot(dotPixel, running);
  statusPixel.textContent = running ? 'Running…' : 'Stopped';
}

async function startPixelLoop(){
  const ms = Math.max(200, +document.getElementById('pixelInterval').value || 2000);
  if (pixelTimer) clearInterval(pixelTimer);
  pixelTimer = setInterval(async ()=>{
    const controls = packControls('pixel');
    await postJSON('/api/manual/send', {channel: 'pixel', event:'Purchase', controls});
    await postJSON('/api/pixel_auto/increment', {});
    const c = (parseInt(countPixel.textContent,10) || 0) + 1;
    countPixel.textContent = String(c);
  }, ms);
  await postJSON('/api/pixel_auto/set', {running: true, interval_ms: ms, ...packControls('pixel')});
  await postJSON('/api/pixel_auto/reset_count', {});
  countPixel.textContent = '0';
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

// CAPI Auto (server)
const cardServer = document.getElementById('cardServer');
const dotServer = document.getElementById('dotServer');
const statusServer = document.getElementById('statusServer');
const countServer = document.getElementById('countServer');
const toggleServer = document.getElementById('toggleServer');

async function updateServerUI(running){
  setActive(cardServer, running);
  setDot(dotServer, running);
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

// Restore from server on load
(async function init(){
  try{
    const status = await getJSON('/api/status');
    if (status.ok){
      // Pixel
      const pa = status.pixel_auto || {};
      togglePixel.checked = !!pa.running;
      countPixel.textContent = String(pa.count || 0);
      if (pa.running){
        // recreate loop with the saved interval and controls
        document.getElementById('pixelInterval').value = pa.interval_ms || 2000;
        // set UI
        updatePixelUI(true);
        // start loop fresh to ensure client-side timer exists
        await startPixelLoop();
        // carry over count after reset
        countPixel.textContent = String(pa.count || 0);
      }else{
        updatePixelUI(false);
      }
      // Server
      const sa = status.server_auto || {};
      toggleServer.checked = !!sa.running;
      countServer.textContent = String(sa.count || 0);
      updateServerUI(!!sa.running);
    }
  }catch(e){
    togglePixel && (togglePixel.checked = false);
    toggleServer && (toggleServer.checked = false);
    updatePixelUI(false);
    updateServerUI(false);
  }
})();