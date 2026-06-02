#!/usr/bin/env python3

import os
import json
import time
import base64
import logging
import requests
import random
import re

from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# =========================================================
# CONFIG
# =========================================================

GROQ_API_KEY = "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"

CREDENTIALS_PATH = "/Users/vikramgaur/Desktop/credentials.json"
TOKEN_PATH = "/Users/vikramgaur/Desktop/gmail_token.json"
SENT_LOG_PATH = "/Users/vikramgaur/Desktop/vikram_sent_log.json"

YOUR_NAME = "Vikram Gaur"
YOUR_EMAIL = "gaur.vikram0023@gmail.com"

RUN_HOURS = 3
TARGET_EMAILS = 500

MIN_DELAY = 45
MAX_DELAY = 120

LEADS_PER_BATCH = 4

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send"
]

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(
            "/Users/vikramgaur/Desktop/outreach_bot.log"
        ),
        logging.StreamHandler()
    ]
)

log = logging.getLogger(__name__)

# =========================================================
# SENT LOG
# =========================================================

def load_sent_log():

    if os.path.exists(SENT_LOG_PATH):

        with open(SENT_LOG_PATH) as f:
            return json.load(f)

    return {}

def save_sent_log(log_data):

    with open(SENT_LOG_PATH, "w") as f:
        json.dump(log_data, f, indent=2)

def already_pitched(email, sent_log):

    return email.lower().strip() in sent_log

# =========================================================
# EMAIL CLEANER
# =========================================================

def clean_email(email):

    email = email.strip()

    email = email.replace(" ", "")

    email = email.replace("@.", "@")

    return email

# =========================================================
# GMAIL AUTH
# =========================================================

def get_gmail_service():

    creds = None

    if os.path.exists(TOKEN_PATH):

        creds = Credentials.from_authorized_user_file(
            TOKEN_PATH,
            GMAIL_SCOPES
        )

    if not creds or not creds.valid:

        if creds and creds.expired and creds.refresh_token:

            creds.refresh(Request())

        else:

            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH,
                GMAIL_SCOPES
            )

            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build(
        "gmail",
        "v1",
        credentials=creds
    )

# =========================================================
# GROQ
# =========================================================

def groq_chat(system_prompt, user_prompt):

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ],
        "temperature": 0.7,
        "max_tokens": 1200
    }

    for attempt in range(5):

        try:

            log.info(
                "Using model: llama-3.1-8b-instant"
            )

            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=90
            )

            if r.status_code == 429:

                wait_time = (
                    attempt + 1
                ) * 25

                log.warning(
                    f"Rate limited. Sleeping {wait_time}s"
                )

                time.sleep(wait_time)

                continue

            r.raise_for_status()

            return r.json()["choices"][0]["message"]["content"]

        except Exception as e:

            log.error(
                f"Groq API error: {e}"
            )

            time.sleep(15)

    return None

# =========================================================
# SAFE JSON PARSER
# =========================================================

def safe_parse_json(text):

    try:

        text = text.strip()

        text = text.replace(
            "```json",
            ""
        )

        text = text.replace(
            "```",
            ""
        )

        text = text.strip()

        text = re.sub(
            r'[\x00-\x1F\x7F]',
            '',
            text
        )

        text = text.replace(
            "“",
            '"'
        )

        text = text.replace(
            "”",
            '"'
        )

        try:
            return json.loads(text)
        except:
            pass

        match = re.search(
            r'\{.*\}',
            text,
            re.DOTALL
        )

        if match:

            candidate = match.group()

            candidate = candidate.replace(
                '\n',
                ' '
            )

            candidate = re.sub(
                r'\s+',
                ' ',
                candidate
            )

            return json.loads(candidate)

        return None

    except Exception as e:

        log.error(
            f"JSON parse failed: {e}"
        )

        return None

# =========================================================
# FIND LEADS
# =========================================================

