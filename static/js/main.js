async function postJSON(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return r.json();
}
function flashResult(button, ok){
  if(!button) return;
  button.classList.remove('success','error');
  if(ok){
    button.classList.add('success');
    setTimeout(()=>button.classList.remove('success'), 900);
  }else{
    button.classList.add('error');
  }
}

// Master switches
const pixelEnabled = document.getElementById('pixelEnabled');
const capiEnabled = document.getElementById('capiEnabled');
if (window.DEFAULTS){
  if (pixelEnabled) pixelEnabled.checked = window.DEFAULTS.pixelEnabled;
  if (capiEnabled) capiEnabled.checked = window.DEFAULTS.capiEnabled;
}
document.getElementById('saveMaster')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const res = await postJSON('/api/master', {pixel_enabled: pixelEnabled.checked, capi_enabled: capiEnabled.checked});
  flashResult(btn, res.ok);
});

// Catalog size
document.getElementById('saveCatalogSize')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const size = parseInt(document.getElementById('catalogSize').value || '24', 10);
  const res = await postJSON('/api/catalog/size', {size});
  flashResult(btn, res.ok);
});

// Make control packer
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

// Manual send
document.getElementById('sendManual')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const channel = document.getElementById('manualChannel').value;
  const eventName = document.getElementById('manualEvent').value;
  const sku = document.getElementById('manualSKU').value || null;
  const controls = packControls('manual');
  const res = await postJSON('/api/manual/send', {channel, event: eventName, sku, controls});
  flashResult(btn, !!res.ok);
});

// Pixel Auto (browser) — simulated by repeatedly calling manual pixel only
let pixelTimer = null;
document.getElementById('startPixel')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const ms = Math.max(200, +document.getElementById('pixelInterval').value || 2000);
  if (pixelTimer) clearInterval(pixelTimer);
  pixelTimer = setInterval(async ()=>{
    const controls = packControls('pixel');
    const res = await postJSON('/api/manual/send', {channel: 'pixel', event:'Purchase', controls});
    // no button flash during loop
  }, ms);
  flashResult(btn, true);
});
document.getElementById('stopPixel')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  if (pixelTimer) clearInterval(pixelTimer);
  pixelTimer = null;
  flashResult(btn, true);
});

// CAPI Auto (server)
document.getElementById('startServer')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const body = {
    interval_ms: Math.max(200, +document.getElementById('serverInterval').value || 2000),
    ...packControls('server')
  };
  const res = await postJSON('/api/server_auto/start', body);
  flashResult(btn, !!res.ok);
});
document.getElementById('stopServer')?.addEventListener('click', async (e)=>{
  const btn = e.currentTarget;
  const res = await postJSON('/api/server_auto/stop', {});
  flashResult(btn, !!res.ok);
});
