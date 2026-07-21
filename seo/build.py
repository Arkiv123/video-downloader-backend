#!/usr/bin/env python3
"""
pSEO landing-page generator.

Reads seo/config.json and emits one static HTML page per entry into the repo
root (so Cloudflare Pages serves them at /<slug>/), plus sitemap.xml and
robots.txt.

Brand match: every landing page reuses the SAME design as index.html — it links
the shared `/styles.css` and `/app.js` and uses the identical broadcast-deck
body markup (rail, hero, feed, stage, dock). The only per-page differences are
the hero copy, the SEO meta, and an SEO prose/FAQ block below the tool. This is
the "one engine, many doors" strategy: rank for lots of long-tail terms with a
consistent look and zero backend duplication.

Run:  python seo/build.py   (safe to re-run; overwrites generated files only)
"""

import json
import os
import html
import datetime as _dt
try:
    from . import ogimage  # when run as a module
except Exception:
    import ogimage         # when run as a script from seo/

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(HERE, "config.json")

# Marker so we only ever clean up files we created.
GEN_MARKER = "<!-- generated:pseo -->"


def esc(s):
    return html.escape(str(s), quote=True)


def page_jsonld(p, site, origin):
    """Schema.org @graph for a landing page. One <script> carrying several linked
    entities is how Google prefers structured data now — the @id cross-references
    let it understand these describe the same site/app/page rather than four
    unrelated blobs. We emit:
      • WebApplication — tells Google this is a free browser tool (price 0). This
        is what can surface the app-style rich result and the "Free" badge.
      • FAQPage        — wins the expandable FAQ dropdowns in the SERP.
      • BreadcrumbList — draws the Home › Page trail under the result title.
      • Organization / WebSite — brand entity + sitelinks-searchbox eligibility.
    NOTE: we deliberately DON'T fake aggregateRating/review — invented ratings are
    a manual-action risk and get stripped anyway. Earn them, don't invent them."""
    brand, slug = site["brand"], p["slug"]
    page_url = f"{origin}/{slug}/"
    faq_items = [{
        "@type": "Question",
        "name": q,
        "acceptedAnswer": {"@type": "Answer", "text": a},
    } for q, a in p["faq"]]
    graph = [
        {
            "@type": "Organization",
            "@id": f"{origin}/#org",
            "name": brand,
            "url": f"{origin}/",
            "logo": f"{origin}/icon.svg",
        },
        {
            "@type": "WebSite",
            "@id": f"{origin}/#site",
            "url": f"{origin}/",
            "name": brand,
            "publisher": {"@id": f"{origin}/#org"},
        },
        {
            "@type": "WebApplication",
            "@id": f"{page_url}#app",
            "name": p["h1"],
            "url": page_url,
            "applicationCategory": "MultimediaApplication",
            "operatingSystem": "Any (web-based)",
            "browserRequirements": "Requires JavaScript. Runs in any modern browser.",
            "isPartOf": {"@id": f"{origin}/#site"},
            "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        },
        {
            "@type": "FAQPage",
            "@id": f"{page_url}#faq",
            "isPartOf": {"@id": f"{page_url}#app"},
            "mainEntity": faq_items,
        },
        {
            "@type": "BreadcrumbList",
            "@id": f"{page_url}#crumbs",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{origin}/"},
                {"@type": "ListItem", "position": 2, "name": p["h1"], "item": page_url},
            ],
        },
    ]
    return json.dumps({"@context": "https://schema.org", "@graph": graph},
                      ensure_ascii=False)


def render_faq(faq):
    return "\n".join(
        f'    <details class="lp-faq"><summary>{esc(q)}</summary>'
        f'<p>{esc(a)}</p></details>'
        for q, a in faq
    )


