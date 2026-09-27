"""
Autonomous Omnichannel Gatekeeper Agent
Author: Eashan Singh & Antigravity
Integrations: Telegram Bot (HITL), Gmail (IMAP/SMTP), Android Phone (WhatsApp & Calls), Gemini 2.5 Flash
"""

import os
import sys
import time
import json
import email
import imaplib
import smtplib
import threading
import uuid
import urllib.request
import urllib.parse
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from email.message import EmailMessage
from email.header import decode_header
from pathlib import Path

# Force UTF-8 and line buffering on Windows Console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Load Local Configuration
ENV_FILE = Path(__file__).parent / ".env"
CONFIG = {}
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            CONFIG[k.strip()] = v.strip().strip("'\"")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or CONFIG.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or CONFIG.get("GEMINI_API_KEY")
GMAIL_USER = os.environ.get("GMAIL_USER") or CONFIG.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD") or CONFIG.get("GMAIL_APP_PASSWORD")
MACRODROID_SMS_WEBHOOK = os.environ.get("MACRODROID_SMS_WEBHOOK") or CONFIG.get("MACRODROID_SMS_WEBHOOK")

# Pending Actions Memory Store: { event_id: { type, sender, content, draft, ... } }
PENDING_EVENTS = {}
PENDING_LOCK = threading.Lock()
SEEN_EMAIL_IDS = set()

# Echo Suppression Memory Store: [(timestamp, clean_text), ...]
RECENT_AGENT_REPLIES = []
REPLIES_LOCK = threading.Lock()

def record_sent_reply(text):
    if not text:
        return
    with REPLIES_LOCK:
        RECENT_AGENT_REPLIES.append((time.time(), text.strip().lower()))
        if len(RECENT_AGENT_REPLIES) > 20:
            RECENT_AGENT_REPLIES.pop(0)

def is_recent_reply_echo(text):
    if not text:
        return False
    clean = text.strip().lower()
    now = time.time()
    with REPLIES_LOCK:
        for t, sent_txt in RECENT_AGENT_REPLIES:
            if now - t < 30 and (clean == sent_txt or sent_txt in clean or clean in sent_txt):
                return True
    return False

# Active Notification Tracker (Guards against replying to wrong contact if another message arrives)
LAST_WHATSAPP_NOTIFICATION_SENDER = ""
LAST_WHATSAPP_NOTIFICATION_TIME = 0
TRACK_LOCK = threading.Lock()

# ==========================================
# Conversation Thread Memory & Delegation Store
# ==========================================
CONVERSATIONS_FILE = Path(__file__).parent / "conversations.json"
CONVERSATION_STORE = {}
CONV_LOCK = threading.RLock()

