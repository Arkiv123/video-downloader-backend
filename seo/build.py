#!/usr/bin/env python3
"""
pSEO landing-page generator.

Reads seo/config.json and emits one static HTML page per entry into the repo
root (so Cloudflare Pages serves them at /<slug>/), plus sitemap.xml and
robots.txt. Every page shares the SAME backend API and the SAME download logic
as index.html — this is the "one engine, hundreds of doors" strategy: rank for
lots of low-competition long-tail terms without duplicating the backend.

Run:  python seo/build.py
It is safe to re-run; it overwrites generated files only.
"""

import json
import os
import html

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(HERE, "config.json")

# Marker so the generator only ever cleans up files it created.
GEN_MARKER = "<!-- generated:pseo -->"


def esc(s):
    return html.escape(str(s), quote=True)


def faq_jsonld(faq):
    """Schema.org FAQPage markup — this is what wins the rich-result FAQ
    dropdowns in Google, a big pSEO edge for zero extra content."""
    items = [{
        "@type": "Question",
        "name": q,
        "acceptedAnswer": {"@type": "Answer", "text": a},
    } for q, a in faq]
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": items,
    }, ensure_ascii=False)


def render_faq(faq):
    rows = "\n".join(
        f'      <details class="faq-item"><summary>{esc(q)}</summary>'
        f'<p>{esc(a)}</p></details>'
        for q, a in faq
    )
    return rows


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
{marker}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{h1} · {brand}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{origin}/{slug}/">
<meta property="og:type" content="website">
<meta property="og:title" content="{h1} · {brand}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{origin}/{slug}/">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="manifest" href="/manifest.webmanifest">
<script type="application/ld+json">{jsonld}</script>
<style>
  :root{{
    --bg:#0b0d10; --panel:#14181d; --panel-2:#1b2027; --panel-3:#232a33;
    --ink:#eef2f6; --faint:#8a97a6; --line:#2a323c; --accent:#ff3b30;
    --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-6:24px;
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
    font-family:'Space Grotesk',system-ui,sans-serif;line-height:1.55}}
  a{{color:var(--accent)}}
  .wrap{{max-width:820px;margin:0 auto;padding:var(--sp-6) var(--sp-4) 80px}}
  .rail{{display:flex;align-items:center;gap:var(--sp-3);padding-bottom:var(--sp-6);
    font:600 12px/1 'JetBrains Mono',monospace;letter-spacing:.08em;color:var(--faint)}}
  .wordmark{{font-family:Oswald;font-weight:700;letter-spacing:.06em;color:var(--ink)}}
  .wordmark span{{color:var(--accent)}}
  .status{{display:inline-flex;align-items:center;gap:6px}}
  .status .dot{{width:8px;height:8px;border-radius:50%;background:var(--faint)}}
  .status[data-state=live] .dot{{background:#22c55e}}
  .status[data-state=off] .dot{{background:var(--accent)}}
  h1{{font-family:Oswald;font-weight:700;font-size:clamp(28px,6vw,46px);
    line-height:1.05;margin:0 0 var(--sp-3);text-transform:uppercase;letter-spacing:.01em}}
  .tagline{{color:var(--faint);font-size:17px;margin:0 0 var(--sp-6);max-width:60ch}}
  .feed{{display:flex;gap:var(--sp-3);background:var(--panel);border:1px solid var(--line);
    border-radius:14px;padding:var(--sp-2);flex-wrap:wrap}}
  .in-wrap{{flex:1;display:flex;align-items:center;gap:var(--sp-3);padding-left:var(--sp-3);min-width:220px}}
  .ch{{font:700 11px/1 'JetBrains Mono',monospace;color:var(--accent);letter-spacing:.1em}}
  input{{flex:1;background:transparent;border:0;outline:0;color:var(--ink);
    font-size:15px;padding:14px 0;min-width:0}}
  input::placeholder{{color:var(--faint)}}
  .btn{{border:0;border-radius:10px;padding:14px 20px;font-family:Oswald;font-weight:600;
    letter-spacing:.05em;text-transform:uppercase;cursor:pointer;font-size:14px}}
  .btn-fetch{{background:var(--accent);color:#fff}}
  .btn-fetch:disabled{{opacity:.5;cursor:not-allowed}}
  .hint{{color:var(--faint);font-size:13px;margin:var(--sp-3) 2px}}
  .stage{{margin-top:var(--sp-6)}}
  .card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:var(--sp-4)}}
  .msg{{color:var(--faint)}}
  .msg b{{color:var(--ink)}}
  .opt{{display:flex;justify-content:space-between;align-items:center;gap:var(--sp-3);
    padding:12px 14px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;cursor:pointer;background:var(--panel-2)}}
  .opt[aria-pressed=true]{{border-color:var(--accent);background:var(--panel-3)}}
  .opt .lbl{{font-weight:600}}
  .opt .meta{{font:600 12px/1 'JetBrains Mono',monospace;color:var(--faint)}}
  .dock{{display:flex;align-items:center;gap:var(--sp-3);margin-top:var(--sp-4)}}
  .bar-wrap{{flex:1;height:8px;background:var(--panel-3);border-radius:99px;overflow:hidden}}
  .bar{{height:100%;width:0;background:var(--accent);transition:width .2s}}
  .btn-air{{background:var(--accent);color:#fff}}
  .btn-air:disabled{{background:var(--panel-3);color:var(--faint);cursor:not-allowed}}
  .prose{{margin-top:56px;color:var(--faint)}}
  .prose h2{{font-family:Oswald;color:var(--ink);letter-spacing:.02em;margin:32px 0 10px;font-size:22px}}
  .faq-item{{border-top:1px solid var(--line);padding:14px 0}}
  .faq-item summary{{cursor:pointer;color:var(--ink);font-weight:600}}
  .faq-item p{{margin:10px 0 0}}
  .other{{margin-top:40px;display:flex;flex-wrap:wrap;gap:8px}}
  .other a{{font:600 12px/1 'JetBrains Mono',monospace;color:var(--faint);text-decoration:none;
    border:1px solid var(--line);border-radius:99px;padding:8px 12px}}
  .other a:hover{{color:var(--ink);border-color:var(--accent)}}
  footer{{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);
    color:var(--faint);font-size:13px}}
  footer a{{color:var(--faint)}}
  [hidden]{{display:none !important}}
</style>
</head>
<body>
<div class="wrap">
  <div class="rail">
    <span class="status" id="status" data-state="checking"><span class="dot"></span><span id="statusText">CHECKING</span></span>
    <a href="/" style="text-decoration:none"><span class="wordmark">GOOGLY <span>RANKS</span></span></a>
  </div>

  <h1>{h1}</h1>
  <p class="tagline">{tagline}</p>

  <div class="feed">
    <div class="in-wrap">
      <span class="ch">SRC &#9656;</span>
      <input id="url" type="url" placeholder="{placeholder}" autocomplete="off" spellcheck="false">
    </div>
    <button class="btn btn-fetch" id="fetch">Pull Feed</button>
  </div>
  <p class="hint">Free · no signup · paste a link and hit <b>Enter</b>.</p>

  <div class="stage">
    <div class="card" id="empty">
      <p class="msg"><b>Ready.</b> Paste a link above to list every quality it carries.</p>
    </div>
    <div class="card" id="error" hidden><p class="msg"><b>Feed dropped.</b> <span id="errtext"></span></p></div>
    <div class="card" id="loading" hidden><p class="msg">Reading feed…</p></div>
    <div id="results" hidden>
      <div class="card">
        <p class="msg"><b id="title">—</b></p>
        <div id="opts"></div>
      </div>
      <div class="dock" id="dock" hidden>
        <div class="bar-wrap"><div class="bar" id="bar"></div></div>
        <span class="meta" id="ro-pct">0%</span>
        <button class="btn btn-air" id="air" disabled>Download</button>
      </div>
    </div>
  </div>

  <div class="prose">
    <h2>How to use this {keyword}</h2>
    <p>Copy the link to the video you want, paste it in the box above, and press
    Pull Feed. Every resolution and audio track the source carries is listed —
    pick one and hit Download. It runs entirely in your browser; nothing is
    installed and no account is needed.</p>
{faq_html}
  </div>

  <div class="other">{other_links}</div>

  <footer>
    <p>{brand} is a metadata tool for content you own or have the right to
    download. Respect the rights of creators and each platform's terms.
    <a href="/">Home</a></p>
  </footer>
</div>

<script>
const API_BASE = "{api_base}";
const $ = s => document.querySelector(s);
const state = {{data:null, selected:null}};
function fmtSize(b){{ if(!b) return ''; const u=['B','KB','MB','GB']; let i=0; while(b>=1024&&i<u.length-1){{b/=1024;i++;}} return b.toFixed(1)+u[i]; }}

async function checkBackend(){{
  const st=$('#status'), t=$('#statusText');
  try{{ const c=new AbortController(); setTimeout(()=>c.abort(),8000);
    const r=await fetch(`${{API_BASE}}/`,{{signal:c.signal}});
    st.dataset.state=r.ok?'live':'off'; t.textContent=r.ok?'LIVE':'OFF';
  }}catch(e){{ st.dataset.state='off'; t.textContent='OFF'; }}
}}

function show(v){{ ['empty','error','loading','results'].forEach(x=>$('#'+x).hidden=x!==v); }}

function render(){{
  const box=$('#opts'); box.innerHTML='';
  (state.data.formats||[]).forEach(f=>{{
    const el=document.createElement('div'); el.className='opt'; el.setAttribute('aria-pressed','false');
    el.innerHTML=`<span class="lbl">${{f.label}}</span><span class="meta">${{f.filesize?fmtSize(f.filesize):(f.type==='audio'?'audio':'video')}}</span>`;
    el.onclick=()=>{{ state.selected=f.format_id;
      document.querySelectorAll('.opt').forEach(o=>o.setAttribute('aria-pressed','false'));
      el.setAttribute('aria-pressed','true'); $('#dock').hidden=false; $('#air').disabled=false; }};
    box.appendChild(el);
  }});
}}

async function pull(){{
  const url=$('#url').value.trim(); if(!url){{ $('#url').focus(); return; }}
  state.selected=null; $('#fetch').disabled=true; show('loading');
  try{{
    const r=await fetch(`${{API_BASE}}/formats`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{url}})}});
    const j=await r.json();
    if(!r.ok) throw new Error(j.detail||`HTTP ${{r.status}}`);
    if(!j.formats||!j.formats.length) throw new Error('No downloadable formats found.');
    state.data=j; $('#title').textContent=j.title||'Untitled'; show('results'); render(); checkBackend();
  }}catch(e){{ $('#errtext').textContent=(e.message||'').replace(/\\u001b\\[[0-9;]*m/g,''); show('error'); }}
  finally{{ $('#fetch').disabled=false; }}
}}

