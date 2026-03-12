"""
NB Legal Agent v6 - CanLII Criminal Law Case Monitor
New in v6:
  - Compatible with GitHub Actions (free cloud hosting)
  - API keys and email credentials are read from environment variables
    so they are never stored in the script itself (required for GitHub)

All v5 features retained:
  - "Commentaires / Comments" analytical section per case
  - CanLII topic-based criminal law filtering
  - English + French court monitoring
  - Smart lookback window (30 days first run, then from last run date)
  - Rich email subject line with offence types
  - Table of contents in email
"""

import os
import re
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import Counter

# ============================================================
# CONFIGURATION
# Keys are read from environment variables — do NOT hardcode
# them here. On GitHub Actions they come from Secrets.
# For local testing on your laptop, see the guide for how to
# set temporary environment variables in Command Prompt.
# ============================================================

CANLII_API_KEY    = os.environ.get("CANLII_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_SENDER      = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD    = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER    = os.environ.get("EMAIL_RECEIVER", "")

# ============================================================
# COURT DEFINITIONS
# ============================================================

NB_COURTS = [
    {"id": "nbpc", "name": "NB Provincial Court (EN)",          "lang": "en"},
    {"id": "nbkb", "name": "Court of King's Bench (EN)",        "lang": "en"},
    {"id": "nbca", "name": "NB Court of Appeal (EN)",           "lang": "en"},
    {"id": "nbcp", "name": "Cour provinciale du N.-B. (FR)",    "lang": "fr"},
    {"id": "nbbr", "name": "Cour du Banc du Roi du N.-B. (FR)", "lang": "fr"},
    {"id": "nbca", "name": "Cour d'appel du N.-B. (FR)",        "lang": "fr"},
]

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
    if not topics:
        return None
    # CanLII API returns topics as either dicts {"topicId": "..."} or plain strings
    # Handle both formats gracefully
    topic_ids = set()
    for t in topics:
        if isinstance(t, dict):
            topic_ids.add(t.get("topicId", ""))
        elif isinstance(t, str):
            topic_ids.add(t)
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

def fetch_recent_cases(court_id, lang, since_date):
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


def fetch_case_text(court_id, case_id, lang):
    url = (
        f"https://api.canlii.org/v1/caseDocument/{lang}/{court_id}/{case_id}/"
        f"?api_key={CANLII_API_KEY}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("documentContent", data.get("excerpt", ""))


# ============================================================
# SUMMARIZATION  (Call 1 of 2 per case)
# ============================================================

def summarize_case(case_text, case_title, citation, lang="en"):
    """
    First Claude call: structured factual summary of the case.
    Sentencing range is no longer here — it moved to the Comments section.
    """
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
# COMMENTARY  (Call 2 of 2 per case)
# ============================================================

def analyze_case(case_text, case_title, citation, summary, lang="en"):
    """
    Second Claude call: analytical commentary written from the
    perspective of a senior Crown Prosecutor in New Brunswick.

    This is intentionally separate from the summary so Claude can
    focus purely on legal analysis without being constrained by the
    structured summary format. The summary is passed in as context
    so Claude does not need to re-read the full case text for facts.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    max_chars = 10000
    if len(case_text) > max_chars:
        case_text = case_text[:max_chars] + "\n\n[Text truncated]"

    if lang == "fr":
        prompt = f"""Tu es un(e) procureur(e) de la Couronne senior au Nouveau-Brunswick, Canada,
avec une expertise approfondie en droit criminel canadien, en jurisprudence du Nouveau-Brunswick
et des provinces atlantiques, ainsi qu'en droit constitutionnel (Charte canadienne des droits et libertés).

Tu viens de lire la décision suivante et d'en prendre connaissance grâce au résumé ci-dessous.
Rédige maintenant une section de COMMENTAIRES analytiques EN FRANÇAIS, structurée ainsi :

**📌 Pertinence**
Cette décision est-elle pertinente pour la pratique quotidienne d'un procureur de la Couronne au N.-B. ?
Pourquoi, ou pourquoi pas ? S'agit-il d'une décision routinière ou a-t-elle une portée plus large ?

**⚖️ Impact sur le droit au Nouveau-Brunswick**
Cette décision modifie-t-elle, clarifie-t-elle ou confirme-t-elle le droit applicable au N.-B. ?
S'inscrit-elle dans une tendance jurisprudentielle ? A-t-elle une portée limitée au cas d'espèce
ou pourrait-elle servir de précédent ?

**🔄 Conflits et tensions jurisprudentiels**
Cette décision est-elle en opposition ou en tension avec d'autres décisions — que ce soit d'autres
tribunaux du N.-B., d'autres provinces, ou de la Cour suprême du Canada ? Y a-t-il une divergence
entre les provinces sur la question de droit en cause ? Si oui, lesquelles et quel est l'état du droit ?

**🌐 Comparaison avec les autres provinces et les tribunaux supérieurs**
Le droit appliqué dans cette décision est-il le même dans les autres provinces canadiennes ?
Y a-t-il des divergences notables entre provinces ou entre niveaux de juridiction ?
La Cour suprême du Canada s'est-elle prononcée sur cette question ? Si oui, la décision
est-elle conforme à l'arrêt de la Cour suprême ?

**📏 Analyse de la peine (le cas échéant)**
Si la décision porte sur la détermination de la peine :
— Quelle est la fourchette habituelle pour cette infraction au N.-B. et dans les provinces atlantiques ?
— La peine imposée est-elle dans le bas, le milieu ou le haut de cette fourchette ?
— Est-elle remarquablement clémente ou sévère ? Pourquoi le tribunal a-t-il choisi cette peine ?
— Y a-t-il des circonstances aggravantes ou atténuantes significatives à noter ?
Si ce n'est pas une décision sur la peine, écris : N/A

**⚠️ Erreurs judiciaires potentielles et raisonnement questionnable**
À ton avis d'expert, le juge a-t-il commis une erreur de droit ou de fait ?
Le raisonnement juridique est-il solide, ou y a-t-il des lacunes, des contradictions
ou des conclusions qui semblent mal fondées ? Cette décision serait-elle susceptible
d'être infirmée en appel ? Sois direct et honnête dans ton analyse, même si c'est critique.
Si le raisonnement te semble irréprochable, dis-le.

**💡 Points d'action pour la Couronne**
Y a-t-il des éléments que la Couronne devrait surveiller, des arguments à préparer,
des distinctions à noter pour des dossiers similaires, ou des domaines où cette décision
pourrait être invoquée ou distinguée à l'avenir ?

---
RÉSUMÉ DE LA DÉCISION :
{summary}

---
TEXTE COMPLET DE LA DÉCISION :
{case_text}
"""
    else:
        prompt = f"""You are a senior Crown Prosecutor in New Brunswick, Canada,
with deep expertise in Canadian criminal law, New Brunswick and Atlantic Canadian case law,
and constitutional law under the Canadian Charter of Rights and Freedoms.

You have just read the following decision and have the summary below as context.
Write an analytical COMMENTS section structured exactly as follows:

**📌 Relevance**
Is this decision relevant to the day-to-day practice of a Crown Prosecutor in NB?
Why or why not? Is this a routine decision, or does it have broader significance?

**⚖️ Impact on New Brunswick Law**
Does this decision change, clarify, or confirm the law as it applies in NB?
Does it fit within a broader jurisprudential trend? Is its effect limited to the
specific facts, or could it be used as a precedent in future cases?

**🔄 Conflicts and Tensions with Other Decisions**
Is this decision in conflict or tension with other decisions — whether from other NB courts,
other provinces, or the Supreme Court of Canada? Is there a split between provinces on the
legal question at issue? If so, what is the current state of the law?

**🌐 Comparison with Other Provinces and Higher Courts**
Is the law applied in this decision the same across Canadian provinces?
Are there notable differences between provinces or levels of court?
Has the Supreme Court of Canada addressed this issue? If so, is this decision
consistent with what the Supreme Court has said?

**📏 Sentencing Analysis (if applicable)**
If this is a sentencing decision:
— What is the typical sentencing range for this offence in NB and Atlantic Canada?
— Is the sentence imposed at the low end, mid-range, or high end of that range?
— Is it notably lenient or severe? What drove the court's choice?
— Are there significant aggravating or mitigating factors worth noting?
If this is not a sentencing decision, write: N/A

**⚠️ Potential Judicial Errors and Questionable Reasoning**
In your expert opinion, did the judge make an error of law or fact?
Is the legal reasoning sound, or are there gaps, contradictions, or conclusions
that appear poorly grounded? Would this decision be vulnerable on appeal?
Be direct and honest, even if your assessment is critical. If the reasoning
appears sound, say so.

**💡 Action Points for the Crown**
Are there things the Crown should watch for, arguments to prepare, distinctions
to draw in similar cases, or ways this decision might be invoked or distinguished
in future proceedings?

---
CASE SUMMARY (for context):
{summary}

---
FULL CASE TEXT:
{case_text}
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1800,
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
    """Convert **text** to <strong>text</strong> and newlines to <br>."""
    return re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text.replace("\n", "<br>"))


def build_email_html(summaries_by_court, since_date):
    today         = datetime.now().strftime("%B %d, %Y")
    since_display = datetime.strptime(since_date, "%Y-%m-%d").strftime("%B %d, %Y")

    # Table of contents
    toc_items = ""
    for court_name, cases in summaries_by_court.items():
        if cases:
            anchor = re.sub(r"[^a-z0-9\-]", "", court_name.lower().replace(" ", "-"))
            toc_items += (
                f'<li><a href="#{anchor}" style="color:#1a3a5c;">'
                f'{court_name} — {len(cases)} décision{"s" if len(cases) != 1 else ""}'
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
    <html><body style="font-family: Calibri, Arial, sans-serif; max-width: 860px;
                       margin: auto; color: #222; padding: 16px;">
    <h1 style="color:#1a3a5c; border-bottom:3px solid #1a3a5c; padding-bottom:10px;">
        ⚖️ Digest du droit criminel — N.-B. / NB Criminal Law Digest
    </h1>
    <p style="color:#555; font-size:14px;">
        Nouvelles décisions / New decisions — CanLII —
        <strong>{since_display}</strong> au/to <strong>{today}</strong>
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
                    — {count} décision{"s" if count != 1 else ""}
                </span>
            </h2>"""

            for case in cases:
                flag          = "🇫🇷 " if case.get("lang") == "fr" else "🇨🇦 "
                summary_html  = render_markdown_bold(case["summary"])
                comments_html = render_markdown_bold(case.get("comments", ""))

                # Emoji section headers in comments get a subtle highlight
                for emoji in ["📌", "⚖️", "🔄", "🌐", "📏", "⚠️", "💡"]:
                    comments_html = comments_html.replace(
                        f"<strong>{emoji}",
                        f'<strong style="display:inline-block; margin-top:10px;">{emoji}'
                    )

                html += f"""
                <div style="background:#f4f8fc; border-left:4px solid #1a3a5c;
                            padding:16px 20px; margin:20px 0; border-radius:4px;">

                    <!-- Case title -->
                    <p style="margin:0 0 12px 0; font-size:15px;">
                        {flag}<a href="{case['url']}"
                           style="color:#1a3a5c; font-weight:bold; text-decoration:none;">
                            {case['title']}
                        </a>
                        <span style="color:#888; font-size:13px; margin-left:8px;">
                            {case['citation']}
                        </span>
                    </p>

                    <!-- Summary -->
                    <div style="font-size:14px; line-height:1.8;">
                        {summary_html}
                    </div>

                    <!-- Comments section divider -->
                    <div style="margin-top:20px; padding-top:16px;
                                border-top: 2px solid #1a3a5c;">
                        <div style="font-size:13px; font-weight:bold; color:{('#1a7a3c' if is_fr else '#1a3a5c')};
                                    letter-spacing:0.5px; margin-bottom:10px; text-transform:uppercase;">
                            {'💬 Commentaires' if is_fr else '💬 Comments'}
                        </div>
                        <div style="font-size:14px; line-height:1.9;
                                    background:#fff; border-radius:4px;
                                    padding:14px 16px;
                                    border-left: 3px solid {'#1a7a3c' if is_fr else '#e8a020'};">
                            {comments_html if comments_html else
                             "<em style='color:#999;'>No commentary generated.</em>"}
                        </div>
                    </div>

                </div>"""

    html += f"""
    <hr style="margin-top:44px; border:none; border-top:1px solid #ddd;">
    <p style="color:#aaa; font-size:12px; text-align:center;">
        NB Legal Agent v6 — CanLII API + Claude AI — {today}
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
        f"({total_cases} decision{'s' if total_cases != 1 else ''})"
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
# MAIN
# ============================================================

def run():
    print(f"\n{'='*60}")
    print(f"NB Legal Agent v6 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    seen_cases, last_run_date = load_state()
    since_date = get_lookback_date(last_run_date)

    summaries_by_court     = {}
    new_seen               = set()
    processed_court_langs  = set()

    for court in NB_COURTS:
        court_id   = court["id"]
        court_name = court["name"]
        lang       = court["lang"]

        key = (court_id, lang)
        if key in processed_court_langs:
            continue
        processed_court_langs.add(key)

        print(f"Fetching: {court_name} ...")

        try:
            cases = fetch_recent_cases(court_id, lang, since_date)
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

            unique_id = f"{court_id}/{lang}/{case_id}"
            title     = case.get("title", "Untitled")
            citation  = case.get("citation", "")
            case_url  = f"https://www.canlii.org/{lang}/{court_id}/{case_id}.html"

            if unique_id in seen_cases:
                continue

            # ── Filter: CanLII topics then keyword fallback ───────────────────
            topics, _ = fetch_case_metadata(court_id, case_id, lang)
            topic_result = is_criminal_by_topics(topics)
            time.sleep(0.3)

            if topic_result is False:
                print(f"   Skipping (CanLII topics): {title}")
                new_seen.add(unique_id)
                skipped += 1
                continue
            elif topic_result is None:
                if not is_criminal_by_keywords(title, citation, lang=lang):
                    print(f"   Skipping (keywords): {title}")
                    new_seen.add(unique_id)
                    skipped += 1
                    continue

            # ── Fetch full text ───────────────────────────────────────────────
            print(f"   Processing: {title} ...")
            try:
                text = fetch_case_text(court_id, case_id, lang)
                time.sleep(0.3)

                # ── Call 1: Structured summary ────────────────────────────────
                print(f"      → Summarizing ...")
                summary = summarize_case(text, title, citation, lang=lang)

                if summary.startswith("NOT_CRIMINAL"):
                    print(f"      → Claude: not criminal. Skipping.")
                    new_seen.add(unique_id)
                    skipped += 1
                    continue

                # ── Call 2: Analytical commentary ─────────────────────────────
                print(f"      → Analyzing ...")
                comments = analyze_case(text, title, citation, summary, lang=lang)

                court_summaries.append({
                    "title":    title,
                    "citation": citation,
                    "url":      case_url,
                    "summary":  summary,
                    "comments": comments,
                    "lang":     lang,
                })
                new_seen.add(unique_id)

            except Exception as e:
                print(f"   WARNING: Could not process {title}: {e}")

        summaries_by_court[court_name] = court_summaries
        print(f"   {len(court_summaries)} included, {skipped} skipped\n")

    total        = sum(len(v) for v in summaries_by_court.values())
    offence_tags = extract_offence_tags(summaries_by_court)

    print(f"Building email — {total} case(s) ...")
    html_body = build_email_html(summaries_by_court, since_date)
    send_email(html_body, total, offence_tags)

    today_str = datetime.now().strftime("%Y-%m-%d")
    save_state(seen_cases | new_seen, today_str)
    print(f"Done. Next run looks back from {today_str}.")


if __name__ == "__main__":
    # Uncomment the lines below if running daily on PythonAnywhere
    # and you only want emails on Fridays:
    # if datetime.now().weekday() != 4:
    #     print("Not Friday — skipping.")
    # else:
    #     run()
    run()