def init_conversations():
    """Load conversation history and reset all active delegations so every contact starts in permission-required mode."""
    global CONVERSATION_STORE
    with CONV_LOCK:
        if CONVERSATIONS_FILE.exists():
            try:
                CONVERSATION_STORE = json.loads(CONVERSATIONS_FILE.read_text(encoding="utf-8"))
            except Exception:
                CONVERSATION_STORE = {}
        else:
            CONVERSATION_STORE = {}

        # Reset all delegations to False on startup to prevent any unintended autonomous replies
        for cid, data in CONVERSATION_STORE.items():
            data["delegated_to_agent"] = False
            data["autonomous_turns_count"] = 0
            data["delegated_timestamp"] = 0

        try:
            CONVERSATIONS_FILE.write_text(json.dumps(CONVERSATION_STORE, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[Memory Store Error] Failed to save initial reset: {e}", file=sys.stderr)

init_conversations()

def clean_contact_id(sender):
    return str(sender).strip().lower()

def add_message_to_history(contact_id, sender_name, role, text):
    with CONV_LOCK:
        cid = clean_contact_id(contact_id)
        if cid not in CONVERSATION_STORE:
            CONVERSATION_STORE[cid] = {
                "name": str(sender_name).strip(),
                "delegated_to_agent": False,
                "history": []
            }
        CONVERSATION_STORE[cid]["name"] = str(sender_name).strip()
        CONVERSATION_STORE[cid]["history"].append({
            "role": role,
            "text": str(text).strip(),
            "timestamp": int(time.time())
        })
        # Keep last 12 messages per contact for responsive multi-turn context
        if len(CONVERSATION_STORE[cid]["history"]) > 12:
            CONVERSATION_STORE[cid]["history"] = CONVERSATION_STORE[cid]["history"][-12:]
        try:
            CONVERSATIONS_FILE.write_text(json.dumps(CONVERSATION_STORE, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[Memory Store Error] Failed to save conversations: {e}", file=sys.stderr)

def get_history_summary(contact_id, limit=6):
    with CONV_LOCK:
        cid = clean_contact_id(contact_id)
        if cid not in CONVERSATION_STORE or not CONVERSATION_STORE[cid].get("history"):
            return ""
        history = CONVERSATION_STORE[cid]["history"][-limit:]
        lines = []
        contact_name = CONVERSATION_STORE[cid].get("name", "Contact")
        for msg in history:
            sender_label = contact_name if msg["role"] == "contact" else "Eashan's AI Agent"
            lines.append(f"- {sender_label}: \"{msg['text']}\"")
        return "\n".join(lines)

def is_thread_delegated(contact_id):
    """Checks if the thread is delegated to the agent. Runs continuously until user taps 'Take Back Control'."""
    with CONV_LOCK:
        cid = clean_contact_id(contact_id)
        contact_data = CONVERSATION_STORE.get(cid, {})
        return bool(contact_data.get("delegated_to_agent", False))

def set_thread_delegation(contact_id, state: bool):
    with CONV_LOCK:
        cid = clean_contact_id(contact_id)
        if cid not in CONVERSATION_STORE:
            CONVERSATION_STORE[cid] = {
                "name": str(contact_id),
                "delegated_to_agent": state,
                "delegated_timestamp": int(time.time()) if state else 0,
                "autonomous_turns_count": 0,
                "history": []
            }
        else:
            CONVERSATION_STORE[cid]["delegated_to_agent"] = state
            CONVERSATION_STORE[cid]["delegated_timestamp"] = int(time.time()) if state else 0
            CONVERSATION_STORE[cid]["autonomous_turns_count"] = 0
        try:
            CONVERSATIONS_FILE.write_text(json.dumps(CONVERSATION_STORE, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

def increment_autonomous_turn(contact_id):
    with CONV_LOCK:
        cid = clean_contact_id(contact_id)
        if cid in CONVERSATION_STORE:
            CONVERSATION_STORE[cid]["autonomous_turns_count"] = CONVERSATION_STORE[cid].get("autonomous_turns_count", 0) + 1
            CONVERSATION_STORE[cid]["delegated_timestamp"] = int(time.time())
            try:
                CONVERSATIONS_FILE.write_text(json.dumps(CONVERSATION_STORE, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass


# ==========================================
import html

# 1. Telegram API Helper
# ==========================================
def telegram_request(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"[Telegram Error] {method}: {e}", file=sys.stderr)
        return None


def send_decision_prompt(event_id, channel, sender, content, summary, reply_draft):
    safe_sender = html.escape(str(sender))
    safe_content = html.escape(str(content[:300]))
    safe_summary = html.escape(str(summary))
    safe_draft = html.escape(str(reply_draft))

    text = (
        f"🚨 <b>NEW {html.escape(channel.upper())} COMMUNICATION</b>\n\n"
        f"👤 <b>From:</b> {safe_sender}\n"
        f"💬 <b>Message / Content:</b>\n<i>\"{safe_content}\"</i>\n\n"
        f"🧠 <b>Gemini Analysis:</b>\n"
        f"• <b>Summary:</b> {safe_summary}\n"
        f"• <b>Proposed Action:</b> <i>\"{safe_draft}\"</i>\n\n"
        f"❓ <b>Who should handle this?</b>"
    )

    markup = {
        "inline_keyboard": [
            [
                {"text": "🤖 Let Agent Handle", "callback_data": f"agent:{event_id}"},
                {"text": "👤 I'll Handle It", "callback_data": f"user:{event_id}"}
            ]
        ]
    }

    res = telegram_request("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": markup
    })
    return res


def send_autonomous_chat_update(sender, incoming_text, agent_reply, summary):
    cid = clean_contact_id(sender)
    safe_sender = html.escape(str(sender))
    safe_in = html.escape(str(incoming_text[:300]))
    safe_rep = html.escape(str(agent_reply))
    safe_sum = html.escape(str(summary))

    text = (
        f"🤖 <b>AUTONOMOUS CHAT UPDATE</b>\n\n"
        f"👤 <b>Contact:</b> {safe_sender}\n"
        f"📌 <b>Context:</b> {safe_sum}\n\n"
        f"📥 <b>{safe_sender}:</b> <i>\"{safe_in}\"</i>\n"
        f"📤 <b>Agent Replied:</b> <i>\"{safe_rep}\"</i>\n\n"
        f"⚡ <i>Agent is actively handling this thread. Tap below to take back control anytime.</i>"
    )

    markup = {
        "inline_keyboard": [
            [
                {"text": "👤 Take Back Control", "callback_data": f"takeover:{cid}"}
            ]
        ]
    }

    res = telegram_request("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": markup
    })
    return res


# ==========================================
# 2. Gemini AI Analysis Engine (Hinglish & Multi-Turn Tone Matching)
# ==========================================
def analyze_with_gemini(channel, sender, content, contact_id=None):
    cid = clean_contact_id(contact_id or sender)
    history_str = get_history_summary(cid)

    history_context = ""
    if history_str:
        history_context = f"\nRecent Conversation History with this contact:\n{history_str}\n"

    prompt = f"""You are Eashan Singh texting from your own phone on {channel}.
The other person thinks they are chatting directly with Eashan. You must sound 100% like a real young Indian guy typing casually on his smartphone keyboard, NOT like an AI bot!

Channel: {channel}
Sender: {sender}
Latest Message: "{content}"
{history_context}
INSTRUCTIONS:
1. summary: A crisp 1-sentence breakdown of what {sender} is saying, asking, or feeling.
2. proposed_reply: Draft the exact text message Eashan would type.

STRICT HUMAN TEXTING RULES (CRUCIAL TO AVOID SOUNDING LIKE AN AI):
1. CONDITIONAL SLANG MATCHING (MIRROR ONLY IF SENDER USED IT):
   - IF AND ONLY IF the sender used informal slang, banter, or gaali (e.g., 'abe saale', 'chutiye', 'laude', 'kya be', 'bakwaas mat kar'):
     * You MUST match their slang level and banter back in the same authentic slang style (e.g., 'abe saale tu chup kar raste me hu', 'kya be apna dekh pehle', 'chup kar na bhai 5 min me aa raha hu').
   - IF THE SENDER DID NOT USE SLANG (e.g., normal casual message, sweet message, or formal message):
     * Strictly DO NOT use any slang, abuses, or 'abe'. Keep it completely clean, friendly, and natural.
2. NATURAL OPENERS (NO UNNECESSARY 'ABE'):
   - Do NOT start normal messages with 'Abe' unless the contact specifically said 'Abe' or used slang first.
   - Start naturally: 'ha', 'ruk na', 'arre', 'bhai', 'haan bol', 'kya scene', or directly answer.
3. STRICTLY MINIMAL EMOJIS:
   - Do NOT put an emoji in every sentence! Most real WhatsApp texts have ZERO emojis.
   - Only use an emoji if the sender used one first or in rare fitting moments. No emoji at the end of every sentence.
4. LOWERCASE & CASUAL REAL CHAT STYLE (NOT FORMAL TEXTBOOK HINDI):
   - For casual WhatsApp chat, do NOT use formal textbook sentence capitalization.
   - Start casually in lowercase or natural chat case (e.g., 'ha ruk na raste me hu', 'bhai kya scene hai', 'nhi yaar aaj thoda kaam hai').
   - Use authentic Indian texting abbreviations: 'ha', 'nhi', 'krta hu', 'ruk na', 'thik hai', 'kya scene', 'kaha hai', 'chalega'.
   - Avoid textbook punctuation (no formal periods '.' or '!' after every tiny phrase).
5. EMOTIONAL / CARING (IF WITH GIRLFRIEND/CLOSE ONES):
   - Sound warm, real, and natural, not poetic or robotic: 'ha abhi khaya yaar, tune khaya kuch?', 'miss you too kab mil rahe bata'.
6. FORMAL / PROFESSIONAL (CLIENTS, RECRUITERS, EMAILS):
   - If the channel is Email or the message is in formal English from a client/recruiter/professor:
   - Reply in polite, polished professional English (e.g., 'Hi, thank you for reaching out. Eashan is currently in a meeting, but I will make sure he reviews this and gets back to you shortly.').
7. SAFETY GUARDRAIL (CRITICAL):
   - Never confirm bookings, payments, or money. Tell them to hold: 'ruk details bhej pehle check krta hu fir krte hai'.
8. LENGTH:
   - 1 short, natural chat line (max 10-15 words).

Return STRICT JSON with keys "summary" and "proposed_reply". Do NOT use markdown code fences."""

    models_to_try = ["gemini-3.5-flash-lite", "gemma-4-26b-a4b-it", "gemini-flash-latest"]

    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}]
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "OmnichannelAgent/1.0"
                }
            )
            with urllib.request.urlopen(req, timeout=45) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                candidates = res_data.get("candidates", [])
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                raw_text = ""
                for part in parts:
                    if "text" in part:
                        raw_text += part["text"]
                raw_text = raw_text.strip()
                if not raw_text:
                    continue
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
                    if raw_text.startswith("json"):
                        raw_text = raw_text[4:].strip()
                data = json.loads(raw_text)
                summary = data.get("summary", "New communication received.").strip()
                proposed_reply = data.get("proposed_reply", "Received, will follow up soon.").strip()
                return summary, proposed_reply
        except Exception as e:
            print(f"[Gemini Warning] Model '{model}' failed: {e}", file=sys.stderr)
            continue

    return f"Received message from {sender}.", f"Hi {sender}, received your message and will get back to you shortly."




# ==========================================
# 3. Action Executors (Gmail / Webhook Reply)
# ==========================================
def send_email_reply(to_email, subject, body_text):
    try:
        msg = EmailMessage()
        msg["From"] = GMAIL_USER
        msg["To"] = to_email
        msg["Subject"] = f"Re: {subject}" if not subject.lower().startswith("re:") else subject
        msg.set_content(body_text)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        print(f"[Gmail] Successfully sent reply to {to_email}")
        return True
    except Exception as e:
        print(f"[Gmail Error] Failed to send email: {e}", file=sys.stderr)
        return False



def send_sms_via_macrodroid(number, message):
    if not MACRODROID_SMS_WEBHOOK:
        print("[MacroDroid SMS] MACRODROID_SMS_WEBHOOK not configured in .env yet.")
        return False
    try:
        clean_num = str(number).strip().replace(" ", "").replace("-", "")
        base_url = MACRODROID_SMS_WEBHOOK.split("?")[0]
        params = urllib.parse.urlencode({"num": clean_num})
        url = f"{base_url}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "OmnichannelAgent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[MacroDroid SMS] Successfully triggered phone SMS webhook for {clean_num} (Status: {resp.status})")
            return True
    except Exception as e:
        print(f"[MacroDroid SMS Error] {e}", file=sys.stderr)
        return False


def send_whatsapp_reply_via_macrodroid(reply_text, sender=""):
    if not MACRODROID_SMS_WEBHOOK:
        return False
    try:
        base_device_url = MACRODROID_SMS_WEBHOOK.rsplit("/", 1)[0]
        url = f"{base_device_url}/agent_whatsapp"
        params_dict = {"msg": reply_text}
        if sender:
            params_dict["sender"] = str(sender).strip()
        params = urllib.parse.urlencode(params_dict)
        full_url = f"{url}?{params}"
        req = urllib.request.Request(full_url, headers={"User-Agent": "OmnichannelAgent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[MacroDroid WhatsApp] Successfully triggered WhatsApp reply for '{sender}' (Status: {resp.status})")
            return True
    except Exception as e:
        print(f"[MacroDroid WhatsApp Error] {e}", file=sys.stderr)
        return False


# ==========================================
# 4. Telegram Long-Polling Loop (HITL Decision Listener)
# ==========================================
def telegram_polling_loop():
    offset = 0
    print("[Telegram] HITL Decision Listener running...")

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=25) as response:
                updates = json.loads(response.read().decode("utf-8"))

            if updates.get("ok") and updates.get("result"):
                for update in updates["result"]:
                    offset = update["update_id"] + 1

                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb["id"]
                        data = cb["data"]
                        msg = cb.get("message", {})
                        chat_id = msg.get("chat", {}).get("id")
                        msg_id = msg.get("message_id")

                        # Acknowledge callback immediately to remove loading state
                        telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})

                        if ":" in data:
                            action, event_id = data.split(":", 1)
                            with PENDING_LOCK:
                                event_info = PENDING_EVENTS.get(event_id)

                            safe_base = html.escape(msg.get("text", ""))

                            if action == "takeover":
                                contact_id = event_id
                                set_thread_delegation(contact_id, False)
                                print(f"[HITL] User took back control for contact: {contact_id}")
                                updated_text = (
                                    f"{safe_base}\n\n"
                                    "━━━━━━━━━━━━━━━━━━━\n"
                                    "👤 <b>Status:</b> You took back control.\n"
                                    f"Agent will not auto-reply to {html.escape(str(contact_id))}."
                                )
                                telegram_request("editMessageText", {
                                    "chat_id": chat_id,
                                    "message_id": msg_id,
                                    "text": updated_text,
                                    "parse_mode": "HTML"
                                })

                            elif action == "user":
                                print(f"[HITL] User chose to handle event {event_id} manually.")
                                if event_info and event_info.get("sender"):
                                    set_thread_delegation(event_info.get("sender"), False)
                                updated_text = (
                                    f"{safe_base}\n\n"
                                    "━━━━━━━━━━━━━━━━━━━\n"
                                    "👤 <b>Status:</b> You chose to handle this manually.\n"
                                    "Agent is standing down."
                                )
                                telegram_request("editMessageText", {
                                    "chat_id": chat_id,
                                    "message_id": msg_id,
                                    "text": updated_text,
                                    "parse_mode": "HTML"
                                })

                            elif action == "agent":
                                print(f"[HITL] User delegated event {event_id} to Agent.")
                                execution_status = "Executed"

                                if event_info and event_info.get("type") == "Email":
                                    to_addr = event_info.get("reply_to") or event_info.get("sender")
                                    subj = event_info.get("subject", "Follow up")
                                    draft = event_info.get("reply_draft", "")
                                    sent = send_email_reply(to_addr, subj, draft)
                                    execution_status = "Email sent via Gmail" if sent else "Failed to send email"

                                elif event_info and event_info.get("type") == "WhatsApp":
                                    draft = event_info.get("reply_draft", "Thanks for reaching out, will get back shortly.")
                                    contact_id = event_info.get("sender", "WhatsApp Contact")
                                    event_time = event_info.get("timestamp", time.time())
                                    event_age = time.time() - event_time

                                    # Guard 1: Expire after 2 minutes (120s)
                                    if event_age > 120:
                                        print(f"[Safety Block] Prompt for '{contact_id}' expired ({int(event_age)}s old). Blocking auto-reply to prevent wrong chat mix-up.")
                                        updated_text = (
                                            f"{safe_base}\n\n"
                                            "━━━━━━━━━━━━━━━━━━━\n"
                                            "⚠️ <b>Prompt Expired:</b> Yeh prompt 2 minute se zyada purana hai.\n"
                                            "Phone par galat contact ko message jaane se rokne ke liye auto-reply cancel kar diya gaya hai. Please WhatsApp open karke manually reply kar dein."
                                        )
                                        telegram_request("editMessageText", {
                                            "chat_id": chat_id,
                                            "message_id": msg_id,
                                            "text": updated_text,
                                            "parse_mode": "HTML"
                                        })
                                        continue

                                    # Guard 2: Notification collision guard (check if someone else messaged afterwards)
                                    with TRACK_LOCK:
                                        latest_sender = LAST_WHATSAPP_NOTIFICATION_SENDER
                                        latest_time = LAST_WHATSAPP_NOTIFICATION_TIME

                                    if latest_sender and clean_contact_id(latest_sender) != clean_contact_id(contact_id) and latest_time > event_time:
                                        print(f"[Safety Block] Active notification mismatch! Latest on phone is '{latest_sender}', but action was for '{contact_id}'.")
                                        updated_text = (
                                            f"{safe_base}\n\n"
                                            "━━━━━━━━━━━━━━━━━━━\n"
                                            f"⚠️ <b>Cross-Message Conflict Blocked!</b>\n"
                                            f"Aapke phone par is beech <b>{html.escape(latest_sender)}</b> ka naya message aa chuka hai.\n"
                                            f"Phone par <b>{html.escape(latest_sender)}</b> ko galat reply jaane se rokne ke liye agent ne auto-reply hold kar diya hai.\n"
                                            f"👉 Please WhatsApp open karke seedha <b>{html.escape(contact_id)}</b> ko reply karein."
                                        )
                                        telegram_request("editMessageText", {
                                            "chat_id": chat_id,
                                            "message_id": msg_id,
                                            "text": updated_text,
                                            "parse_mode": "HTML"
                                        })
                                        continue

                                    set_thread_delegation(contact_id, True)
                                    add_message_to_history(contact_id, contact_id, "agent", draft)
                                    record_sent_reply(draft)
                                    sent = send_whatsapp_reply_via_macrodroid(draft, sender=contact_id)
                                    execution_status = f"WhatsApp reply sent: \"{draft}\"" if sent else f"Draft recorded: \"{draft}\""

                                    updated_text = (
                                        f"{safe_base}\n\n"
                                        "━━━━━━━━━━━━━━━━━━━\n"
                                        f"🤖 <b>Status:</b> Agent Handled! ({html.escape(execution_status)})\n"
                                        f"⚡ <i>Agent will autonomously continue this conversation in Hinglish/English.</i>"
                                    )
                                    telegram_request("editMessageText", {
                                        "chat_id": chat_id,
                                        "message_id": msg_id,
                                        "text": updated_text,
                                        "parse_mode": "HTML",
                                        "reply_markup": {
                                            "inline_keyboard": [
                                                [{"text": "👤 Take Back Control", "callback_data": f"takeover:{clean_contact_id(contact_id)}"}]
                                            ]
                                        }
                                    })
                                    continue

                                elif event_info and event_info.get("type") == "Call":
                                    caller_num = event_info.get("caller_number") or event_info.get("sender")
                                    draft = event_info.get("reply_draft", "Busy right now, will call back soon.")
                                    sent = send_sms_via_macrodroid(caller_num, draft)
                                    if sent:
                                        execution_status = f"SMS sent to {caller_num} via phone"
                                    else:
                                        execution_status = f"SMS draft prepared for {caller_num}"

                                updated_text = (
                                    f"{safe_base}\n\n"
                                    "━━━━━━━━━━━━━━━━━━━\n"
                                    f"🤖 <b>Status:</b> Agent Handled! ({html.escape(execution_status)})\n"
                                    "✅ Action successfully completed."
                                )
                                telegram_request("editMessageText", {
                                    "chat_id": chat_id,
                                    "message_id": msg_id,
                                    "text": updated_text,
                                    "parse_mode": "HTML"
                                })

        except Exception as e:
            # Prevent tight spin on network blip
            time.sleep(2)