async function saveResponse(r, fallbackName){{
  const bar=$('#bar'), pct=$('#ro-pct'); const total=+r.headers.get('content-length')||0;
  const reader=r.body.getReader(); const chunks=[]; let got=0;
  while(true){{ const {{done,value}}=await reader.read(); if(done) break; chunks.push(value); got+=value.length;
    if(total){{ const p=Math.min(100,Math.round(got/total*100)); bar.style.width=p+'%'; pct.textContent=p+'%'; }}
    else {{ pct.textContent=fmtSize(got); bar.style.width='60%'; }} }}
  bar.style.width='100%'; pct.textContent='100%';
  let name=fallbackName; const cd=r.headers.get('content-disposition');
  const m=cd&&/filename[^;=\\n]*=(?:UTF-8''|")?([^";\\n]+)/i.exec(cd);
  if(m){{ try{{ name=decodeURIComponent(m[1]); }}catch(e){{ name=m[1]; }} }}
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob(chunks)); a.download=name;
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),4000);
}}

async function goLive(){{
  if(!state.selected||!state.data) return;
  const air=$('#air'); air.disabled=true; $('#bar').style.width='0%'; $('#ro-pct').textContent='0%';
  const sel=state.data.formats.find(x=>x.format_id===state.selected);
  let name=(state.data.title||'video').replace(/[\\\\/:*?"<>|]/g,'').slice(0,80)+'.'+((sel&&sel.type==='audio')?(sel.ext||'m4a'):((sel&&sel.ext)||'mp4'));
  try{{
    if(sel&&sel.direct_url){{ try{{ const dr=await fetch(sel.direct_url);
      if(dr.ok&&dr.body){{ await saveResponse(dr,name); return; }} throw 0; }}catch(_){{}} }}
    const body=JSON.stringify({{url:$('#url').value.trim(),format_id:state.selected,
      audio_only:!!(sel&&sel.type==='audio'),height:(sel&&sel.type==='video'&&sel.height)?sel.height:null}});
    let r,tries=0;
    while(true){{ r=await fetch(`${{API_BASE}}/download`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body}});
      if(r.status!==503||tries>=4) break; tries++; await new Promise(x=>setTimeout(x,1500*tries)); }}
    if(!r.ok){{ const j=await r.json().catch(()=>({{}})); throw new Error(j.detail||`HTTP ${{r.status}}`); }}
    await saveResponse(r,name);
  }}catch(e){{ $('#errtext').textContent='Download stalled. '+(e.message||''); show('error'); }}
  finally{{ air.disabled=false; }}
}}

