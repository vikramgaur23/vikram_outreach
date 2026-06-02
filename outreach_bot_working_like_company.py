#!/usr/bin/env python3
"""
Vikram Gaur — Automated Event Outreach Bot
==========================================
Finds Bengaluru tech events, drafts personalised outreach emails
via Groq/LLaMA, and sends them through the Gmail API.

Usage:
    python outreach_bot.py

Environment variables (set in .env or shell):
    GROQ_API_KEY         — Groq API key
    GMAIL_CREDS_PATH     — Path to Google OAuth credentials JSON
    GMAIL_TOKEN_PATH     — Path to cached Gmail token JSON
    SENT_LOG_PATH        — Path to sent-log JSON  (optional)
    LOG_PATH             — Path to log file        (optional)
"""

# ── stdlib ───────────────────────────────────────────────
import os, json, time, base64, logging, random, re, sys
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# ── third-party ──────────────────────────────────────────
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── optional: load .env automatically ───────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed – fall back to OS env


# ═══════════════════════════════════════════════════════════
# CONFIG  (all secrets from environment, never hard-coded)
# ═══════════════════════════════════════════════════════════

def _require(var: str) -> str:
    val = os.getenv(var, "").strip()
    if not val:
        sys.exit(f"[FATAL] Environment variable '{var}' is not set. Exiting.")
    return val


GROQ_API_KEY     = _require("GROQ_API_KEY")
CREDENTIALS_PATH = _require("GMAIL_CREDS_PATH")
TOKEN_PATH       = _require("GMAIL_TOKEN_PATH")
SENT_LOG_PATH    = os.getenv("SENT_LOG_PATH",  "vikram_sent_log.json")
LOG_PATH         = os.getenv("LOG_PATH",        "outreach_bot.log")

YOUR_NAME  = "Vikram Gaur"
YOUR_EMAIL = "gaur.vikram0023@gmail.com"

RUN_HOURS       = 3
TARGET_EMAILS   = 500
MIN_DELAY       = 45          # seconds between sends (avoid spam filters)
MAX_DELAY       = 120
LEADS_PER_BATCH = 4

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

BLOCKED_PREFIXES = ("info@", "support@", "hello@", "contact@",
                    "noreply@", "no-reply@", "admin@", "team@")

GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"


# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# SENT LOG
# ═══════════════════════════════════════════════════════════

def load_sent_log() -> dict:
    path = Path(SENT_LOG_PATH)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("Sent log corrupted — starting fresh.")
    return {}


def save_sent_log(data: dict) -> None:
    Path(SENT_LOG_PATH).write_text(json.dumps(data, indent=2))


def already_pitched(email: str, sent_log: dict) -> bool:
    return email.lower().strip() in sent_log


# ═══════════════════════════════════════════════════════════
# EMAIL UTILITIES
# ═══════════════════════════════════════════════════════════

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(raw: str) -> str:
    """Normalize and lightly validate an email address."""
    email = raw.strip().replace(" ", "").replace("@.", "@").lower()
    return email if _EMAIL_RE.match(email) else ""


def is_blocked(email: str) -> bool:
    return any(email.startswith(p) for p in BLOCKED_PREFIXES)


# ═══════════════════════════════════════════════════════════
# GMAIL AUTH
# ═══════════════════════════════════════════════════════════

def get_gmail_service():
    creds = None
    token = Path(TOKEN_PATH)

    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ═══════════════════════════════════════════════════════════
# GROQ API
# ═══════════════════════════════════════════════════════════

def groq_chat(system_prompt: str, user_prompt: str) -> str | None:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.72,
        "max_tokens": 1400,
    }

    for attempt in range(5):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=90)
            if r.status_code == 429:
                wait = (attempt + 1) * 25
                log.warning(f"Rate limited — sleeping {wait}s …")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.error(f"Groq attempt {attempt+1} failed: {exc}")
            time.sleep(15)

    log.error("All Groq retries exhausted.")
    return None


# ═══════════════════════════════════════════════════════════
# JSON PARSER  (robust against markdown fences)
# ═══════════════════════════════════════════════════════════

_CURLY_QUOTES = str.maketrans('""''', '""\'\'')

def safe_parse_json(text: str) -> dict | None:
    try:
        cleaned = (
            text.strip()
                .replace("```json", "")
                .replace("```", "")
                .translate(_CURLY_QUOTES)
                .strip()
        )
        # strip control chars except \n\t
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            candidate = re.sub(r"\s+", " ", m.group())
            return json.loads(candidate)
    except Exception as exc:
        log.error(f"JSON parse failed: {exc}")
    return None


