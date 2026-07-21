/* ============================================================
   ⬇⬇⬇  THE ONLY LINE YOU EVER CHANGE  ⬇⬇⬇
   Put your Render backend URL between the quotes.
   No slash at the end. Then save + redeploy. Done.
   ============================================================ */
const API_BASE = "https://video-downloader-backend-79wc.onrender.com";
/* ============================================================
   ⬆⬆⬆  THAT'S IT. DON'T TOUCH ANYTHING BELOW.  ⬆⬆⬆
   ============================================================ */

/* ============================================================
   💰 MONEY SLOTS — the ONLY block you touch to start earning.
   Everything is styled to look like part of the site (native).
   Leave a field "" and that slot stays INVISIBLE — so this is
   safe to ship right now; nothing shows until you fill it in.
   ============================================================ */
const ADS = {
  // (1) NATIVE AFFILIATE card — shows inside the result card after a
  // successful fetch (highest-intent moment). Pay-per-signup, free to join.
  // Fill title + cta + href when you have an affiliate link.
  affiliate: {
    img:   "",              // optional brand logo/image URL (premium look). Falls back to icon.
    icon:  "shield-check",  // any lucide icon name (used only if img is empty)
    tag:   "Editor's pick",  // small kicker label, e.g. "Editor's pick" / "Recommended"
    title: "",              // e.g. "Download on any network, unblocked"
    body:  "",              // one calm line, no hype
    cta:   "",              // button text, e.g. "Get NordVPN — 70% off"
    href:  ""               // ← your affiliate URL
  },
  // (1b) HOUSE PROMOS — YOUR OWN slides, 100% brand-safe (no third party).
  // These animate in the same premium rotator as the affiliate card above,
  // so the slot is NEVER empty and NEVER shows dating/adult junk. Edit freely.
  // action:'install' triggers the PWA install prompt; otherwise href navigates.
  house: [
    { icon:"download-cloud", tag:"Get the app", title:"Install GOOGLY RANKS",
      body:"One tap from your home screen. Works offline-ready, no store needed.",
      cta:"Add to device", action:"install" },
    { icon:"globe", tag:"1000+ sources", title:"One paste. Any platform.",
      body:"YouTube, TikTok, Instagram, X, Reddit, Twitch and a thousand more.",
      cta:"Try another link", action:"focus" },
    { icon:"audio-lines", tag:"Pro tip", title:"Rip clean audio too",
      body:"Pull an MP3 off any video — pick the audio grade on the board.",
      cta:"How it works", href:"/youtube-to-mp3/" }
  ],
  // (2) NATIVE DISPLAY unit — Adsterra Native Banner. Needs BOTH the
  // invoke.js URL and its matching container id (both from the GET CODE
  // snippet). Empty = nothing loads, zero network calls.
  nativeDisplay: {
    scriptSrc:   "https://pl30468258.effectivecpmnetwork.com/ab9c3d2a7064ac3c2d80b47f43df954d/invoke.js",
    containerId: "container-ab9c3d2a7064ac3c2d80b47f43df954d"
  }
};
/* ============================================================
   ⬆  Fill those in when accounts exist. Don't touch below.
   ============================================================ */

const $ = s => document.querySelector(s);
const state = { data:null, selected:null, filter:'all' };

/* Inject the native money slots. Idempotent + self-hiding: a slot with no
   content renders nothing, so shipping this empty changes the site by 0px. */
let deferredInstall = null; // captured PWA install event, if the browser offers one
window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); deferredInstall = e; });

function slideMarkup(s, sponsored){
  const media = s.img
    ? `<span class="sp-ic sp-img"><img src="${s.img}" alt="" loading="lazy"></span>`
    : `<span class="sp-ic"><i data-lucide="${s.icon||'sparkles'}" width="22" height="22"></i></span>`;
  return `${media}
    <span class="sp-txt">
      <span class="sp-tag">${s.tag || (sponsored?'Partner':'GOOGLY RANKS')}</span>
      <b>${s.title||''}</b>
      <span class="sp-body">${s.body||''}</span>
    </span>
    <span class="sp-cta">${s.cta||'Learn more'}<i data-lucide="arrow-right" width="15" height="15"></i></span>`;
}