# The body markup below is a trimmed copy of index.html's — same classes, so the
# shared /styles.css styles it identically. app.js binds to these same IDs.
PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
{marker}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{h1} · {brand}</title>
<meta name="description" content="{desc}">
<meta name="keywords" content="{keyword}, {keyword} free, {keyword} online, download {keyword}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1">
<meta name="author" content="{brand}">
<link rel="canonical" href="{origin}/{slug}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{brand}">
<meta property="og:title" content="{h1} · {brand}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{origin}/{slug}/">
<meta property="og:image" content="{origin}/og/{slug}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{h1} — {brand}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{h1} · {brand}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{origin}/og/{slug}.png">
<meta name="theme-color" content="#0b0d10">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/styles.css">
<script type="application/ld+json">{jsonld}</script>
<style>
  /* Landing-only supplements — the SEO prose block below the tool. Everything
     above uses the shared broadcast-deck styles from /styles.css. */
  .lp-crumbs{{display:flex;align-items:center;gap:8px;margin-top:var(--sp-4);
    font-family:var(--mono);font-size:.7rem;font-weight:700;letter-spacing:.08em;
    text-transform:uppercase;color:var(--faint)}}
  .lp-crumbs a{{color:var(--muted);text-decoration:none}}
  .lp-crumbs a:hover{{color:var(--lime)}}
  .lp-crumbs span[aria-current]{{color:var(--text)}}
  .lp-prose{{margin-top:var(--sp-16);max-width:70ch}}
  .lp-prose h2{{font-family:var(--disp);text-transform:uppercase;letter-spacing:.03em;
    color:var(--text);font-size:1.4rem;margin:var(--sp-8) 0 var(--sp-3)}}
  .lp-prose p{{color:var(--muted);margin-bottom:var(--sp-3)}}
  .lp-steps{{color:var(--muted);margin:0 0 var(--sp-4) 1.1em;padding:0;display:flex;
    flex-direction:column;gap:var(--sp-2)}}
  .lp-steps li{{padding-left:6px}}
  .lp-steps b{{color:var(--text)}}
  .lp-faq{{border-top:1px solid var(--line-soft);padding:var(--sp-4) 0}}
  .lp-faq summary{{cursor:pointer;color:var(--text);font-weight:600;font-family:var(--disp);
    text-transform:uppercase;letter-spacing:.02em;font-size:1.02rem}}
  .lp-faq p{{margin:var(--sp-3) 0 0}}
  .lp-other{{margin-top:var(--sp-12);display:flex;flex-wrap:wrap;gap:8px}}
  .lp-other a{{font-family:var(--mono);font-size:0.68rem;font-weight:700;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--muted);text-decoration:none;
    border:1px solid var(--line-soft);border-radius:99px;padding:6px 12px;transition:all .16s}}
  .lp-other a:hover{{color:var(--lime);border-color:var(--lime-dim)}}
  .lp-foot{{margin-top:var(--sp-12);padding-top:var(--sp-4);border-top:1px solid var(--line-soft);
    color:var(--faint);font-size:0.85rem}}
  .lp-foot a{{color:var(--muted)}}
</style>
</head>
<body>
<div class="wrap">

  <!-- top rail -->
  <div class="rail">
    <span class="status" id="status" data-state="checking" title="Backend status">
      <span class="dot"></span><span id="statusText">CHECKING</span>
    </span>
    <a href="/" style="text-decoration:none;color:inherit"><span class="wordmark"><img src="/icon.svg" alt="" class="brand-mark" width="28" height="28">GOOGLY <span>RANKS</span></span></a>
    <span class="rail-spacer"></span>
    <span class="clock" id="clock">--:--:--</span>
  </div>

  <!-- breadcrumb (mirrors the BreadcrumbList schema) -->
  <nav class="lp-crumbs" aria-label="Breadcrumb">
    <a href="/">Home</a><span aria-hidden="true">›</span><span aria-current="page">{h1}</span>
  </nav>

  <!-- hero (page-specific copy) -->
  <header class="hero">
    <div class="eyebrow">Studio Feed · Grab &amp; Grade · {keyword_upper}</div>
    <h1>{h1_html}</h1>
    <p>{tagline}</p>
    <div class="sources">
      <span class="src-chip">YouTube</span>
      <span class="src-chip">TikTok</span>
      <span class="src-chip">Instagram</span>
      <span class="src-chip">X / Twitter</span>
      <span class="src-chip">Facebook</span>
      <span class="src-chip">Reddit</span>
      <span class="src-chip">+1000 more</span>
    </div>
  </header>

  <!-- feed input -->
  <div class="feed">
    <div class="in-wrap">
      <span class="ch">SRC &#9656;</span>
      <input id="url" type="url" placeholder="{placeholder}" autocomplete="off" spellcheck="false">
    </div>
    <button class="btn-fetch" id="fetch"><i data-lucide="radio" width="18" height="18"></i>Pull Feed</button>
  </div>
  <p class="hint">Tip: hit <b>Enter</b> to pull.</p>

  <!-- stage -->
  <div class="stage">
    <div class="empty" id="empty">
      <i data-lucide="satellite-dish" width="46" height="46"></i>
      <h3>No feed on air</h3>
      <p>Paste a link above and pull it in. The board fills with every quality your source offers, ready to grab.</p>
    </div>

    <div class="error" id="error" hidden>
      <i data-lucide="signal-zero" width="20" height="20"></i>
      <div class="msg"><b>Feed dropped</b><span id="errtext"></span></div>
    </div>

    <div class="program" id="loading" hidden>
      <div class="monitor"><div class="sk sk-mon"></div></div>
      <div>
        <div class="board-head"><h3>Reading feed…</h3></div>
        <div class="sk-board">
          <div class="sk sk-row"></div><div class="sk sk-row"></div>
          <div class="sk sk-row"></div><div class="sk sk-row"></div>
        </div>
      </div>
    </div>

    <div class="program" id="results" hidden>
      <div class="monitor">
        <div class="thumb">
          <img id="thumb" alt="">
          <div class="scan"></div>
          <span class="live"><span class="d"></span>LIVE</span>
          <span class="dur" id="dur">0:00</span>
        </div>
        <div class="meta">
          <div class="lbl">Now On Air</div>
          <h2 id="title">—</h2>
          <div class="stats">
            <div class="stat"><span>Tracks</span><b id="s-tracks">0</b></div>
            <div class="stat"><span>Best</span><b id="s-best">—</b></div>
            <div class="stat"><span>Runtime</span><b id="s-run">0:00</b></div>
          </div>
        </div>
      </div>

      <div>
        <div class="board-head">
          <h3>Grade Board</h3>
          <span class="count" id="count">0 signals</span>
        </div>
        <div class="seg" role="group" aria-label="Filter">
          <button data-filter="all" aria-pressed="true">All</button>
          <button data-filter="video" aria-pressed="false">Video</button>
          <button data-filter="audio" aria-pressed="false">Audio</button>
        </div>
        <div class="board" id="board"></div>
      </div>
    </div>
  </div>

  <div class="dock" id="dock" hidden>
    <div class="tally"><span class="bulb"></span><span id="tally-label">Standby</span></div>
    <div class="readout">
      <div class="top"><span id="ro-name">Select a grade</span><b id="ro-pct">0%</b></div>
      <div class="prog"><i id="bar"></i></div>
    </div>
    <button class="btn-air" id="air" disabled><i data-lucide="download" width="20" height="20"></i>On Air</button>
  </div>

  <!-- SEO prose + FAQ (page-specific, static) -->
  <section class="lp-prose">
    <h2>How to use this {keyword}</h2>
    <p>{howto}</p>
    <ol class="lp-steps">
      <li>Copy the link to the video or track you want to save.</li>
      <li>Paste it into the box at the top of this page and press <b>Pull Feed</b>.</li>
      <li>Pick the quality or audio you want from the board, then hit <b>On Air</b> to save it to your device.</li>
    </ol>

    <h2>Quality &amp; formats</h2>
    <p>Every resolution the source carries is listed for you automatically — from
    4K and 1080p down to lighter sizes — plus an audio-only option for a clean MP3.
    Nothing is upscaled or faked: you always get exactly what the source offers,
    and high-resolution video and audio are merged into one file where needed.</p>

    <h2>Free, on any device — no app</h2>
    <p>{brand}'s {keyword} runs entirely in your browser, so it works the same on
    Android, iPhone, iPad, Windows, Mac and Linux. There is no app to install, no
    account to create, and no limit on how many links you pull.</p>
{faq_html}
  </section>

  <nav class="lp-other" aria-label="More downloaders">{other_links}</nav>

  <footer class="lp-foot">
    <p>{brand} is a metadata tool for content you own or have the right to
    download. Respect the rights of creators and each platform's terms.</p>
    <p style="margin-top:8px">
      <a href="/">Home</a> &nbsp;·&nbsp;
      <a href="/terms/">Terms</a> &nbsp;·&nbsp;
      <a href="/privacy/">Privacy</a> &nbsp;·&nbsp;
      <a href="/dmca/">DMCA</a>
    </p>
  </footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/lucide@0.383.0/dist/umd/lucide.min.js"></script>