# ═══════════════════════════════════════════════════════════
# LEAD FINDER
# ═══════════════════════════════════════════════════════════

def find_brands_to_pitch() -> list[dict]:
    today = datetime.now().strftime("%B %d, %Y")

    system = f"""
You are an elite Bengaluru startup-ecosystem researcher.

Your task: identify {LEADS_PER_BATCH} REAL, upcoming Bengaluru tech events
happening within the next 60 days, and return a SPECIFIC, REAL contact
(partnerships / community / devrel / ecosystem / developer-marketing role)
with their work email.

Rules:
- Contacts must be REAL people with REAL public work emails.
- DO NOT use generic addresses: info@, support@, hello@, contact@,
  noreply@, team@, admin@.
- DO NOT hallucinate people or events. If unsure, omit.
- Focus on: AI, GenAI, cloud, SaaS, startup, hackathon, demo-day events.
- Priority organisations: Airtribe, Pine Labs, Razorpay, Inc42,
  YourStory, AWS India, Scaler, Microsoft India, Google Cloud India.

Return ONLY valid JSON — no prose, no markdown fences:

{{
  "events": [
    {{
      "event_name": "",
      "event_type": "",
      "date": "",
      "venue": "",
      "company": "",
      "themes": [],
      "contact_name": "",
      "contact_role": "",
      "contact_email": "",
      "reason": "",
      "hook": ""
    }}
  ]
}}
"""

    user = f"Today is {today}. Find exactly {LEADS_PER_BATCH} high-quality leads."

    raw = groq_chat(system, user)
    if not raw:
        return []

    log.debug(f"Raw leads response:\n{raw}")

    data = safe_parse_json(raw)
    if not data:
        return []

    leads = []
    for ev in data.get("events", []):
        email = clean_email(ev.get("contact_email", ""))
        if not email or is_blocked(email):
            continue
        leads.append({
            "name":         ev.get("event_name", ""),
            "event_type":   ev.get("event_type", ""),
            "date":         ev.get("date", ""),
            "venue":        ev.get("venue", ""),
            "company":      ev.get("company", ""),
            "themes":       ev.get("themes", []),
            "contact_name": ev.get("contact_name", ""),
            "contact_role": ev.get("contact_role", ""),
            "contact_email": email,
            "reason":       ev.get("reason", ""),
            "hook":         ev.get("hook", ""),
        })

    log.info(f"Found {len(leads)} valid leads this batch.")
    return leads


# ═══════════════════════════════════════════════════════════
# EMAIL DRAFTER
# ═══════════════════════════════════════════════════════════

def draft_email(brand: dict) -> tuple[str | None, str | None]:
    themes = ", ".join(brand.get("themes", []))

    system = """
You are writing a premium creator-collaboration outreach email on behalf
of Vikram Gaur (the SENDER, never the recipient).

Goal: secure a creator collaboration, media/event invite, or ecosystem
partnership.

Tone: warm, founder-level, intelligent, thoughtful. Never spammy.

Hard rules:
- NEVER open with "Dear Vikram" or address Vikram in the body.
- NEVER use placeholder text like [Your Name] or [Company].
- Address the recipient by their first name.
- Do NOT add a sign-off — the signature block is added automatically.
- Keep paragraphs concise: 3–5 sentences each.

Return in EXACTLY this format (no extra text before SUBJECT:):

SUBJECT:
<subject line here>

BODY:
<email body here>
"""

    user = f"""
Write a personalised outreach email FROM Vikram Gaur.

Recipient name:  {brand['contact_name']}
Recipient role:  {brand['contact_role']}
Company:         {brand['company']}

Event:           {brand['name']}
Event type:      {brand['event_type']}
Date:            {brand['date']}
Venue:           {brand['venue']}
Themes:          {themes}

Why relevant:    {brand['reason']}
Opening hook:    {brand['hook']}

About Vikram:
• AI / Senior Software Engineer based in Bengaluru
• LinkedIn Top Voice | 152 K+ followers
• Covers AI, GenAI, Cloud, DevOps, startups, and builder culture
• Past brand collabs: NVIDIA, Google Cloud, Amazon, AMD, Dell, ASUS,
  Razorpay, Samsung, Dassault Systèmes, Pine Labs, DBS Bank,
  Intuit, Cashfree

Aim for 3–4 focused paragraphs. Make it feel handcrafted, not templated.
"""

    raw = groq_chat(system, user)
    if not raw:
        return None, None

    subj_m = re.search(r"SUBJECT:(.*?)BODY:", raw, re.DOTALL | re.IGNORECASE)
    body_m = re.search(r"BODY:(.*)",          raw, re.DOTALL | re.IGNORECASE)

    if not subj_m or not body_m:
        log.error("Could not parse SUBJECT/BODY from Groq response.")
        return None, None

    subject = subj_m.group(1).strip()
    body    = body_m.group(1).strip()

    # Safety cleanup
    body = re.sub(r"(?i)dear vikram,?",   "",  body)
    body = re.sub(r"\[Your Name\]",        "",  body)
    body = re.sub(r"\n{3,}",            "\n\n", body)
    body = body.strip()

    return subject, body


