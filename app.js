/* ============================================================
   ⬇⬇⬇  THE ONLY LINE YOU EVER CHANGE  ⬇⬇⬇
   Put your Render backend URL between the quotes.
   No slash at the end. Then save + redeploy. Done.
   ============================================================ */
const API_BASE = "https://video-downloader-backend-79wc.onrender.com";
/* ============================================================
   ⬆⬆⬆  THAT'S IT. DON'T TOUCH ANYTHING BELOW.  ⬆⬆⬆
   ============================================================ */

const $ = s => document.querySelector(s);
const state = { data:null, selected:null, filter:'all' };

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
  const idc = document.getElementById('identify');
  if(idc) idc.hidden = view!=='identify';
}

/* IMDb links point at a catalog page, not a media file — IMDb hosts no films.
   So instead of trying to download, we IDENTIFY: resolve the title/series/person
   the link refers to (name, year, kind, poster, cast). Detect those links here
   and route them to the /identify endpoint. */
const IMDB_RE = /(?:imdb\.com|imdb\.to)\/.*(?:tt|nm|co)\d{6,9}|\b(?:tt|nm|co)\d{6,9}\b/i;
function isImdb(url){ return IMDB_RE.test(url); }

/* The identify card is built once, on demand, and reused. Keeping it out of the
   HTML means every page (main + landing pages) gets the feature from app.js
   alone, with no markup duplication. It reuses the .monitor/.thumb/.meta look
   of the results panel so it matches the broadcast-deck design. */
function identifyCard(){
  let el = document.getElementById('identify');
  if(el) return el;
  el = document.createElement('div');
  el.id = 'identify'; el.className = 'program'; el.hidden = true;
  el.innerHTML = `
    <div class="monitor">
      <div class="thumb" style="aspect-ratio:2/3">
        <img id="id-poster" alt="">
      </div>
    </div>
    <div>
      <div class="board-head"><h3>Identified</h3><span class="count" id="id-kind">—</span></div>
      <div class="meta" style="border:0;padding:0">
        <div class="lbl">From IMDb</div>
        <h2 id="id-title">—</h2>
        <div class="stats">
          <div class="stat"><span>Year</span><b id="id-year">—</b></div>
          <div class="stat"><span>Type</span><b id="id-type">—</b></div>
          <div class="stat"><span>IMDb rank</span><b id="id-rank">—</b></div>
        </div>
        <p id="id-stars" style="color:var(--muted);margin-top:var(--sp-4)"></p>
        <a id="id-link" href="#" target="_blank" rel="noopener"
           class="btn-fetch" style="margin-top:var(--sp-4);display:inline-flex;text-decoration:none">
           <i data-lucide="external-link" width="16" height="16"></i>View on IMDb</a>
        <p class="hint" style="margin-top:var(--sp-4)">This is a catalog lookup to identify the title — IMDb hosts no downloadable film.</p>
      </div>
    </div>`;
  // Drop it into the stage so show() can toggle it alongside the other views.
  ($('.stage') || document.body).appendChild(el);
  return el;
}

async function identify(url){
  identifyCard();
  show('loading');
  try{
    const r = await fetch(`${API_BASE}/identify`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})
    });
    const j = await r.json();
    if(!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    $('#id-poster').src = j.poster || '';
    $('#id-poster').alt = j.title || '';
    $('#id-title').textContent = j.title || '—';
    $('#id-kind').textContent = j.kind || '—';
    $('#id-type').textContent = j.kind || '—';
    $('#id-year').textContent = j.year_range || j.year || '—';
    $('#id-rank').textContent = j.rank ? '#'+j.rank : '—';
    $('#id-stars').textContent = j.stars ? (j.kind==='Person' ? j.stars : 'Featuring '+j.stars) : '';
    $('#id-link').href = j.imdb_url || '#';
    show('identify');
    if(window.lucide) lucide.createIcons();
    checkBackend();
  }catch(e){
    $('#errtext').innerHTML = `We couldn't identify that link. <code>${(e.message||'').replace(/\[[0-9;]*m/g,'')}</code>`;
    show('error');
  }
}

async function pull(){
  const url = $('#url').value.trim();
  if(!url){ $('#url').focus(); return; }
  // IMDb links identify, they don't download.
  if(isImdb(url)){
    $('#fetch').disabled=true;
    try{ await identify(url); } finally{ $('#fetch').disabled=false; }
    return;
  }
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
if(window.lucide) lucide.createIcons();
/* PWA: register the shell service worker so repeat visits are instant and the
   app is installable. It never caches media or API calls (see sw.js). */
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});
