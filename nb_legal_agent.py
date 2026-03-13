"""
NB Legal Agent v7 - NB Criminal Law Case Monitor
New in v7:
  - SECOND SOURCE: NB Courts website (courtsnb-coursnb.ca)
    * Scrapes Court of Appeal decisions directly as PDFs
    * Typically 1-4 weeks AHEAD of CanLII for Court of Appeal cases
    * Both English and French decisions captured
    * PDF text extracted and passed to Claude for summary + analysis
  - Fixed CanLII topics filter to handle missing topic data (NB not yet enriched)
  - Cases from both sources deduplicated by citation number

All v6 features retained:
  - Compatible with GitHub Actions (free cloud hosting)
  - Keys read from environment variables
  - "Comments" analytical section (7 sub-sections) per case
  - CanLII topic + keyword criminal law filtering
  - English + French monitoring
  - Smart lookback window
  - Rich subject line and table of contents in email
"""

import os
import re
import io
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import Counter

# ============================================================
# CONFIGURATION — keys come from GitHub Secrets / env vars
# ============================================================

CANLII_API_KEY    = os.environ.get("CANLII_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_SENDER      = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD    = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER    = os.environ.get("EMAIL_RECEIVER", "")

# ============================================================
# COURT DEFINITIONS — CanLII sources
# ============================================================

NB_COURTS_CANLII = [
    {"id": "nbpc", "name": "NB Provincial Court (EN)",          "lang": "en"},
    {"id": "nbkb", "name": "Court of King's Bench (EN)",        "lang": "en"},
    {"id": "nbca", "name": "NB Court of Appeal — CanLII (EN)", "lang": "en"},
    {"id": "nbcp", "name": "Cour provinciale du N.-B. (FR)",    "lang": "fr"},
    {"id": "nbbr", "name": "Cour du Banc du Roi du N.-B. (FR)", "lang": "fr"},
    {"id": "nbca", "name": "Cour d'appel du N.-B. — CanLII (FR)", "lang": "fr"},
]

# NB Courts website — Court of Appeal decisions by month
# URL pattern: /content/cour/{lang}/appeal/content/decisions/{year}/{month}.html
# French:      /content/cour/fr/appel/content/decisions/{year}/{mars}.html
NBCOURTS_BASE = "https://www.courtsnb-coursnb.ca"
NBCOURTS_CA_EN = "/content/cour/en/appeal/content/decisions/{year}/{month}.html"
NBCOURTS_CA_FR = "/content/cour/fr/appel/content/decisions/{year}/{month_fr}.html"

MONTH_NAMES_EN = {
    1:"january", 2:"february", 3:"march", 4:"april",
    5:"may", 6:"june", 7:"july", 8:"august",
    9:"september", 10:"october", 11:"november", 12:"december"
}
MONTH_NAMES_FR = {
    1:"janvier", 2:"fevrier", 3:"mars", 4:"avril",
    5:"mai", 6:"juin", 7:"juillet", 8:"aout",
    9:"septembre", 10:"octobre", 11:"novembre", 12:"decembre"
}

# ============================================================
# CRIMINAL LAW FILTER
# ============================================================

CRIMINAL_TOPIC_IDS = {
    "8REJ", "8REM", "8REL", "8REK", "8REI",
    "8REH", "8REG", "8REF", "8REE",
}

CRIMINAL_KEYWORDS_EN = [
    "r. v.", "r v ", "regina v", "the queen v", "the king v",
    "assault", "sexual assault", "murder", "manslaughter", "homicide",
    "robbery", "theft", "fraud", "mischief", "arson", "break and enter",
    "possession", "trafficking", "drug", "impaired", "dangerous driving",
    "criminal negligence", "uttering threats", "forcible confinement",
    "kidnapping", "extortion", "weapons", "firearm", "bail", "remand",
    "sentencing", "sentence", "guilty plea", "criminal code", "charter",
    "search and seizure", "arrest", "wiretap", "disclosure",
    "stay of proceedings", "acquittal", "conviction", "parole", "probation",
    "conditional sentence", "preliminary inquiry", "indictment",
    "summary conviction", "ycja", "youth criminal justice",
]

CRIMINAL_KEYWORDS_FR = [
    "r. c.", "r c ", "regina c", "le roi c", "la reine c",
    "voies de fait", "agression sexuelle", "meurtre", "homicide",
    "vol qualifié", "vol", "fraude", "méfait", "incendie criminel",
    "introduction par effraction", "possession", "trafic", "drogue",
    "facultés affaiblies", "conduite dangereuse", "négligence criminelle",
    "menaces", "séquestration", "enlèvement", "extorsion", "arme",
    "arme à feu", "mise en liberté provisoire", "détention", "détermination",
    "peine", "plaidoyer de culpabilité", "code criminel", "charte",
    "fouille", "saisie", "arrestation", "écoute électronique", "divulgation",
    "arrêt des procédures", "acquittement", "déclaration de culpabilité",
    "liberté conditionnelle", "probation", "enquête préliminaire",
    "acte d'accusation", "lsjpa", "justice pénale pour les adolescents",
]

# ============================================================
# STATE
# ============================================================

STATE_FILE              = "agent_state.json"
FIRST_RUN_LOOKBACK_DAYS = 30
LOOKBACK_BUFFER_DAYS    = 2


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return set(data.get("seen_cases", [])), data.get("last_run_date", None)
    return set(), None


def save_state(seen_cases, run_date):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen_cases": list(seen_cases), "last_run_date": run_date}, f, indent=2)


def get_lookback_date(last_run_date):
    if last_run_date is None:
        print(f"   First run — looking back {FIRST_RUN_LOOKBACK_DAYS} days")
        return (datetime.now() - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    lookback = datetime.strptime(last_run_date, "%Y-%m-%d") - timedelta(days=LOOKBACK_BUFFER_DAYS)
    print(f"   Last run: {last_run_date} — looking back to {lookback.strftime('%Y-%m-%d')}")
    return lookback.strftime("%Y-%m-%d")


# ============================================================
# CRIMINAL LAW FILTERING
# ============================================================

def is_criminal_by_topics(topics):
    """
    Returns True  = confirmed criminal by CanLII topics
    Returns False = confirmed NOT criminal
    Returns None  = no topic data (NB enrichment not yet rolled out — fall through)
    """
    if not topics:
        return None
    topic_ids = set()
    for t in topics:
        if isinstance(t, dict):
            topic_ids.add(t.get("topicId", ""))
        elif isinstance(t, str):
            topic_ids.add(t)
    if not topic_ids:
        return None
    if topic_ids & CRIMINAL_TOPIC_IDS:
        return True
    return False


def is_criminal_by_keywords(title, citation, text_snippet="", lang="en"):
    haystack = (title + " " + citation + " " + text_snippet).lower()
    keywords = CRIMINAL_KEYWORDS_FR if lang == "fr" else CRIMINAL_KEYWORDS_EN
    return any(kw.lower() in haystack for kw in keywords)


# ============================================================
# CANLII API
# ============================================================

def fetch_recent_cases_canlii(court_id, lang, since_date):
    url = (
        f"https://api.canlii.org/v1/caseBrowse/{lang}/{court_id}/"
        f"?offset=0&resultCount=50&publishedAfter={since_date}&api_key={CANLII_API_KEY}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json().get("cases", [])


def fetch_case_metadata(court_id, case_id, lang):
    url = (
        f"https://api.canlii.org/v1/caseBrowse/{lang}/{court_id}/{case_id}/"
        f"?api_key={CANLII_API_KEY}"
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("topics", []), data.get("keywords", [])
    except Exception:
        return [], []


def fetch_case_text_canlii(court_id, case_id, lang):
    url = (
        f"https://api.canlii.org/v1/caseDocument/{lang}/{court_id}/{case_id}/"
        f"?api_key={CANLII_API_KEY}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("documentContent", data.get("excerpt", ""))


# ============================================================
# NB COURTS WEBSITE — Court of Appeal scraper
# ============================================================

def extract_pdf_text(pdf_bytes):
    """Extract text from a PDF using pypdf."""
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text.strip()
    except Exception as e:
        print(f"      PDF extraction error: {e}")
        return ""


def fetch_nbcourts_decisions(since_date):
    """
    Scrape the NB Courts website Court of Appeal decisions pages
    for the current and previous month(s), returning new decisions
    published after since_date as a list of dicts.

    Returns list of:
      { title, citation, url, pdf_url, date_str, lang, source }
    """
    since_dt = datetime.strptime(since_date, "%Y-%m-%d")
    today    = datetime.now()

    # Build list of (year, month) tuples to check — current month + any prior
    # months still within the lookback window
    months_to_check = set()
    cursor = today.replace(day=1)
    while cursor >= since_dt.replace(day=1):
        months_to_check.add((cursor.year, cursor.month))
        # Go back one month
        if cursor.month == 1:
            cursor = cursor.replace(year=cursor.year - 1, month=12)
        else:
            cursor = cursor.replace(month=cursor.month - 1)

    decisions = []
    seen_pdfs = set()

    for year, month in sorted(months_to_check):
        month_en = MONTH_NAMES_EN[month]
        url_en   = NBCOURTS_BASE + NBCOURTS_CA_EN.format(year=year, month=month_en)

        print(f"   NB Courts — scraping Court of Appeal {month_en.capitalize()} {year} ...")
        try:
            resp = requests.get(url_en, timeout=20)
            if resp.status_code != 200:
                print(f"      HTTP {resp.status_code} — skipping")
                continue

            html = resp.text

            # Find all PDF links on the page — each is one decision
            # Pattern: href="...pdf"  with title and citation in the link text
            pdf_links = re.findall(
                r'<a href="([^"]+\.pdf)"[^>]*>([^<]+)</a>',
                html, re.IGNORECASE
            )

            for pdf_path, link_text in pdf_links:
                link_text = link_text.strip()
                if not link_text:
                    continue

                # Build full PDF URL
                if pdf_path.startswith("http"):
                    pdf_url = pdf_path
                else:
                    pdf_url = NBCOURTS_BASE + pdf_path

                if pdf_url in seen_pdfs:
                    continue
                seen_pdfs.add(pdf_url)

                # Extract citation from link text
                # e.g. "Sheppard v. R., 2026 NBCA 23 - 45-25-CA - Judgment"
                citation_match = re.search(r'(\d{4}\s+NBCA\s+\d+)', link_text)
                citation = citation_match.group(1) if citation_match else ""

                # Extract case name (everything before the citation)
                if citation:
                    title = link_text[:link_text.find(citation)].strip().rstrip(",- ")
                else:
                    title = link_text

                # Try to extract date from surrounding HTML
                # The page has bold headings like <strong>Thursday - March 12</strong>
                # Find the date heading that precedes this link
                date_str = ""
                link_pos = html.find(f'href="{pdf_path}"')
                if link_pos == -1:
                    link_pos = html.find(pdf_path)
                if link_pos > 0:
                    # Search backwards for a date heading
                    preceding = html[:link_pos]
                    date_matches = re.findall(
                        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
                        r'\s*[-–]\s*(\w+\s+\d+)',
                        preceding
                    )
                    if date_matches:
                        raw_date = date_matches[-1].strip()  # most recent heading before link
                        try:
                            dt = datetime.strptime(f"{raw_date} {year}", "%B %d %Y")
                            date_str = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            date_str = ""

                # Skip if before lookback window
                if date_str:
                    try:
                        decision_dt = datetime.strptime(date_str, "%Y-%m-%d")
                        if decision_dt < since_dt:
                            continue
                    except ValueError:
                        pass

                # Determine language from link text / title
                # French cases typically contain "c." instead of "v." and French words
                lang = "fr" if re.search(r'\bc\.\s+[A-Z]|\bC\.\s+R\.', link_text) else "en"

                decisions.append({
                    "title":    title,
                    "citation": citation,
                    "url":      url_en,        # page URL (for email link)
                    "pdf_url":  pdf_url,
                    "date_str": date_str,
                    "lang":     lang,
                    "source":   "nbcourts",
                })

        except Exception as e:
            print(f"      WARNING: Could not fetch NB Courts page: {e}")

    print(f"   NB Courts — found {len(decisions)} Court of Appeal decision(s) in lookback window")
    return decisions


def fetch_pdf_text(pdf_url):
    """Download a PDF from the NB Courts website and extract its text."""
    resp = requests.get(pdf_url, timeout=30)
    resp.raise_for_status()
    return extract_pdf_text(resp.content)


# ============================================================
# SUMMARIZATION  (Claude call 1 of 2)
# ============================================================

def summarize_case(case_text, case_title, citation, lang="en"):
    """Structured factual summary. Sentencing range is in the Comments section."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    max_chars = 12000
    if len(case_text) > max_chars:
        case_text = case_text[:max_chars] + "\n\n[Texte tronqué / Text truncated]"

    if lang == "fr":
        prompt = f"""Tu assistes un(e) procureur(e) de la Couronne au Nouveau-Brunswick, Canada.
Résume la décision judiciaire suivante. Rédige le résumé EN FRANÇAIS.
Utilise exactement ces rubriques :

**Cause :** {case_title} ({citation})
**Tribunal et date :** [extraire du texte]
**Type de procédure :** [ex. détermination de la peine, mise en liberté provisoire, procès, appel]
**Accusations :** [liste des accusations criminelles]
**Faits essentiels :** [2 à 4 points résumant les faits]
**Questions juridiques :** [les principales questions que le tribunal devait trancher]
**Décision :** [ce que le tribunal a décidé et pourquoi, en langage clair]
**Peine (le cas échéant) :** [emprisonnement, probation, amendes, interdiction d'armes, etc.]
**Pertinence pour la Couronne :** [principes juridiques notables, conclusions relatives à la Charte, règles de preuve, points de procédure utiles à la Couronne]

Si cette décision ne semble pas relever du droit criminel, réponds uniquement : NOT_CRIMINAL

---
TEXTE DE LA DÉCISION :
{case_text}
"""
    else:
        prompt = f"""You are assisting a Crown Prosecutor in New Brunswick, Canada.
Summarize the following criminal law decision in plain language.
Structure your summary using exactly these headings:

**Case:** {case_title} ({citation})
**Court & Date:** [extract from text]
**Type of Proceeding:** [e.g., sentencing, bail hearing, trial, appeal, preliminary inquiry]
**Charges:** [list the criminal charges involved]
**Key Facts:** [2-4 bullet points summarizing the facts]
**Legal Issues:** [the main legal questions the court had to decide]
**Outcome / Decision:** [what the court decided and why, in plain language]
**Sentence (if applicable):** [custodial term, probation, fines, firearms prohibition, etc.]
**Relevance for Crown Prosecutors:** [notable legal principles, Charter findings, evidentiary rulings, or procedural points useful to the Crown]

If this decision does not appear to be a criminal law matter, reply only with: NOT_CRIMINAL

---
CASE TEXT:
{case_text}
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


# ============================================================
# COMMENTARY  (Claude call 2 of 2)
# ============================================================

def analyze_case(case_text, case_title, citation, summary, lang="en"):
    """Analytical commentary from the perspective of a senior NB Crown Prosecutor."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    max_chars = 10000
    if len(case_text) > max_chars:
        case_text = case_text[:max_chars] + "\n\n[Text truncated]"

    if lang == "fr":
        prompt = f"""Tu es un(e) procureur(e) de la Couronne senior au Nouveau-Brunswick, Canada,
avec une expertise approfondie en droit criminel canadien, en jurisprudence du Nouveau-Brunswick
et des provinces atlantiques, ainsi qu'en droit constitutionnel (Charte canadienne des droits et libertés).

Rédige une section de COMMENTAIRES analytiques EN FRANÇAIS pour un(e) procureur(e) de la Couronne au N.-B.

RÈGLES IMPORTANTES :
- Identifie UNIQUEMENT les 2 à 4 points les plus importants que cette décision soulève pour un procureur.
- Ne commente QUE ce qui est réellement notable dans cette décision. Si un aspect est banal ou sans intérêt pratique, ne l'inclus pas.
- N'invente pas de catégories fixes. Utilise un titre court et descriptif pour chaque point, adapté au contenu.
- Sois direct et concis. Chaque point devrait tenir en 2 à 5 phrases.
- Si la décision est purement routinière et sans intérêt particulier pour la pratique, dis-le franchement en une phrase.
- N'utilise PAS de catégories prédéfinies (pas de "Pertinence", "Impact sur le droit", etc. systématiques).
- Exemples de titres possibles selon le cas : "Nouveau précédent contraignant", "Point de vigilance en appel", "Fourchette de peine à retenir", "Impact sur les dossiers en cours", "Argument à anticiper de la défense" — ou tout autre titre pertinent.

---
RÉSUMÉ :
{summary}

---
TEXTE COMPLET :
{case_text}
"""
    else:
        prompt = f"""You are a senior Crown Prosecutor in New Brunswick, Canada,
with deep expertise in Canadian criminal law, NB and Atlantic Canadian case law,
and constitutional law under the Charter.

Write an analytical COMMENTS section for a NB Crown Prosecutor.

IMPORTANT RULES:
- Identify ONLY the 2 to 4 most important points this decision raises for Crown practice.
- Only comment on what is genuinely notable. If an aspect is routine or unremarkable, leave it out entirely.
- Do NOT use fixed categories. Give each point a short, descriptive heading that fits the content.
- Be direct and concise. Each point should be 2 to 5 sentences.
- If the decision is entirely routine with nothing of particular significance, say so plainly in one sentence.
- Do NOT use preset headings like "Relevance", "Impact on NB Law", etc. as a checklist.
- Examples of possible headings depending on the case: "New binding precedent", "Sentencing range to note", "Watch point on appeal", "Impact on pending files", "Anticipate this defence argument" — or whatever heading actually fits.

---
CASE SUMMARY:
{summary}

---
FULL CASE TEXT:
{case_text}
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


# ============================================================
# SUBJECT LINE
# ============================================================

def extract_offence_tags(summaries_by_court):
    offence_map = [
        ("Assault",          ["assault", "voies de fait"]),
        ("Sexual Assault",   ["sexual assault", "agression sexuelle"]),
        ("Murder/Homicide",  ["murder", "manslaughter", "homicide", "meurtre"]),
        ("Robbery/Theft",    ["robbery", "theft", "vol qualifié"]),
        ("Drug Offences",    ["trafficking", "possession", "trafic", "drogue"]),
        ("Impaired Driving", ["impaired", "facultés affaiblies"]),
        ("Fraud",            ["fraud", "fraude"]),
        ("Weapons",          ["weapon", "firearm", "arme"]),
        ("Bail",             ["bail", "mise en liberté"]),
        ("Sentencing",       ["sentencing", "sentence", "détermination de la peine"]),
        ("Appeal",           ["appeal", "appel"]),
        ("Charter",          ["charter", "charte"]),
    ]
    counts = Counter()
    for cases in summaries_by_court.values():
        for case in cases:
            text = (case["title"] + " " + case["summary"]).lower()
            for label, keywords in offence_map:
                if any(kw in text for kw in keywords):
                    counts[label] += 1
                    break
    if not counts:
        return ""
    top   = counts.most_common(3)
    parts = [f"{label} ({n})" for label, n in top]
    return " | " + ", ".join(parts)


# ============================================================
# EMAIL BUILDER
# ============================================================

def render_markdown_bold(text):
    return re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text.replace("\n", "<br>"))


def source_badge(source):
    if source == "nbcourts":
        return ('<span style="background:#fff3cd; color:#7a4a00; font-size:11px; '
                'padding:2px 7px; border-radius:10px; margin-left:6px; '
                'font-weight:bold;">NB Courts</span>')
    return ('<span style="background:#e8eef8; color:#1a3a5c; font-size:11px; '
            'padding:2px 7px; border-radius:10px; margin-left:6px;">CanLII</span>')


def build_email_html(summaries_by_court, since_date):
    today         = datetime.now().strftime("%B %d, %Y")
    since_display = datetime.strptime(since_date, "%Y-%m-%d").strftime("%B %d, %Y")

    toc_items = ""
    for court_name, cases in summaries_by_court.items():
        if cases:
            anchor = re.sub(r"[^a-z0-9\-]", "", court_name.lower().replace(" ", "-"))
            toc_items += (
                f'<li><a href="#{anchor}" style="color:#1a3a5c;">'
                f'{court_name} — {len(cases)} decision{"s" if len(cases)!=1 else ""}'
                f'</a></li>'
            )

    toc_html = ""
    if toc_items:
        toc_html = f"""
        <div style="background:#f0f4f8; border:1px solid #c8d8e8; border-radius:6px;
                    padding:16px 20px; margin:16px 0;">
            <strong style="color:#1a3a5c;">Contenu / Contents</strong>
            <ul style="margin:8px 0 0 0; padding-left:20px; line-height:2.2;">{toc_items}</ul>
        </div>"""

    html = f"""
    <html><body style="font-family:Calibri,Arial,sans-serif; max-width:860px;
                       margin:auto; color:#222; padding:16px;">
    <h1 style="color:#1a3a5c; border-bottom:3px solid #1a3a5c; padding-bottom:10px;">
        ⚖️ Digest du droit criminel — N.-B. / NB Criminal Law Digest
    </h1>
    <p style="color:#555; font-size:14px;">
        Nouvelles décisions / New decisions —
        <strong>{since_display}</strong> au/to <strong>{today}</strong>
    </p>
    <p style="color:#888; font-size:12px;">
        Sources: CanLII API &nbsp;|&nbsp;
        <span style="background:#fff3cd; color:#7a4a00; padding:1px 6px;
              border-radius:8px; font-weight:bold;">NB Courts</span>
        &nbsp;= direct from courtsnb-coursnb.ca (often weeks ahead of CanLII)
    </p>
    {toc_html}
    """

    total = sum(len(v) for v in summaries_by_court.values())

    if total == 0:
        html += """<p style="font-style:italic; color:#666;">
            Aucune nouvelle décision criminelle / No new criminal decisions this period.</p>"""
    else:
        for court_name, cases in summaries_by_court.items():
            if not cases:
                continue
            anchor     = re.sub(r"[^a-z0-9\-]", "", court_name.lower().replace(" ", "-"))
            count      = len(cases)
            is_fr      = "(FR)" in court_name
            lang_badge = (
                '<span style="background:#e8f4e8; color:#1a7a3c; font-size:12px; '
                'padding:2px 8px; border-radius:10px; margin-left:8px;">FR</span>'
                if is_fr else
                '<span style="background:#e8eef8; color:#1a3a5c; font-size:12px; '
                'padding:2px 8px; border-radius:10px; margin-left:8px;">EN</span>'
            )

            html += f"""
            <h2 id="{anchor}" style="color:#1a3a5c; margin-top:44px;
                       border-bottom:1px solid #ccc; padding-bottom:4px;">
                {court_name} {lang_badge}
                <span style="font-weight:normal; font-size:16px; color:#666;">
                    — {count} decision{"s" if count!=1 else ""}
                </span>
            </h2>"""

            for case in cases:
                flag          = "🇫🇷 " if case.get("lang") == "fr" else "🇨🇦 "
                summary_html  = render_markdown_bold(case["summary"])
                comments_html = render_markdown_bold(case.get("comments", ""))

                # Style any bold heading in comments with top margin (works for free-form headings)
                import re as _re
                comments_html = _re.sub(
                    r'<strong>',
                    '<strong style="display:inline-block; margin-top:12px;">',
                    comments_html
                )

                sbadge = source_badge(case.get("source", "canlii"))

                html += f"""
                <div style="background:#f4f8fc; border-left:4px solid #1a3a5c;
                            padding:16px 20px; margin:20px 0; border-radius:4px;">
                    <p style="margin:0 0 12px 0; font-size:15px;">
                        {flag}<a href="{case['url']}"
                           style="color:#1a3a5c; font-weight:bold; text-decoration:none;">
                            {case['title']}
                        </a>
                        <span style="color:#888; font-size:13px; margin-left:8px;">
                            {case['citation']}
                        </span>
                        {sbadge}
                    </p>
                    <div style="font-size:14px; line-height:1.8;">{summary_html}</div>
                    <div style="margin-top:20px; padding-top:16px;
                                border-top:2px solid #1a3a5c;">
                        <div style="font-size:13px; font-weight:bold;
                                    color:{'#1a7a3c' if is_fr else '#1a3a5c'};
                                    letter-spacing:0.5px; margin-bottom:10px;
                                    text-transform:uppercase;">
                            {'💬 Commentaires' if is_fr else '💬 Comments'}
                        </div>
                        <div style="font-size:14px; line-height:1.9;
                                    background:#fff; border-radius:4px; padding:14px 16px;
                                    border-left:3px solid {'#1a7a3c' if is_fr else '#e8a020'};">
                            {comments_html if comments_html else
                             "<em style='color:#999;'>No commentary generated.</em>"}
                        </div>
                    </div>
                </div>"""

    html += f"""
    <hr style="margin-top:44px; border:none; border-top:1px solid #ddd;">
    <p style="color:#aaa; font-size:12px; text-align:center;">
        NB Legal Agent v7 — CanLII API + NB Courts + Claude AI — {today}
    </p>
    </body></html>"""
    return html


# ============================================================
# EMAIL SENDER
# ============================================================

def send_email(html_body, total_cases, offence_tags):
    today   = datetime.now().strftime("%B %d, %Y")
    subject = (
        f"NB Criminal Digest — {today} "
        f"({total_cases} decision{'s' if total_cases!=1 else ''})"
        f"{offence_tags}"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
    print(f"Email sent: {subject}")


# ============================================================
# PROCESS A SINGLE CASE (shared by both pipelines)
# ============================================================

def process_case(text, title, citation, case_url, lang, source, seen_cases, new_seen, unique_id):
    """
    Run Claude summary + analysis on a case and return the result dict,
    or None if the case should be skipped.
    """
    print(f"      → Summarizing ...")
    summary = summarize_case(text, title, citation, lang=lang)

    if summary.startswith("NOT_CRIMINAL"):
        print(f"      → Claude: not criminal. Skipping.")
        new_seen.add(unique_id)
        return None

    print(f"      → Analyzing ...")
    comments = analyze_case(text, title, citation, summary, lang=lang)

    new_seen.add(unique_id)
    return {
        "title":    title,
        "citation": citation,
        "url":      case_url,
        "summary":  summary,
        "comments": comments,
        "lang":     lang,
        "source":   source,
    }


# ============================================================
# MAIN
# ============================================================

def run():
    print(f"\n{'='*60}")
    print(f"NB Legal Agent v7 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    seen_cases, last_run_date = load_state()
    since_date = get_lookback_date(last_run_date)

    summaries_by_court    = {}
    new_seen              = set()
    processed_court_langs = set()

    # ── PIPELINE 1: CanLII ────────────────────────────────────────────────────
    print("\n── Source 1: CanLII API ────────────────────────────────────\n")

    for court in NB_COURTS_CANLII:
        court_id   = court["id"]
        court_name = court["name"]
        lang       = court["lang"]

        key = (court_id, lang)
        if key in processed_court_langs:
            continue
        processed_court_langs.add(key)

        print(f"Fetching: {court_name} ...")
        try:
            cases = fetch_recent_cases_canlii(court_id, lang, since_date)
        except Exception as e:
            print(f"   WARNING: Could not fetch {court_name}: {e}")
            summaries_by_court[court_name] = []
            continue

        court_summaries = []
        skipped         = 0

        for case in cases:
            case_id = case.get("caseId", case.get("databaseId", ""))
            if not case_id:
                continue
            if isinstance(case_id, dict):
                case_id = case_id.get("en") or case_id.get("fr") or str(case_id)

            unique_id = f"canlii/{court_id}/{lang}/{case_id}"
            title     = case.get("title", "Untitled")
            citation  = case.get("citation", "")
            case_url  = f"https://www.canlii.org/{lang}/{court_id}/{case_id}.html"

            if unique_id in seen_cases:
                continue

            topics, _ = fetch_case_metadata(court_id, case_id, lang)
            topic_result = is_criminal_by_topics(topics)
            time.sleep(0.3)

            if topic_result is False:
                print(f"   Skipping (CanLII topics — not criminal): {title}")
                new_seen.add(unique_id)
                skipped += 1
                continue
            elif topic_result is None:
                if not is_criminal_by_keywords(title, citation, lang=lang):
                    print(f"   Skipping (keywords — not criminal): {title}")
                    new_seen.add(unique_id)
                    skipped += 1
                    continue

            print(f"   Processing: {title} ...")
            try:
                text = fetch_case_text_canlii(court_id, case_id, lang)
                time.sleep(0.3)
                result = process_case(
                    text, title, citation, case_url, lang, "canlii",
                    seen_cases, new_seen, unique_id
                )
                if result:
                    court_summaries.append(result)
                else:
                    skipped += 1
            except Exception as e:
                print(f"   WARNING: Could not process {title}: {e}")

        summaries_by_court[court_name] = court_summaries
        print(f"   {len(court_summaries)} included, {skipped} skipped\n")

    # ── PIPELINE 2: NB Courts Website — Court of Appeal ──────────────────────
    print("\n── Source 2: NB Courts Website (Court of Appeal) ───────────\n")

    nbcourts_label = "NB Court of Appeal — NB Courts Website"
    nbcourts_cases = []
    skipped_nbcourts = 0

    try:
        decisions = fetch_nbcourts_decisions(since_date)

        # Track citations already seen from CanLII to avoid duplicates
        canlii_citations = set()
        for cases in summaries_by_court.values():
            for c in cases:
                if c.get("citation"):
                    canlii_citations.add(c["citation"].strip())

        for dec in decisions:
            title    = dec["title"]
            citation = dec["citation"]
            pdf_url  = dec["pdf_url"]
            lang     = dec["lang"]
            case_url = dec["url"]

            # Skip if already captured from CanLII by citation
            if citation and citation in canlii_citations:
                print(f"   Skipping (already in CanLII): {title} {citation}")
                skipped_nbcourts += 1
                continue

            # Unique ID based on PDF URL (stable identifier for NB Courts decisions)
            unique_id = f"nbcourts/{re.sub(r'[^a-z0-9]', '_', pdf_url.lower())}"

            if unique_id in seen_cases:
                continue

            # For NB Courts website decisions we skip the keyword filter entirely.
            # The CA decisions page only lists Court of Appeal judgments — every one
            # is worth Claude screening. The keyword filter was designed for CanLII
            # where hundreds of mixed civil/criminal cases arrive together. Here it
            # was incorrectly rejecting cases like "Legal Aid Commission v. R." because
            # the title has "v. R." (Crown as respondent) not "R. v." (Crown prosecuting).
            # Claude will flag non-criminal cases with NOT_CRIMINAL as usual.

            print(f"   Processing: {title} {citation} ...")
            try:
                text = fetch_pdf_text(pdf_url)
                time.sleep(0.5)

                if not text or len(text) < 200:
                    print(f"      WARNING: PDF too short or empty — skipping")
                    skipped_nbcourts += 1
                    continue

                result = process_case(
                    text, title, citation, case_url, lang, "nbcourts",
                    seen_cases, new_seen, unique_id
                )
                if result:
                    # Use the PDF URL as the clickable link so they can read the full decision
                    result["url"] = pdf_url
                    nbcourts_cases.append(result)
                else:
                    skipped_nbcourts += 1

            except Exception as e:
                print(f"   WARNING: Could not process {title}: {e}")

    except Exception as e:
        print(f"   WARNING: NB Courts pipeline failed: {e}")

    summaries_by_court[nbcourts_label] = nbcourts_cases
    print(f"   {len(nbcourts_cases)} included, {skipped_nbcourts} skipped\n")

    # ── EMAIL ─────────────────────────────────────────────────────────────────
    total        = sum(len(v) for v in summaries_by_court.values())
    offence_tags = extract_offence_tags(summaries_by_court)

    print(f"Building email — {total} case(s) total ...")
    html_body = build_email_html(summaries_by_court, since_date)
    send_email(html_body, total, offence_tags)

    today_str = datetime.now().strftime("%Y-%m-%d")
    save_state(seen_cases | new_seen, today_str)
    print(f"Done. Next run looks back from {today_str}.")


if __name__ == "__main__":
    # Uncomment if running daily and want Friday-only delivery:
    # if datetime.now().weekday() != 4:
    #     print("Not Friday — skipping.")
    # else:
    #     run()
    run()