# ═══════════════════════════════════════════════════════════
# EMAIL RENDERER  (clean, professional HTML)
# ═══════════════════════════════════════════════════════════

def render_html_email(plain_body: str) -> str:
    """
    Converts the plain-text email body into a polished HTML email.
    Uses inline styles for maximum email-client compatibility.
    """

    # ── convert paragraphs ──────────────────────────────
    paragraphs_html = ""
    for line in plain_body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("•") or line.startswith("-"):
            paragraphs_html += (
                f'<p style="margin:0 0 8px 20px;line-height:1.75;color:#1a1a1a;">'
                f'{line}</p>'
            )
        else:
            paragraphs_html += (
                f'<p style="margin:0 0 16px 0;line-height:1.75;color:#1a1a1a;">'
                f'{line}</p>'
            )

    # ── signature ───────────────────────────────────────
    signature_html = """
<table role="presentation" cellpadding="0" cellspacing="0"
       style="margin-top:28px;border-top:2px solid #0a66c2;padding-top:18px;
              font-family:Arial,Helvetica,sans-serif;font-size:13px;
              color:#1a1a1a;line-height:1.6;max-width:420px;">
  <tr>
    <td>
      <p style="margin:0 0 2px 0;font-size:16px;font-weight:700;
                color:#0a66c2;letter-spacing:0.3px;">Vikram Gaur</p>

      <p style="margin:0 0 10px 0;font-size:12px;color:#555;
                font-style:italic;">
        AI &amp; Senior Software Engineer &nbsp;|&nbsp; Bengaluru, India
      </p>

      <p style="margin:0 0 4px 0;">
        🏆 <strong>LinkedIn Top Voice</strong> &nbsp;·&nbsp;
        40 Under 40 &nbsp;·&nbsp; Times Square Featured
      </p>

      <p style="margin:0 0 4px 0;">
        👥 <strong>152 K+</strong> followers &nbsp;·&nbsp;
        India's Top 50 LinkedIn Creator
      </p>

      <p style="margin:0 0 4px 0;">
        🎤 Speaker &nbsp;·&nbsp; Mentor &nbsp;·&nbsp; Topmate Rising Star
        &nbsp;·&nbsp; GirlScript Ireland
      </p>

      <p style="margin:0 0 4px 0;">
        💼 AI for Businesses &nbsp;|&nbsp; Data &amp; GenAI B2B SaaS
      </p>

      <p style="margin:12px 0 4px 0;font-size:12px;">
        📧 <a href="mailto:gaur.vikram0023@gmail.com"
              style="color:#0a66c2;text-decoration:none;">
          gaur.vikram0023@gmail.com</a>
      </p>

      <p style="margin:0 0 4px 0;font-size:12px;">
        📞 +91 77708 08111 &nbsp;|&nbsp; +91 99266 38518
      </p>

      <p style="margin:8px 0 0 0;font-size:12px;">
        🔗 <a href="https://www.linkedin.com/in/vikram-gaur-0252aa185"
              style="color:#0a66c2;text-decoration:none;">
          linkedin.com/in/vikram-gaur-0252aa185</a>
      </p>
    </td>
  </tr>
</table>
"""

    # ── outer wrapper ────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Email</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f4;
             font-family:Arial,Helvetica,sans-serif;">

  <!-- Outer container -->
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#f4f4f4;padding:30px 0;">
    <tr>
      <td align="center">

        <!-- Card -->
        <table role="presentation" width="640" cellpadding="0" cellspacing="0"
               style="max-width:640px;width:100%;background:#ffffff;
                      border-radius:8px;overflow:hidden;
                      box-shadow:0 2px 12px rgba(0,0,0,0.08);">

          <!-- Top accent bar -->
          <tr>
            <td style="background:linear-gradient(135deg,#0a66c2 0%,#0d8ecf 100%);
                       height:5px;font-size:0;line-height:0;">&nbsp;</td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px 20px 40px;
                       font-size:15px;line-height:1.75;color:#1a1a1a;">
              {paragraphs_html}
              {signature_html}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f9f9f9;padding:14px 40px;
                       font-size:11px;color:#999;
                       border-top:1px solid #e8e8e8;text-align:center;">
              You are receiving this email because your event / organisation
              aligns with Vikram's creator focus areas.
              This is a direct, personalised outreach — not a bulk campaign.
            </td>
          </tr>

        </table>
        <!-- /Card -->

      </td>
    </tr>
  </table>