function renderAds(){
  const res = $('#results');
  if(res && !$('#promo')){
    // Build the slide deck: real affiliate offer first (if set), then house promos.
    const slides = [];
    const a = ADS.affiliate;
    if(a && a.title && a.href) slides.push({...a, _sponsored:true});
    (ADS.house||[]).forEach(h => { if(h && h.title) slides.push(h); });

    if(slides.length){
      const rot = document.createElement('div');
      rot.id = 'promo'; rot.className = 'promo';
      rot.setAttribute('aria-label','Recommended');

      const track = document.createElement('div');
      track.className = 'promo-track';
      slides.forEach((s,i)=>{
        const el = document.createElement(s.href ? 'a' : 'button');
        el.className = 'sponsor promo-slide' + (i===0?' is-active':'');
        el.setAttribute('type', s.href ? '' : 'button');
        if(s.href){ el.href = s.href; if(s._sponsored){ el.target='_blank'; el.rel='sponsored noopener'; } }
        el.dataset.action = s.action || '';
        el.innerHTML = slideMarkup(s, s._sponsored);
        track.appendChild(el);
      });
      rot.appendChild(track);

      // dots + progress line only when there's more than one slide
      if(slides.length > 1){
        const nav = document.createElement('div'); nav.className='promo-nav';
        slides.forEach((_,i)=>{
          const d=document.createElement('button');
          d.className='promo-dot'+(i===0?' is-active':''); d.type='button';
          d.setAttribute('aria-label',`Slide ${i+1}`);
          d.onclick = ()=>goSlide(i, true);
          nav.appendChild(d);
        });
        rot.appendChild(nav);
        const line=document.createElement('div'); line.className='promo-line'; line.innerHTML='<i></i>';
        rot.appendChild(line);
      }

      res.appendChild(rot);
      if(window.lucide) lucide.createIcons();
      initPromo(slides.length);
    }
  }
  const nd = ADS.nativeDisplay;
  if(nd && nd.scriptSrc && !document.getElementById('native-slot')){
    const slot = document.createElement('div');
    slot.id = 'native-slot'; slot.className = 'native-slot';
    // Native-ad convention: a "Recommended for you" kicker makes the unit read
    // as part of the page (fonts/colors come from .native-slot CSS below).
    const head = document.createElement('div');
    head.className = 'native-head';
    head.innerHTML = '<span class="native-kicker">Recommended for you</span>'
                   + '<span class="native-tag">Sponsored</span>';
    slot.appendChild(head);
    // Adsterra fills a specific container id; invoke.js looks for it, so the
    // div must exist BEFORE the script runs — build the div first, then append.
    if(nd.containerId){
      const box = document.createElement('div');
      box.id = nd.containerId;
      slot.appendChild(box);
    }
    const foot = document.querySelector('.site-foot');
    if(foot) foot.parentElement.insertBefore(slot, foot);
    const s = document.createElement('script');
    s.src = nd.scriptSrc; s.async = true; s.setAttribute('data-cfasync','false');
    slot.appendChild(s);
  }
}

/* ── Promo rotator engine ─────────────────────────────────────────────
   Crossfades between the affiliate + house slides. Auto-advances every
   ROTATE ms, pauses on hover/focus, restarts the progress line each step,
   and fully disables motion when the user prefers reduced motion (shows
   slide 1 statically). CTA actions: install → PWA prompt, focus → URL box. */
const PROMO = { i:0, n:0, timer:null, ROTATE:6000,
  reduce: window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches };

function paintSlide(){
  const slides = document.querySelectorAll('#promo .promo-slide');
  const dots   = document.querySelectorAll('#promo .promo-dot');
  slides.forEach((el,k)=>el.classList.toggle('is-active', k===PROMO.i));
  dots.forEach((el,k)=>el.classList.toggle('is-active', k===PROMO.i));
  const bar = document.querySelector('#promo .promo-line i');
  if(bar && !PROMO.reduce){ // restart the fill animation
    bar.style.animation='none'; void bar.offsetWidth;
    bar.style.animation=`promoFill ${PROMO.ROTATE}ms linear`;
  }
}
function goSlide(idx, manual){
  if(!PROMO.n) return;
  PROMO.i = (idx + PROMO.n) % PROMO.n;
  paintSlide();
  if(manual) restartPromo();
}
function nextSlide(){ goSlide(PROMO.i + 1); }
function startPromo(){ if(!PROMO.reduce && PROMO.n>1){ clearInterval(PROMO.timer); PROMO.timer=setInterval(nextSlide, PROMO.ROTATE); } }
function stopPromo(){ clearInterval(PROMO.timer); PROMO.timer=null; }
function restartPromo(){ stopPromo(); startPromo(); }