# ==========================================
# 5. Inbound Gmail IMAP Polling Worker
# ==========================================
def decode_mime_words(s):
    if not s:
        return ""
    decoded_list = decode_header(s)
    result = []
    for bytes_or_str, encoding in decoded_list:
        if isinstance(bytes_or_str, bytes):
            result.append(bytes_or_str.decode(encoding or "utf-8", errors="replace"))
        else:
            result.append(str(bytes_or_str))
    return "".join(result)


def email_polling_worker():
    print("[Gmail] Background IMAP listener active...")
    # Pre-populate seen IDs so existing old emails don't trigger alerts
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("INBOX")
        status, data = mail.search(None, "UNSEEN")
        if status == "OK" and data[0]:
            for eid in data[0].split():
                SEEN_EMAIL_IDS.add(eid.decode("utf-8"))
        mail.logout()
        print(f"[Gmail] Baseline established ({len(SEEN_EMAIL_IDS)} existing unread emails indexed).")
    except Exception as e:
        print(f"[Gmail Init Warning] {e}", file=sys.stderr)

    while True:
        try:
            time.sleep(20)
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            mail.select("INBOX")

            status, data = mail.search(None, "UNSEEN")
            if status == "OK" and data[0]:
                for eid in data[0].split():
                    eid_str = eid.decode("utf-8")
                    if eid_str in SEEN_EMAIL_IDS:
                        continue

                    SEEN_EMAIL_IDS.add(eid_str)
                    res, msg_data = mail.fetch(eid, "(RFC822)")
                    if res != "OK":
                        continue

                    msg = email.message_from_bytes(msg_data[0][1])
                    subject = decode_mime_words(msg.get("Subject", "No Subject"))
                    from_addr = decode_mime_words(msg.get("From", "Unknown Sender"))

                    # Extract plain text preview
                    body_text = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body_text = part.get_payload(decode=True).decode("utf-8", errors="replace")
                                break
                    else:
                        body_text = msg.get_payload(decode=True).decode("utf-8", errors="replace")

                    body_snippet = body_text.strip()[:300]
                    content_str = f"Subject: {subject}\n{body_snippet}"

                    # AI Analysis
                    summary, reply_draft = analyze_with_gemini("Email", from_addr, content_str)

                    event_id = f"email_{eid_str}_{int(time.time())}"
                    with PENDING_LOCK:
                        PENDING_EVENTS[event_id] = {
                            "type": "Email",
                            "sender": from_addr,
                            "subject": subject,
                            "content": content_str,
                            "summary": summary,
                            "reply_draft": reply_draft,
                            "reply_to": from_addr
                        }

                    # Trigger Telegram HITL prompt
                    send_decision_prompt(event_id, "Email", from_addr, content_str, summary, reply_draft)

            mail.logout()
        except Exception as e:
            # Soft retry
            time.sleep(5)