</body>
</html>"""

    return html


# ═══════════════════════════════════════════════════════════
# SEND EMAIL
# ═══════════════════════════════════════════════════════════

def send_email(
    service,
    to_email: str,
    to_name: str,
    subject: str,
    plain_body: str,
    html_body: str,
) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{YOUR_NAME} <{YOUR_EMAIL}>"
    msg["To"]      = f"{to_name} <{to_email}>" if to_name else to_email

    # Plain-text fallback (strip HTML tags from html_body for safety)
    plain_fallback = re.sub(r"<[^>]+>", "", plain_body).strip()
    plain_fallback += (
        f"\n\n---\nVikram Gaur\n"
        f"gaur.vikram0023@gmail.com\n"
        f"+91 77708 08111 | +91 99266 38518\n"
        f"https://www.linkedin.com/in/vikram-gaur-0252aa185"
    )

    msg.attach(MIMEText(plain_fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,      "html",  "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        return True
    except HttpError as exc:
        log.error(f"Gmail API error sending to {to_email}: {exc}")
    except Exception as exc:
        log.error(f"Unexpected send error to {to_email}: {exc}")
    return False


# ═══════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════

def continuous_outreach() -> None:
    start_time = time.time()
    gmail      = get_gmail_service()
    total_sent = 0

    log.info(f"Session start — target {TARGET_EMAILS} emails over {RUN_HOURS} h.")

    while True:
        elapsed_hours = (time.time() - start_time) / 3600
        if elapsed_hours >= RUN_HOURS:
            log.info(f"Time limit reached ({RUN_HOURS} h). Session complete.")
            break
        if total_sent >= TARGET_EMAILS:
            log.info(f"Email target reached ({TARGET_EMAILS}). Session complete.")
            break

        sent_log = load_sent_log()

        log.info("Fetching fresh leads …")
        leads = find_brands_to_pitch()

        if not leads:
            log.warning("No valid leads found. Retrying in 5 min …")
            time.sleep(300)
            continue

        for brand in leads:
            if total_sent >= TARGET_EMAILS:
                break

            email = brand["contact_email"]
            if already_pitched(email, sent_log):
                log.info(f"Already pitched {email} — skipping.")
                continue

            log.info(f"Drafting email for '{brand['name']}' → {email}")
            subject, plain_body = draft_email(brand)

            if not subject or not plain_body:
                log.warning(f"Draft failed for {brand['name']} — skipping.")
                continue

            html_body = render_html_email(plain_body)

            success = send_email(
                gmail,
                email,
                brand.get("contact_name", ""),
                subject,
                plain_body,
                html_body,
            )

            if success:
                total_sent += 1
                log.info(f"✓ Sent #{total_sent:>3}  →  {email}  |  {subject!r}")

                sent_log[email] = {
                    "event":    brand["name"],
                    "subject":  subject,
                    "sent_at":  datetime.now().isoformat(),
                }
                save_sent_log(sent_log)

                delay = random.randint(MIN_DELAY, MAX_DELAY)
                log.info(f"Sleeping {delay}s before next send …")
                time.sleep(delay)
            else:
                log.warning(f"✗ Failed to send to {email}.")


# ═══════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("═" * 60)
    log.info("  Vikram Gaur — Outreach Bot  |  starting up")
    log.info("═" * 60)
    continuous_outreach()
    log.info("Bot session finished.")