<script>document.addEventListener("DOMContentLoaded",()=>window.lucide&&lucide.createIcons())</script>
<script src="/app.js"></script>
</body>
</html>
"""


def build():
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    site = cfg["site"]
    pages = cfg["pages"]
    origin = site["origin"].rstrip("/")

    # Cross-links: every page links to its siblings for internal-link juice.
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
            slug=slug,
            h1=esc(p["h1"]),
            # H1 shown in the hero, uppercased by CSS; keep as plain text.
            h1_html=esc(p["h1"]),
            tagline=esc(p["tagline"]),
            desc=esc(desc),
            keyword=esc(p["keyword"]),
            keyword_upper=esc(p["keyword"].upper()),
            howto=esc(p.get("howto") or (
                "Copy the link to the video you want, paste it in the box above, "
                "and hit Pull Feed. Every resolution and audio track the source "
                "carries is laid out on the board — pick one and grab it. It runs "
                "in your browser, nothing to install and no account needed.")),
            placeholder=esc(p["placeholder"]),
            jsonld=page_jsonld(p, site, origin),
            faq_html=render_faq(p["faq"]),
            other_links=other_links(slug),
        )
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        written.append(slug)
        print(f"  wrote {slug}/index.html")

        # OG card (1200x630 PNG) for rich social previews. Best-effort: if Pillow
        # is missing, ogimage.render returns False and we keep the icon fallback.
        og_ok = ogimage.render(
            title=p["h1"], brand=site["brand"],
            subtitle="Free · No signup · 1000+ sites · Every quality",
            out_path=os.path.join(ROOT, "og", f"{slug}.png"),
        )
        if og_ok:
            print(f"  wrote og/{slug}.png")

    # sitemap.xml — home first (priority 1.0), then every generated page (0.8).
    # lastmod uses today's build date so crawlers see a fresh signal each deploy.
    today = _dt.date.today().isoformat()
    entries = [(f"{origin}/", "1.0", "daily")]
    entries += [(f"{origin}/{p['slug']}/", "0.8", "weekly") for p in pages]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, prio, freq in entries:
        sm.append(
            f"  <url><loc>{esc(loc)}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>"
        )
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