function initPromo(count){
  PROMO.i=0; PROMO.n=count;
  const rot=$('#promo'); if(!rot) return;
  paintSlide();
  // CTA actions on slides that aren't plain links
  rot.querySelectorAll('.promo-slide').forEach(el=>{
    const act = el.dataset.action;
    if(act==='install' || act==='focus'){
      el.addEventListener('click', async (e)=>{
        e.preventDefault();
        if(act==='install'){
          if(deferredInstall){ deferredInstall.prompt(); try{ await deferredInstall.userChoice; }catch(_){}; deferredInstall=null; }
          else { window.scrollTo({top:0,behavior:'smooth'}); }
        } else { const u=$('#url'); if(u){ u.focus(); u.scrollIntoView({block:'center',behavior:'smooth'}); } }
      });
    }
  });
  // pause while the user is looking at / interacting with it
  rot.addEventListener('mouseenter', stopPromo);
  rot.addEventListener('mouseleave', startPromo);
  rot.addEventListener('focusin', stopPromo);
  rot.addEventListener('focusout', startPromo);
  startPromo();
}

/* clock */
function tick(){ $('#clock').textContent = new Date().toLocaleTimeString('en-US',{hour12:false}); }
setInterval(tick,1000); tick();

/* live backend status light */
function setStatus(stateName, text){ $('#status').dataset.state = stateName; $('#statusText').textContent = text; }
async function checkBackend(){
  if(!API_BASE || API_BASE.includes('your-app')){ setStatus('off','OFF AIR'); return; }
  setStatus('checking','CHECKING');
  try{
    const ctrl = new AbortController();
    // cold starts on the free tier can take up to ~60s to answer
    const t = setTimeout(()=>ctrl.abort(), 60000);
    const r = await fetch(`${API_BASE}/`, {signal:ctrl.signal});
    clearTimeout(t);
    setStatus(r.ok ? 'live' : 'off', r.ok ? '● REC · LIVE' : 'OFF AIR');
  }catch(e){ setStatus('off','OFF AIR'); }
}

/* helpers */
function fmtDur(sec){
  sec = Math.round(sec||0);
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60), s=sec%60;
  const pad=n=>String(n).padStart(2,'0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
function fmtSize(b){
  if(!b) return '—';
  const u=['B','KB','MB','GB']; let i=0;
  while(b>=1024&&i<u.length-1){b/=1024;i++;}
  return `${b.toFixed(b<10&&i>0?1:0)} ${u[i]}`;
}
function cleanRes(label){
  const m = /(\d+)p/.exec(label||'');
  if(!m) return {res:(label||'').replace(/\s*\(.*\)/,''), ext:extOf(label)};
  let h = parseInt(m[1],10);
  const map={1920:1080,1280:720,854:480,640:360,426:240,256:144};
  if(map[h]) h=map[h];
  return {res:h+'p', ext:extOf(label)};
}
function extOf(label){ const m=/\(([^)]+)\)/.exec(label||''); return m?m[1]:''; }