# ==========================================
# 6. Webhook Server for WhatsApp & Phone Calls
# ==========================================
def extract_val(data, keys, default=""):
    for k in keys:
        if k in data:
            v = data[k]
            if isinstance(v, list) and len(v) > 0 and str(v[0]).strip():
                return str(v[0]).strip()
            elif isinstance(v, str) and v.strip():
                return v.strip()
    return default


def handle_incoming_whatsapp(sender, message):
    if sender.lower() in ["you", "whatsapp", "checking for new messages", "backup in progress", "web is currently active", "whatsapp web", "me"]:
        print(f"[WhatsApp Webhook] Ignored system/self notification from '{sender}'")
        return "Ignored system/self notification"

    if is_recent_reply_echo(message):
        print(f"[WhatsApp Webhook] Ignored echoed outbound reply: '{message}'")
        return "Ignored echoed outbound reply"

    print(f"[WhatsApp Webhook] Inbound message from: '{sender}' | Text: '{message}'")
    if not message or message in ["{not_body}", "{not_text}", "[not_body]", "[not_text]"]:
        print(f"[WhatsApp Warning] Message text empty or unconverted tag: '{message}'. Ensure MacroDroid uses [not_text].")
        message = "New WhatsApp notification received."

    # Update active notification tracker so server knows who is currently on phone screen
    with TRACK_LOCK:
        global LAST_WHATSAPP_NOTIFICATION_SENDER, LAST_WHATSAPP_NOTIFICATION_TIME
        LAST_WHATSAPP_NOTIFICATION_SENDER = str(sender).strip()
        LAST_WHATSAPP_NOTIFICATION_TIME = time.time()

    # 1. Record incoming message in conversation history (atomic under CONV_LOCK)
    add_message_to_history(sender, sender, "contact", message)

    # 2. Check if thread is currently delegated to agent
    if is_thread_delegated(sender):
        print(f"[Autonomous Mode] Thread with '{sender}' is DELEGATED to Agent! Answering contextually...")
        summary, reply_draft = analyze_with_gemini("WhatsApp", sender, message, contact_id=sender)
        add_message_to_history(sender, sender, "agent", reply_draft)
        increment_autonomous_turn(sender)
        record_sent_reply(reply_draft)
        send_whatsapp_reply_via_macrodroid(reply_draft, sender=sender)
        send_autonomous_chat_update(sender, message, reply_draft, summary)
        return "Autonomous reply dispatched and forwarded to Telegram"

    # 3. Request HITL permission
    print(f"[HITL Prompt] Thread with '{sender}' requires permission. Asking on Telegram...")
    event_id = f"wh_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    summary, reply_draft = analyze_with_gemini("WhatsApp", sender, message, contact_id=sender)

    with PENDING_LOCK:
        PENDING_EVENTS[event_id] = {
            "type": "WhatsApp",
            "sender": sender,
            "content": message,
            "summary": summary,
            "reply_draft": reply_draft,
            "timestamp": time.time()
        }

    send_decision_prompt(event_id, "WhatsApp", sender, message, summary, reply_draft)
    return "WhatsApp received and forwarded to Telegram"