$('#fetch').onclick=pull;
$('#url').addEventListener('keydown',e=>{{ if(e.key==='Enter') pull(); }});
$('#air').onclick=goLive;
let warmed=false;
function warm(){{ if(warmed) return; warmed=true; fetch(`${{API_BASE}}/`).then(checkBackend).catch(()=>{{}}); setTimeout(()=>warmed=false,60000); }}
$('#url').addEventListener('focus',warm);
$('#url').addEventListener('input',warm);
checkBackend();
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{{}});
</script>
</body>
</html>
"""


def build():
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    site = cfg["site"]
    pages = cfg["pages"]
    origin = site["origin"].rstrip("/")

    # cross-links: every page links to a few siblings for internal-link juice
    def other_links(current_slug):
        out = []
        for p in pages:
            if p["slug"] == current_slug:
                continue
            out.append(f'<a href="/{p["slug"]}/">{esc(p["h1"])}</a>')
        return "\n    ".join(out)

    written = []
    for p in pages:
        slug = p["slug"]
        desc = f'{p["tagline"]} Free {p["keyword"]} — no signup, works in your browser.'
        page_dir = os.path.join(ROOT, slug)
        os.makedirs(page_dir, exist_ok=True)
        out_path = os.path.join(page_dir, "index.html")
        rendered = PAGE.format(
            marker=GEN_MARKER,
            brand=esc(site["brand"]),
            origin=origin,
            api_base=site["api_base"],
            slug=slug,
            h1=esc(p["h1"]),
            tagline=esc(p["tagline"]),
            desc=esc(desc),
            keyword=esc(p["keyword"]),
            placeholder=esc(p["placeholder"]),
            jsonld=faq_jsonld(p["faq"]),
            faq_html=render_faq(p["faq"]),
            other_links=other_links(slug),
        )
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        written.append((slug, out_path))
        print(f"  wrote {slug}/index.html")

    # sitemap.xml — home first, then every generated page
    urls = [f"{origin}/"] + [f"{origin}/{p['slug']}/" for p in pages]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sm.append(f"  <url><loc>{esc(u)}</loc></url>")
    sm.append("</urlset>")
    with open(os.path.join(ROOT, "sitemap.xml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sm) + "\n")
    print("  wrote sitemap.xml")

    # robots.txt
    with open(os.path.join(ROOT, "robots.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n")
    print("  wrote robots.txt")

    print(f"\nDone. {len(written)} landing pages generated.")


if __name__ == "__main__":
    build()