function render(){
  const d = state.data; if(!d) return;
  $('#thumb').src = d.thumbnail || '';
  $('#title').textContent = d.title || 'Untitled feed';
  $('#dur').textContent = fmtDur(d.duration);
  $('#s-run').textContent = fmtDur(d.duration);

  // Live broadcasts can't be captured in real time on this host — show the
  // note from the backend so it's clear the recording is needed instead.
  let ln = $('#liveNote');
  if(d.is_live && d.live_note){
    if(!ln){
      ln = document.createElement('div');
      ln.id = 'liveNote';
      ln.style.cssText = 'margin:10px 0;padding:10px 12px;border-radius:8px;'
        + 'background:rgba(255,180,60,.12);border:1px solid rgba(255,180,60,.4);'
        + 'color:#ffcf80;font-size:13px;line-height:1.4';
      $('#title').parentElement.appendChild(ln);
    }
    ln.textContent = '⚠ ' + d.live_note;
  } else if(ln){ ln.remove(); }

  const vids = d.formats.filter(f=>f.type==='video');
  const best = vids.length ? cleanRes(vids[0].label).res : '—';
  $('#s-tracks').textContent = d.formats.length;
  $('#s-best').textContent = best;

  const list = d.formats.filter(f => state.filter==='all' || f.type===state.filter);
  $('#count').textContent = `${list.length} signal${list.length!==1?'s':''}`;

  const board = $('#board'); board.innerHTML='';
  list.forEach((f,i)=>{
    const c = cleanRes(f.label);
    const isAudio = f.type==='audio';
    const btn = document.createElement('button');
    btn.className='flap'; btn.style.setProperty('--i',i);
    btn.setAttribute('aria-pressed', state.selected===f.format_id);
    btn.innerHTML = `
      <span class="res">${isAudio ? '<i data-lucide=\"audio-lines\" width=\"18\" height=\"18\"></i>' : c.res}</span>
      <span class="kind">
        <span class="k">${isAudio ? (f.label.replace(/\s*\(.*\)/,'')||'Audio') : c.res+' video'}</span>
        <span class="sub">${(c.ext||f.ext||'').toUpperCase()} · ${f.format_id}</span>
      </span>
      <span class="tag ${isAudio?'audio':'video'}">${isAudio?'Audio':'Video'}</span>
      <span class="size">${fmtSize(f.filesize)}</span>
      <span class="check"><i data-lucide="check" width="13" height="13"></i></span>`;
    btn.onclick = ()=>select(f);
    board.appendChild(btn);
  });
  if(window.lucide) lucide.createIcons();
}

function select(f){
  state.selected = f.format_id;
  const c = cleanRes(f.label);
  $('#ro-name').textContent = `${f.type==='audio'?'Audio':c.res} · ${(c.ext||f.ext||'').toUpperCase()}`;
  $('#air').disabled = false;
  $('#dock').classList.add('armed');
  $('#tally-label').textContent = 'Armed';
  render();
}

document.querySelectorAll('.seg button').forEach(b=>{
  b.onclick = ()=>{
    document.querySelectorAll('.seg button').forEach(x=>x.setAttribute('aria-pressed', x===b));
    state.filter = b.dataset.filter; render();
  };
});

function show(view){
  ['empty','error','loading','results'].forEach(v=>$('#'+v).hidden = v!==view);
  $('#dock').hidden = view!=='results';
  if(view==='results') renderAds();
}
async function pull(){
  const url = $('#url').value.trim();
  if(!url){ $('#url').focus(); return; }
  state.selected=null; $('#air').disabled=true; $('#dock').classList.remove('armed','live');
  $('#fetch').disabled=true; show('loading');
  try{
    const r = await fetch(`${API_BASE}/formats`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})
    });
    const j = await r.json();
    if(!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    if(!j.formats || !j.formats.length) throw new Error('No downloadable formats found on this source.');
    state.data = j; state.filter='all';
    document.querySelectorAll('.seg button').forEach(x=>x.setAttribute('aria-pressed', x.dataset.filter==='all'));
    show('results'); render();
    checkBackend();
  }catch(e){
    $('#errtext').innerHTML = `We couldn't read that feed. <code>${(e.message||'').replace(/\u001b\[[0-9;]*m/g,'')}</code>`;
    show('error');
  }finally{
    $('#fetch').disabled=false;
  }
}

/* Save a fetched Response to disk, streaming a progress bar as bytes arrive.
   Shared by the direct-CDN path and the server path so progress UX is identical. */
async function saveResponse(r, fallbackName){
  const bar=$('#bar'), pct=$('#ro-pct');
  const total = +r.headers.get('content-length') || 0;
  const reader = r.body.getReader(); const chunks=[]; let got=0;
  while(true){
    const {done,value} = await reader.read();
    if(done) break;
    chunks.push(value); got += value.length;
    if(total){ const p=Math.min(100,Math.round(got/total*100)); bar.style.width=p+'%'; pct.textContent=p+'%'; }
    else { pct.textContent=fmtSize(got); bar.style.width='60%'; }
  }
  bar.style.width='100%'; pct.textContent='100%';
  let name = fallbackName;
  const cd = r.headers.get('content-disposition');
  const m = cd && /filename[^;=\n]*=(?:UTF-8''|")?([^";\n]+)/i.exec(cd);
  if(m){ try{ name = decodeURIComponent(m[1]); }catch(e){ name = m[1]; } }
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob(chunks)); a.download=name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),4000);
}