def find_brands_to_pitch():

    today = datetime.now().strftime(
        "%B %d, %Y"
    )

    system = """
You are an elite Bengaluru startup ecosystem researcher.

Find REAL upcoming Bengaluru ecosystem opportunities happening within next 60 days.

Focus on:
- AI events
- GenAI conferences
- startup demo days
- hackathons
- cloud summits
- developer events
- startup networking
- SaaS launches
- VC events
- AI builders

VERY IMPORTANT:

Find REAL PEOPLE and PUBLIC WORK EMAILS.

Target:
- partnerships
- ecosystem
- startup marketing
- community
- developer marketing
- creator partnerships
- devrel
- founder office

Avoid:
- info@
- support@
- hello@
- contact@

Return ONLY valid JSON.

FORMAT:

{
  "events": [
    {
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
    }
  ]
}
"""

    user = f"""
Today is {today}.

Find ONLY {LEADS_PER_BATCH} HIGH QUALITY Bengaluru opportunities happening within next 60 days.

Prioritize:
- Airtribe
- Pine Labs
- Razorpay
- Inc42
- YourStory
- AWS
- Scaler
- AI startups
- developer ecosystems

Return ONLY valid JSON.
"""

    response = groq_chat(
        system,
        user
    )

    if not response:
        return []

    print("\nRAW RESPONSE:\n")
    print(response)

    data = safe_parse_json(response)

    if not data:
        return []

    events = data.get(
        "events",
        []
    )

    parsed = []

    for event in events:

        email = clean_email(
            event.get(
                "contact_email",
                ""
            )
        )

        if (
            not email or
            "@" not in email
        ):
            continue

        blocked = [
            "info@",
            "support@",
            "hello@",
            "contact@"
        ]

        if any(
            b in email.lower()
            for b in blocked
        ):
            continue

        parsed.append({
            "name": event.get(
                "event_name",
                ""
            ),
            "event_type": event.get(
                "event_type",
                ""
            ),
            "date": event.get(
                "date",
                ""
            ),
            "venue": event.get(
                "venue",
                ""
            ),
            "company": event.get(
                "company",
                ""
            ),
            "themes": event.get(
                "themes",
                []
            ),
            "contact_name": event.get(
                "contact_name",
                ""
            ),
            "contact_role": event.get(
                "contact_role",
                ""
            ),
            "contact_email": email,
            "reason": event.get(
                "reason",
                ""
            ),
            "hook": event.get(
                "hook",
                ""
            )
        })

    return parsed

# =========================================================
# EMAIL DRAFTING
# =========================================================

def draft_email(brand):

    themes = ", ".join(
        brand.get(
            "themes",
            []
        )
    )

    system = """
You are writing a premium creator collaboration outreach email ON BEHALF OF Vikram Gaur.

IMPORTANT:
Vikram is the sender.

The goal:
- creator collaboration
- event invite
- media invite
- LinkedIn amplification
- ecosystem partnership

STYLE:
- thoughtful
- premium
- ecosystem aware
- founder level
- intelligent
- warm
- handcrafted

STRICT RULES:
- NEVER write Dear Vikram
- NEVER use placeholders
- NEVER use [Your Name]
- NEVER impersonate the company
- NEVER sound spammy

Return EXACTLY in this format:

SUBJECT:
your subject

BODY:
your email body
"""

    user = f"""
Write a highly personalized creator collaboration email FROM Vikram Gaur.

EVENT:
{brand['name']}

EVENT TYPE:
{brand.get('event_type', '')}

DATE:
{brand.get('date', '')}

VENUE:
{brand.get('venue', '')}

COMPANY:
{brand.get('company', '')}

CONTACT:
{brand.get('contact_name', '')}

ROLE:
{brand.get('contact_role', '')}

THEMES:
{themes}

CONTEXT:
{brand.get('reason', '')}

HOOK:
{brand.get('hook', '')}

ABOUT VIKRAM:

- AI Engineer
- Senior Software Engineer
- LinkedIn Top Voice
- 152K+ followers
- Bengaluru based
- Covers AI, GenAI, Cloud, DevOps, startups, builders

PAST COLLABS:
NVIDIA, Google Cloud, Amazon, AMD,
Dell, ASUS, Razorpay, Samsung,
Dassault Systèmes, Pine Labs,
DBS Bank, Intuit, Cashfree

The email should feel similar to:
- Pine Labs Playground outreach
- AI BuildCon outreach
- Bengaluru Tech Summit outreach
- Razorpay summit outreach

Do NOT make it short.
"""

    response = groq_chat(
        system,
        user
    )

    if not response:
        return None, None

    subject_match = re.search(
        r"SUBJECT:(.*?)BODY:",
        response,
        re.DOTALL
    )

    body_match = re.search(
        r"BODY:(.*)",
        response,
        re.DOTALL
    )

    if not subject_match or not body_match:

        log.error(
            "Failed to parse email format"
        )

        return None, None

    subject = subject_match.group(1).strip()

    body = body_match.group(1).strip()

    body = re.sub(
        r"Dear Vikram,?",
        "",
        body,
        flags=re.IGNORECASE
    )

    body = re.sub(
        r"\[Your Name\]",
        "",
        body
    )

    body = re.sub(
        r"\n{3,}",
        "\n\n",
        body
    )

    paragraphs = body.split("\n")

    formatted = ""

    for p in paragraphs:

        p = p.strip()

        if not p:
            continue

        if p.startswith("•"):

            formatted += f"""
            <p style='margin:0 0 10px 20px;
            line-height:1.7;'>
            {p}
            </p>
            """

        else:

            formatted += f"""
            <p style='margin:0 0 14px 0;
            line-height:1.75;'>
            {p}
            </p>
            """

    signature = f"""
    <br>

    <p style='margin:0;'>Warm regards,</p>

    <p style='margin:8px 0 0 0;'>
    <b>Vikram Gaur</b>
    </p>

    <p style='margin:8px 0 0 0;'>
    𝗟𝗶𝗻𝗸𝗲𝗱𝗜𝗻 𝗧𝗼𝗽 𝗩𝗼𝗶𝗰𝗲 | 𝟰𝟬 𝗨𝗻𝗱𝗲𝗿 𝟰𝟬
    </p>

    <p style='margin:4px 0 0 0;'>
    𝗧𝗶𝗺𝗲𝘀 𝗦𝗾𝘂𝗮𝗿𝗲 𝗙𝗲𝗮𝘁𝘂𝗿𝗲𝗱 | 𝟭𝟱𝟬𝗞+ 𝗙𝗼𝗹𝗹𝗼𝘄𝗲𝗿𝘀
    </p>

    <p style='margin:10px 0 0 0;'>
    AI for Businesses | Data & GenAI B2B SaaS
    </p>

    <p style='margin:10px 0 0 0;'>
    India's Top 50 LinkedIn Creator | Topmate Rising Star
    </p>

    <p style='margin:4px 0 0 0;'>
    Speaker | Mentor | GirlScript Ireland
    </p>

    <p style='margin:12px 0 0 0;'>
    📧 gaur.vikram0023@gmail.com
    </p>

    <p style='margin:4px 0 0 0;'>
    📞 +91 77708 08111 | +91 99266 38518
    </p>

    <p style='margin:10px 0 0 0;'>
    LinkedIn:
    </p>

    <p style='margin:4px 0 0 0;'>
    https://www.linkedin.com/in/vikram-gaur-0252aa185
    </p>
    """

    final_body = formatted + signature

    return subject, final_body

