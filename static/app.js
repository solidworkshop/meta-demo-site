(function(){
  function el(sel, root){ return (root||document).querySelector(sel); }
  function els(sel, root){ return Array.from((root||document).querySelectorAll(sel)); }

  function flashButton(btn, ok){
    btn.classList.remove('ok','err');
    void btn.offsetWidth;
    if(ok){ btn.classList.add('ok'); setTimeout(()=>btn.classList.remove('ok'), 400); }
    else{ btn.classList.add('err'); }
  }

  async function sendOnce(scopeEl, channel){
    const get = c => el(c, scopeEl);
    const priceStr = get('.js-price')?.value?.trim();
    const price = priceStr ? parseFloat(priceStr) : null;

    const body = {
      channel,
      event_name: get('.js-event')?.value || 'Purchase',
      sku: get('.js-sku')?.value || null,
      price,
      currency: get('.js-currency')?.value || 'AUTO',
      allow_null_price: !!get('.js-null-price')?.checked,
      allow_null_currency: !!get('.js-null-currency')?.checked,
      allow_null_event_id: !!get('.js-null-eid')?.checked,
      pltv: (get('.js-pltv') && get('.js-pltv').value) ? parseFloat(get('.js-pltv').value) : null,
      delay_ms: parseInt(get('.js-delay')?.value || '0', 10),
      margin_cost_min_pct: parseFloat(get('.js-cost-min')?.value || '20'),
      margin_cost_max_pct: parseFloat(get('.js-cost-max')?.value || '70'),
    };

    const btn = el(`.btn[data-channel="${channel}"]`, scopeEl) || el('.btn', scopeEl);
    try{
      const res = await fetch('/api/send', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body)
      });
      const data = await res.json();
      flashButton(btn, data.ok && (!data.capi || data.capi.status !== 'http_error'));
      if(!data.ok || (data.capi && data.capi.status !== 'ok' && data.capi.status !== 'dry-run')){
        console.error('CAPI result:', data.capi);
      }
      return data.ok;
    }catch(e){
      console.error(e);
      flashButton(btn, false);
      return false;
    }
  }

  els('.controls[data-scope="manual"] .js-send').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      const scope = btn.closest('.controls');
      const ch = btn.getAttribute('data-channel');
      sendOnce(scope, ch);
    });
  });

  const timers = { pixel: null, capi: null };
  els('.js-toggle-auto').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      const scope = btn.closest('.controls');
      const channel = btn.getAttribute('data-channel');
      if(timers[channel]){
        clearInterval(timers[channel]); timers[channel] = null;
        btn.textContent = 'Start Auto';
      }else{
        const iv = parseInt(el('.js-interval', scope)?.value || '2000', 10);
        if(!iv || iv < 200){ btn.textContent = 'Start Auto'; return; }
        btn.textContent = 'Stop Auto';
        timers[channel] = setInterval(()=> sendOnce(scope, channel), iv);
      }
    });
  });
})();