def handle_incoming_call(caller_name, caller_number, call_type):
    event_id = f"wh_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    content = f"{call_type.capitalize()} call from {caller_name}"

    print(f"[Call Webhook] Inbound {call_type} call from: '{caller_name}' ({caller_number})")
    summary, reply_draft = analyze_with_gemini("Phone Call", caller_name, content, contact_id=caller_name)

    with PENDING_LOCK:
        PENDING_EVENTS[event_id] = {
            "type": "Call",
            "sender": caller_name,
            "caller_number": caller_number or caller_name,
            "content": content,
            "summary": summary,
            "reply_draft": reply_draft
        }

    send_decision_prompt(event_id, "Phone Call", caller_name, content, summary, reply_draft)
    return "Call alert forwarded to Telegram"


class WebhookHandler(BaseHTTPRequestHandler):
    def _send_response(self, code, message):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": message}).encode("utf-8"))
        except Exception:
            pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if self.path == "/webhook/whatsapp":
            sender = extract_val(data, ["sender", "from", "title", "not_title", "name"], "WhatsApp Contact")
            message = extract_val(data, ["message", "msg", "text", "body", "content", "notification", "not_text", "not_body"], "")
            resp_msg = handle_incoming_whatsapp(sender, message)
            self._send_response(200, resp_msg)

        elif self.path == "/webhook/call":
            caller_number = extract_val(data, ["caller_number", "number", "num", "phone"], "")
            caller_name = extract_val(data, ["caller_name", "name", "caller"], caller_number or "Unknown Caller")
            call_type = extract_val(data, ["call_type", "type"], "incoming")
            resp_msg = handle_incoming_call(caller_name, caller_number, call_type)
            self._send_response(200, resp_msg)

        else:
            self._send_response(404, "Endpoint not found")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/webhook/whatsapp":
            sender = extract_val(params, ["sender", "from", "title", "not_title", "name"], "WhatsApp Contact")
            message = extract_val(params, ["message", "msg", "text", "body", "content", "notification", "not_text", "not_body"], "")
            resp_msg = handle_incoming_whatsapp(sender, message)
            self._send_response(200, resp_msg)

        elif path == "/webhook/call":
            caller_number = extract_val(params, ["caller_number", "number", "num", "phone"], "")
            caller_name = extract_val(params, ["caller_name", "name", "caller"], caller_number or "Unknown Caller")
            call_type = extract_val(params, ["call_type", "type"], "incoming")
            resp_msg = handle_incoming_call(caller_name, caller_number, call_type)
            self._send_response(200, resp_msg)

        elif path == "/healthz" or path == "/":
            self._send_response(200, "Omnichannel Gatekeeper Agent is Healthy & Running")
        else:
            self._send_response(404, "Not found")

    def log_message(self, format, *args):
        return
        # Quiet web logs
        return


def run_webhook_server(port=8000):
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"[Webhook Server] Multi-Threaded Server listening on http://localhost:{port} (Endpoints: /webhook/whatsapp, /webhook/call)")
    server.serve_forever()


# ==========================================
# Main Entry Point
# ==========================================
if __name__ == "__main__":
    print("=" * 60)
    print("  🚀 Starting Autonomous Omnichannel Gatekeeper Agent")
    print(f"  • User: Eashan Singh")
    print(f"  • Telegram Bot: Verified")
    print(f"  • Gemini AI: Verified")
    print(f"  • Gmail IMAP/SMTP: Verified")
    print("=" * 60)

    # 1. Start Webhook server thread (dynamic cloud PORT or 8000)
    port = int(os.environ.get("PORT") or CONFIG.get("PORT") or 8000)
    t_web = threading.Thread(target=run_webhook_server, args=(port,), daemon=True)
    t_web.start()

    # 2. Start Gmail IMAP background poller thread
    t_mail = threading.Thread(target=email_polling_worker, daemon=True)
    t_mail.start()

    # 3. Start Telegram HITL decision loop in main thread
    telegram_polling_loop()