# =========================================================
# SEND EMAIL
# =========================================================

def send_email(
    service,
    to_email,
    to_name,
    subject,
    body
):

    html_body = f"""
    <html>
    <body style="
        font-family: Arial, sans-serif;
        line-height: 1.6;
        color: #111111;
        font-size: 15px;
        max-width: 680px;
        padding: 10px;
    ">

    {body}

    </body>
    </html>
    """

    plain_text = re.sub(
        r'<[^>]+>',
        '',
        body
    )

    msg = MIMEMultipart(
        "alternative"
    )

    msg["Subject"] = subject

    msg["From"] = (
        f"{YOUR_NAME} "
        f"<{YOUR_EMAIL}>"
    )

    if to_name:

        msg["To"] = (
            f"{to_name} "
            f"<{to_email}>"
        )

    else:

        msg["To"] = to_email

    msg.attach(
        MIMEText(
            plain_text,
            "plain"
        )
    )

    msg.attach(
        MIMEText(
            html_body,
            "html"
        )
    )

    raw = base64.urlsafe_b64encode(
        msg.as_bytes()
    ).decode()

    try:

        service.users().messages().send(
            userId="me",
            body={
                "raw": raw
            }
        ).execute()

        return True

    except Exception as e:

        log.error(
            f"Gmail send error: {e}"
        )

        return False

# =========================================================
# MAIN LOOP
# =========================================================

def continuous_outreach():

    start_time = time.time()

    gmail = get_gmail_service()

    total_sent = 0

    while True:

        elapsed_hours = (
            time.time() - start_time
        ) / 3600

        if elapsed_hours >= RUN_HOURS:

            log.info(
                "3-hour outreach completed."
            )

            break

        sent_log = load_sent_log()

        log.info(
            "Finding fresh leads..."
        )

        brands = find_brands_to_pitch()

        print("\nPARSED LEADS:\n")
        print(brands)

        if not brands:

            log.warning(
                "No leads found. Retrying in 5 mins."
            )

            time.sleep(300)

            continue

        for brand in brands:

            if total_sent >= TARGET_EMAILS:
                return

            email = brand.get(
                "contact_email",
                ""
            ).strip()

            if already_pitched(
                email,
                sent_log
            ):
                continue

            log.info(
                f"Drafting for {brand['name']}"
            )

            subject, body = draft_email(
                brand
            )

            if not subject or not body:
                continue

            success = send_email(
                gmail,
                email,
                brand.get(
                    "contact_name",
                    ""
                ),
                subject,
                body
            )

            if success:

                total_sent += 1

                log.info(
                    f"Sent #{total_sent} → {email}"
                )

                sent_log[email] = {
                    "event": brand["name"],
                    "subject": subject,
                    "sent_at": datetime.now().isoformat()
                }

                save_sent_log(sent_log)

                sleep_time = random.randint(
                    MIN_DELAY,
                    MAX_DELAY
                )

                log.info(
                    f"Sleeping {sleep_time}s"
                )

                time.sleep(sleep_time)

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    log.info(
        "Vikram Outreach Bot started"
    )

    log.info(
        f"Running for {RUN_HOURS} hours"
    )

    continuous_outreach()

    log.info(
        "Bot session completed"
    )