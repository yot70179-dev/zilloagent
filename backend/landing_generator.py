"""
Personalised demo landing-page generator.

Built ONLY when a prospect says yes (see marketing_agent.handle_inbound_reply).
Produces a self-contained HTML page branded with the prospect's business name,
served by the app at /lp/{slug}. Photos are elegant placeholder slots the user
fills with the business's real Instagram photos — we don't scrape Instagram
(no legal/reliable API for someone else's account); a handle, if known, is
linked so the visitor can see the real feed.
"""
import os
import secrets
from html import escape

from sqlalchemy.orm import Session

from database import LandingPage, Prospect

PUBLIC_URL = os.getenv("MK_PUBLIC_URL", "").rstrip("/")


def _page_html(p: Prospect) -> str:
    he = p.segment != "US"
    biz = escape(p.business_name or (p.name or ("העסק שלך" if he else "Your Business")))
    ig = (p.linkedin_url or "").strip()  # we store any handle/url the user adds here
    tagline = "הלקוחות הבאים שלך מחכים כאן" if he else "Your next customers are waiting here"
    sub = ("דף נחיתה מהיר שממיר גולשים מהאינסטגרם ללקוחות משלמים — נבנה במיוחד עבורכם."
           if he else
           "A fast landing page that turns your Instagram visitors into paying customers — built just for you.")
    cta = "לפרטים ותיאום" if he else "Get in touch"
    dir_ = "rtl" if he else "ltr"
    photo = "📸 כאן תמונה מהאינסטגרם שלכם" if he else "📸 Your Instagram photo here"
    demo_note = ("זו דוגמה ראשונית שנבנתה עבור " + biz + " · נבנה על ידי יותם"
                 if he else "Initial sample built for " + biz + " · by Yotam")
    ig_link = (f'<a class="ig" href="{escape(ig)}" target="_blank" rel="noopener">'
               + ("עקבו אחרינו באינסטגרם" if he else "Follow us on Instagram") + "</a>") if ig else ""
    return f"""<!doctype html><html lang="{'he' if he else 'en'}" dir="{dir_}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{biz}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Assistant:wght@400;600;800&family=Secular+One&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0d0f10;--s:#15181a;--line:#2a2f31;--ink:#f4f1ec;--mut:#9aa0a2;--acc:#ff5a1f;--acc2:#ff8a3d}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);
font-family:Assistant,system-ui,sans-serif;line-height:1.6}}
h1,h2{{font-family:"Secular One",Assistant,sans-serif;line-height:1.05;margin:0}}
.wrap{{width:min(900px,92vw);margin:auto}} a{{color:inherit}}
header{{min-height:82vh;display:flex;align-items:center;padding:6rem 0;position:relative;overflow:hidden}}
header::before{{content:"";position:absolute;inset:0;background:radial-gradient(80% 70% at 80% 10%,rgba(255,90,31,.18),transparent 60%)}}
.eyebrow{{font-weight:800;letter-spacing:.2em;text-transform:uppercase;color:var(--acc2);font-size:.8rem}}
h1{{font-size:clamp(2.6rem,7vw,5rem);margin:.6rem 0}} h1 span{{color:var(--acc)}}
.lead{{color:var(--mut);font-size:1.2rem;max-width:36ch;margin:1.2rem 0 2rem}}
.btn{{display:inline-block;background:var(--acc);color:#170a03;font-weight:800;padding:1rem 2rem;
border-radius:999px;text-decoration:none;box-shadow:0 12px 30px -10px rgba(255,90,31,.7)}}
.grid{{position:relative;z-index:1;display:grid;grid-template-columns:1.1fr .9fr;gap:2.5rem;align-items:center}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
.slot{{aspect-ratio:4/5;border-radius:20px;border:1.5px dashed #34393b;background:linear-gradient(135deg,#1c2123,#121517);
display:grid;place-items:center;text-align:center;color:var(--mut);font-weight:700;padding:1rem}}
section{{padding:4rem 0;border-top:1px solid var(--line)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem}}
.card{{background:var(--s);border:1px solid var(--line);border-radius:16px;padding:1.4rem}}
.card h3{{margin:.2rem 0 .4rem;font-family:Assistant;font-weight:800}} .card p{{color:var(--mut);font-size:.95rem;margin:0}}
.gal{{display:grid;grid-template-columns:repeat(3,1fr);gap:.8rem}} @media(max-width:600px){{.gal{{grid-template-columns:1fr 1fr}}}}
.gal .slot{{aspect-ratio:1/1;border-radius:14px}}
.foot{{text-align:center;color:#6c7173;padding:2.5rem 0}}
.ig{{display:inline-block;margin-top:1rem;color:var(--acc2);font-weight:700}}
.flag{{background:rgba(255,90,31,.12);border:1px solid rgba(255,90,31,.4);color:var(--acc2);
font-weight:700;font-size:.8rem;padding:.4rem 1rem;border-radius:999px;display:inline-block;margin-bottom:1.2rem}}
</style></head><body>
<header><div class="wrap grid">
<div><span class="eyebrow">{biz}</span>
<h1>{tagline.split(' ')[0]} <span>{' '.join(tagline.split(' ')[1:])}</span></h1>
<p class="lead">{sub}</p>
<a class="btn" href="#contact">{cta}</a>{ig_link}</div>
<div class="slot">{photo}</div>
</div></header>
<section><div class="wrap">
<div class="flag">{demo_note}</div>
<div class="cards">
<div class="card"><h3>{'איכות שרואים' if he else 'Quality that shows'}</h3><p>{'עיצוב נקי שמשדר מקצועיות מהשנייה הראשונה.' if he else 'Clean design that signals professionalism instantly.'}</p></div>
<div class="card"><h3>{'מהיר בנייד' if he else 'Fast on mobile'}</h3><p>{'נטען מהר, נראה מושלם בכל טלפון.' if he else 'Loads fast, looks perfect on every phone.'}</p></div>
<div class="card"><h3>{'ממיר ללקוחות' if he else 'Converts to customers'}</h3><p>{'כפתור פנייה ברור שמביא הודעות אמיתיות.' if he else 'A clear contact button that brings real leads.'}</p></div>
</div></div></section>
<section><div class="wrap">
<h2 style="margin-bottom:1.4rem">{'מהעבודות שלנו' if he else 'Our work'}</h2>
<div class="gal"><div class="slot">📸</div><div class="slot">📸</div><div class="slot">📸</div></div>
</div></section>
<section id="contact"><div class="wrap" style="text-align:center">
<h2>{'מוכנים להתחיל?' if he else 'Ready to start?'}</h2>
<p class="lead" style="margin:1rem auto 1.6rem">{'השאירו הודעה ונחזור אליכם.' if he else 'Send a message and we will get back to you.'}</p>
<a class="btn" href="#">{cta}</a>
</div></section>
<div class="foot">© {biz} · {demo_note}</div>
</body></html>"""


def get_or_create_landing(db: Session, prospect: Prospect) -> LandingPage:
    lp = db.query(LandingPage).filter(LandingPage.prospect_id == prospect.id).first()
    if lp:
        return lp
    lp = LandingPage(prospect_id=prospect.id, slug=secrets.token_urlsafe(8),
                     html=_page_html(prospect))
    db.add(lp)
    db.commit()
    db.refresh(lp)
    return lp


def landing_url(lp: LandingPage) -> str:
    return f"{PUBLIC_URL}/lp/{lp.slug}" if PUBLIC_URL else f"/lp/{lp.slug}"