/* Server path: backend downloads + merges, then streams the file back. The
   reliable fallback and the only route for HD merges / MP3 transcodes. */
async function serverDownload(sel, fallbackName){
  const body = JSON.stringify({
    url:$('#url').value.trim(),
    format_id:state.selected,
    audio_only: !!(sel && sel.type==='audio'),
    height: (sel && sel.type==='video' && sel.height) ? sel.height : null
  });
  // The backend caps concurrent downloads and returns 503 when the box is
  // momentarily saturated. Retry a few times with backoff so a busy spike
  // reads as "queued", not a failure.
  let r, tries=0;
  while(true){
    r = await fetch(`${API_BASE}/download`,{
      method:'POST', headers:{'Content-Type':'application/json'}, body
    });
    if(r.status!==503 || tries>=4) break;
    tries++;
    $('#tally-label').textContent = 'Queued…';
    await new Promise(res=>setTimeout(res, 1500*tries));
  }
  if(!r.ok){ const j=await r.json().catch(()=>({})); throw new Error(j.detail||`HTTP ${r.status}`); }
  await saveResponse(r, fallbackName);
}

async function goLive(){
  if(!state.selected || !state.data) return;
  const dock=$('#dock'), air=$('#air');
  dock.classList.remove('armed'); dock.classList.add('live');
  $('#tally-label').textContent='On Air'; air.disabled=true;
  $('#bar').style.width='0%'; $('#ro-pct').textContent='0%';
  const sel = state.data.formats.find(x=>x.format_id===state.selected);
  let name = (state.data.title||'video').replace(/[\\/:*?"<>|]/g,'').slice(0,80)
             + '.' + ((sel&&sel.type==='audio')?(sel.ext||'m4a'):((sel&&sel.ext)||'mp4'));
  try{
    // FAST PATH: a single pre-merged HTTP stream is pulled straight from the CDN
    // in the browser — no server hop, no double transfer. CDNs sometimes refuse
    // cross-origin fetches; on ANY failure we fall back to the server path, so
    // this is a pure speedup with no new failure mode.
    if(sel && sel.direct_url){
      try{
        const dr = await fetch(sel.direct_url);
        if(dr.ok && dr.body){ await saveResponse(dr, name); $('#tally-label').textContent='Grabbed ✓'; return; }
        throw new Error('direct fetch not ok');
      }catch(_){ $('#tally-label').textContent='Routing…'; /* fall through to server */ }
    }
    await serverDownload(sel, name);
    $('#tally-label').textContent='Grabbed ✓';
  }catch(e){
    $('#tally-label').textContent='Failed';
    $('#errtext').innerHTML = `Download stalled. <code>${(e.message||'').replace(/\u001b\[[0-9;]*m/g,'')}</code>`;
    show('error');
  }finally{
    setTimeout(()=>{ dock.classList.remove('live'); dock.classList.add('armed'); air.disabled=false; $('#tally-label').textContent='Armed'; },1200);
  }
}

/* wire */
$('#fetch').onclick=pull;
$('#url').addEventListener('keydown',e=>{ if(e.key==='Enter') pull(); });
$('#air').onclick=goLive;
$('#status').onclick=checkBackend;

/* pre-warm: the free-tier backend sleeps when idle and takes ~30-60s to wake.
   Ping it the moment the user engages the URL box (focus or first input) so
   it's already awake by the time they hit Pull Feed — hides most cold-start. */
let warmed=false;
function warm(){
  if(warmed) return; warmed=true;
  fetch(`${API_BASE}/`).then(()=>checkBackend()).catch(()=>{});
  setTimeout(()=>{ warmed=false; }, 60000); // allow re-warm after a minute idle
}
$('#url').addEventListener('focus', warm);
$('#url').addEventListener('input', warm);

/* boot */
checkBackend();
setInterval(checkBackend, 30000);
renderAds();
if(window.lucide) lucide.createIcons();
/* PWA: register the shell service worker so repeat visits are instant and the
   app is installable. It never caches media or API calls (see sw.js). */
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});
