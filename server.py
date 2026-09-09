#!/usr/bin/env python3
"""
Hotmail OTP Reader v3.0
========================
New in v3:
  ✅ Enlist all accounts first (show table before fetching)
  ✅ Fetch OTP one-by-one sequentially
  ✅ Latest OTP per account (newest email first)
  ✅ Live progress bar
  ✅ Copy button per row
  ✅ Summary: total found / total failed

Run: python server.py
"""

import json, re, os, hashlib, urllib.request, urllib.parse, socket, subprocess, threading, sys, subprocess, threading, time as _time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

CONFIG = { "site_password": "masood123" }
SESSIONS = set()

def get_local_ip():
    """Get this machine's LAN IP so other devices can connect."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

LOCAL_IP  = get_local_ip()
PUBLIC_URL = None   # filled by ngrok if available

# ─── Ngrok Tunnel Manager ─────────────────────────────────────────────────────
class NgrokTunnel:
    proc    = None
    url     = None
    status  = "idle"   # idle | starting | running | error | not_installed

    @staticmethod
    def _find_ngrok():
        """Find ngrok binary (Windows + Linux)."""
        candidates = ["ngrok", "ngrok.exe",
                      os.path.join(os.path.expanduser("~"), "ngrok.exe"),
                      os.path.join(os.path.expanduser("~"), "ngrok"),
                      r"C:\ngrok\ngrok.exe",
                      r"C:\Users\Public\ngrok.exe"]
        for c in candidates:
            try:
                r = subprocess.run([c, "version"], capture_output=True, timeout=5)
                if r.returncode == 0:
                    return c
            except: pass
        return None

    @classmethod
    def start(cls, port):
        """Start ngrok in background thread."""
        NgrokTunnel.status = "starting"
        NgrokTunnel.url    = None
        def _run():
            global PUBLIC_URL
            ngrok_bin = NgrokTunnel._find_ngrok()
            if not ngrok_bin:
                NgrokTunnel.status = "not_installed"
                return
            try:
                # Kill any existing ngrok
                try: subprocess.run([ngrok_bin,"stop"], capture_output=True, timeout=3)
                except: pass
                _time.sleep(1)
                # Start tunnel
                NgrokTunnel.proc = subprocess.Popen(
                    [ngrok_bin, "http", str(port), "--log=stdout"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                NgrokTunnel.status = "running"
                # Poll ngrok API for public URL
                for _ in range(20):
                    _time.sleep(1)
                    try:
                        req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
                        with urllib.request.urlopen(req, timeout=3) as r:
                            data = json.loads(r.read())
                        tunnels = data.get("tunnels", [])
                        for t in tunnels:
                            u = t.get("public_url","")
                            if u.startswith("https://"):
                                NgrokTunnel.url = u
                                PUBLIC_URL = u
                                print(f"\n🌐 PUBLIC URL: {u}")
                                print(f"   Share this with anyone on any network!\n")
                                return
                    except: pass
                NgrokTunnel.status = "error"
            except Exception as e:
                NgrokTunnel.status = "error"
                print(f"Ngrok error: {e}")
        threading.Thread(target=_run, daemon=True).start()

    @classmethod
    def stop(cls):
        if cls.proc:
            try: cls.proc.terminate()
            except: pass
            cls.proc = None
        cls.url = cls.status = None


# ─── Persistent Account Store ─────────────────────────────────────────────────
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts_data.json")

class AccountStore:
    """Save / load / delete accounts from a JSON file on disk."""
    _lock = threading.Lock()

    @classmethod
    def _load_raw(cls):
        try:
            with open(DATA_FILE,"r",encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except: return []

    @classmethod
    def _save_raw(cls, data):
        with open(DATA_FILE,"w",encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def all(cls):
        with cls._lock: return cls._load_raw()

    @classmethod
    def count(cls):
        return len(cls.all())

    @classmethod
    def add_lines(cls, lines: list):
        """Add new account lines (skip duplicates by email)."""
        with cls._lock:
            existing = cls._load_raw()
            existing_emails = {r.get("email","").lower() for r in existing}
            added = 0
            for line in lines:
                line = line.strip()
                if not line: continue
                acct = parse_account_line(line)
                if not acct: continue
                if acct["email"].lower() in existing_emails: continue
                existing.append({
                    "id":       hashlib.md5(line.encode()).hexdigest()[:10],
                    "line":     line,
                    "email":    acct["email"],
                    "uid":      acct.get("uid",""),
                    "added_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
                existing_emails.add(acct["email"].lower())
                added += 1
            cls._save_raw(existing)
            return added

    @classmethod
    def delete_by_id(cls, acc_id: str):
        with cls._lock:
            data = cls._load_raw()
            new_data = [r for r in data if r.get("id") != acc_id]
            cls._save_raw(new_data)
            return len(data) - len(new_data)

    @classmethod
    def delete_all(cls):
        with cls._lock:
            cls._save_raw([])

MSA_DOMAINS = {
    "hotmail.com","hotmail.co.uk","outlook.com","live.com",
    "live.co.uk","msn.com","windowslive.com","passport.com"
}

MS_CLIENT_IDS = [
    "d3590ed6-52b3-4102-aeff-aad2292ab01c",
    "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
    "27922004-5251-4030-b22d-91ecd9a37ea4",
    "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    "1b730954-1685-4b74-9bfd-dac224a7b894",
]

def is_personal_account(email):
    domain = email.lower().split("@")[-1] if "@" in email else ""
    return domain in MSA_DOMAINS

def get_token_endpoint(email):
    if is_personal_account(email):
        return "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    return "https://login.microsoftonline.com/common/oauth2/v2.0/token"

def detect_client_id(token):
    if not token: return MS_CLIENT_IDS[0]
    t = token.strip()
    if t.startswith("M.C5"): return "d3590ed6-52b3-4102-aeff-aad2292ab01c"
    if t.startswith("M.R3"): return "27922004-5251-4030-b22d-91ecd9a37ea4"
    if t.startswith("0."):   return "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
    return MS_CLIENT_IDS[0]

def clean_token(token):
    if not token: return token
    return re.sub(r'[\x00-\x1F\x7F]', '', token).strip()

def refresh_access_token(refresh_token, client_id, email):
    url  = get_token_endpoint(email)
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": "https://graph.microsoft.com/Mail.Read offline_access",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return None, str(e)

def refresh_access_token_auto(refresh_token, email, client_id=""):
    first  = client_id.strip() if client_id.strip() else detect_client_id(refresh_token)
    cands  = [first] + [c for c in MS_CLIENT_IDS if c != first]
    last_e = None
    for cid in cands:
        tok, err = refresh_access_token(refresh_token, cid, email)
        if err:             last_e = f"[{cid[:8]}] {err}"; continue
        if tok and "access_token" in tok:
            return tok["access_token"], cid, None
        if tok and "error" in tok:
            desc = tok.get("error_description", tok["error"])
            if "invalid_grant" in tok["error"] or "AADSTS70008" in desc:
                return None, cid, f"Token expired: {desc[:150]}"
            last_e = f"[{cid[:8]}] {desc[:80]}"; continue
    return None, "", f"All client IDs failed. {last_e}"

def graph_get(path, access_token):
    if "?" in path:
        base, qs = path.split("?", 1)
        parts = []
        for p in qs.split("&"):
            k, v = p.split("=", 1) if "=" in p else (p, "")
            parts.append(f"{k}={urllib.parse.quote(v, safe=',$')}")
        path = f"{base}?{'&'.join(parts)}"
    url = f"https://graph.microsoft.com/v1.0{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return None, str(e)

# ── Known security/OTP senders ───────────────────────────────────────────────
OTP_SENDERS = [
    "security@facebookmail.com",
    "account-security-noreply@accountprotection.microsoft.com",
    "no-reply@accounts.google.com",
    "noreply@microsoft.com",
    "account@twitter.com",
    "security-noreply@linkedin.com",
    "noreply@steampowered.com",
    "no-reply@mail.instagram.com",
    "no-reply@facebookmail.com",
    "notify@twitter.com",
    "noreply@paypal.com",
    "service@paypal.com",
    "info@amazonses.com",
    "no-reply@amazonses.com",
]

# Subject keywords that indicate this is an OTP/security email
OTP_SUBJECT_KEYWORDS = [
    "code", "otp", "pin", "verify", "verif", "confirm", "authentica",
    "one-time", "one time", "passcode", "security", "2fa", "two-factor",
    "login", "sign in", "sign-in", "access", "votre code", "est votre",
    "is your", "verification", "kode", "mã", "código", "bestätigungscode",
    "bevestigingscode", "codice", "kod", "kód", "code de sécurité",
    "unusual sign", "new sign", "new login", "account recovery",
]

# Strict context-aware patterns (require surrounding keywords)
# These work WITH context — no bare naked digit matches
OTP_PATTERNS_STRICT = [
    # "123456 is your code" style (number first)
    r'(\d{4,8})(?:\s+is\s+your|\s+est\s+votre|\s+ist\s+Ihr|\s+é\s+seu|\s+es\s+tu)',
    # "code: 123456" / "code is 123456" style (keyword first)
    r'(?:code|otp|pin|passcode|kode|mã|código|codice|kod|kód)[^\d]{0,25}(\d{4,8})',
    # "verify|confirm|enter: 123456"
    r'(?:verify|confirm|verif|enter|use|utilize)[^\d]{0,25}(\d{4,8})',
    # "authentication code 123456"
    r'(?:authentication|verification|security|access)[^\d]{0,25}(\d{4,8})',
    # Standalone 6-digit (only used when email is from trusted sender OR subject has keywords)
    r'\b(\d{6})\b',
    r'\b(\d{8})\b',
]

SKIP = {
    "000000","111111","123456","999999","123123","654321",
    "222222","333333","444444","555555","666666","777777","888888",
    "12345678","00000000","11111111","99999999",
}

def is_otp_sender(sender: str) -> bool:
    """Check if sender is a known OTP/security sender."""
    s = sender.lower().strip()
    return any(known in s for known in OTP_SENDERS)

def is_otp_subject(subject: str) -> bool:
    """Check if subject line contains OTP-related keywords."""
    s = subject.lower()
    return any(kw in s for kw in OTP_SUBJECT_KEYWORDS)

def extract_otp_from_text(text: str, strict: bool = False) -> str:
    """
    Extract OTP from text.
    strict=True  → only context-aware patterns (for non-OTP-looking emails)
    strict=False → all patterns including bare 6-digit (for confirmed OTP emails)
    """
    if not text: return None
    # Strip HTML tags and URLs
    c = re.sub(r'<[^>]+>', ' ', text)
    c = re.sub(r'https?://\S+', ' ', c)
    c = re.sub(r'\s+', ' ', c)

    patterns = OTP_PATTERNS_STRICT if not strict else OTP_PATTERNS_STRICT[:4]

    for pat in patterns:
        for m in re.findall(pat, c, re.IGNORECASE):
            m = m.strip()
            if m and m not in SKIP and len(m) >= 4:
                return m
    return None

def extract_otp(subject: str, body: str, sender: str) -> str:
    """
    Smart OTP extraction:
    1. If sender is known OTP sender → try all patterns
    2. If subject has OTP keywords → try all patterns
    3. Otherwise → skip (avoid false positives from notification emails)
    """
    trusted_sender  = is_otp_sender(sender)
    otp_subject     = is_otp_subject(subject)

    if trusted_sender or otp_subject:
        # Full extraction: subject first (fastest), then body
        otp = extract_otp_from_text(subject, strict=False)
        if otp: return otp
        otp = extract_otp_from_text(body, strict=False)
        if otp: return otp
    # else: general email → skip, don't extract
    return None

def parse_account_line(line):
    """
    Supports two formats:
    NEW (5-6 parts): uid|fbpass|email|emailpass|refresh_token[|client_id]
    OLD (3-4 parts): email|emailpass|refresh_token[|client_id]
    """
    line = line.strip()
    if not line or "|" not in line: return None
    p = line.split("|")

    # NEW format: uid|fbpass|email|emailpass|refresh_token[|client_id]
    if len(p) >= 5 and "@" not in p[0].strip() and not p[0].strip().startswith("M."):
        uid       = p[0].strip()
        fbpass    = p[1].strip()
        email     = p[2].strip()
        emailpass = p[3].strip()
        rt        = clean_token(p[4])
        cid       = p[5].strip() if len(p) >= 6 else ""
        if not email or not rt: return None
        return {"uid": uid, "fbpass": fbpass, "email": email,
                "emailpass": emailpass, "refresh_token": rt,
                "client_id": cid, "format": "new"}

    # OLD format: email|emailpass|refresh_token[|client_id]
    if len(p) >= 3:
        email = p[0].strip()
        rt    = clean_token(p[2])
        cid   = p[3].strip() if len(p) >= 4 else ""
        if not email or not rt: return None
        return {"uid": "", "fbpass": "", "email": email,
                "emailpass": p[1].strip(), "refresh_token": rt,
                "client_id": cid, "format": "old"}

    return None

def fetch_latest_otp(account, limit=20):
    """Fetch emails and return ONLY the latest OTP found."""
    email = account["email"]
    at, used_cid, err = refresh_access_token_auto(
        account["refresh_token"], email, account.get("client_id",""))
    if err or not at:
        return None, f"Token refresh failed: {err}"

    fields = "id,subject,from,receivedDateTime,body"
    path   = f"/me/mailFolders/inbox/messages?$top={limit}&$select={fields}&$orderby=receivedDateTime desc"
    data, err = graph_get(path, at)
    if err: return None, f"Graph API error: {err}"

    # Newest first — return first REAL OTP found
    for msg in data.get("value", []):
        subject   = msg.get("subject","") or ""
        body_text = (msg.get("body",{}) or {}).get("content","") or ""
        date_raw  = msg.get("receivedDateTime","")
        sender    = (msg.get("from",{}) or {}).get("emailAddress",{}).get("address","")
        otp       = extract_otp(subject, body_text, sender)
        if otp:
            try:    date_fmt = date_raw[:16].replace("T"," ")
            except: date_fmt = date_raw
            return {
                "otp":      otp,
                "subject":  subject[:120],
                "sender":   sender[:80],
                "date":     date_fmt,
                "endpoint": get_token_endpoint(email),
                "client":   used_cid[:8] + "..." if used_cid else "",
            }, None

    return {"otp": None, "endpoint": get_token_endpoint(email)}, None


# ─── HTML ─────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTP Reader v3</title>
<meta id="serverIp" content="__SERVER_IP__">
<meta id="serverPort" content="__SERVER_PORT__">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{min-height:100vh;background:#f0f2f8;display:flex;align-items:center;justify-content:center;font-family:'Courier New',monospace}}
.box{{background:#fff;border:1.5px solid #dde1ea;border-radius:16px;padding:44px 40px;width:360px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.10)}}
.ico{{font-size:44px;margin-bottom:12px}}
h1{{color:#1a1a2e;font-size:16px;font-weight:900;letter-spacing:5px;margin-bottom:6px;text-transform:uppercase}}
p{{color:#999;font-size:11px;margin-bottom:32px;font-weight:700}}
input{{width:100%;padding:13px 16px;background:#f5f6fa;border:2px solid #dde1ea;border-radius:9px;color:#1a1a2e;font-size:14px;font-family:inherit;font-weight:700;margin-bottom:14px;outline:none;transition:border-color .2s}}
input:focus{{border-color:#4455ff}}
button{{width:100%;padding:14px;background:#4455ff;border:none;border-radius:9px;color:#fff;font-size:13px;font-family:inherit;cursor:pointer;letter-spacing:3px;font-weight:900;transition:background .2s}}
button:hover{{background:#3344dd}}
.err{{color:#cc3333;font-size:12px;margin-bottom:14px;font-weight:700}}
</style></head>
<body><div class="box">
<div class="ico">🔐</div>
<h1>OTP READER</h1>
<p>HOTMAIL · OUTLOOK · LIVE</p>
{err}
<form method="POST" action="/login">
<input type="password" name="site_pass" placeholder="Enter Site Password" required autofocus>
<button>ENTER →</button>
</form>
</div></body></html>"""

APP_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTP Reader v3</title>
<style>
/* ════ BASE ════ */
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f0f2f8;color:#1a1a2e;font-family:'Courier New',monospace;min-height:100vh;font-weight:700}

/* ════ HEADER ════ */
header{background:#fff;border-bottom:2px solid #dde1ea;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 10px rgba(0,0,0,.07);position:sticky;top:0;z-index:200}
.brand{color:#1a1a2e;font-size:15px;font-weight:900;letter-spacing:4px}
.hright{display:flex;align-items:center;gap:14px}
.ver{color:#aaa;font-size:10px;font-weight:700;letter-spacing:2px}
.net-btn{background:#eef0ff;border:2px solid #c0c8ff;border-radius:6px;color:#4455ff;font-size:11px;font-family:inherit;cursor:pointer;font-weight:900;padding:5px 11px;transition:all .2s;letter-spacing:1px}
.net-btn:hover{background:#4455ff;color:#fff}
.tunnel-btn{background:#e6fff3;border-color:#99ddbb;color:#22aa66}
.tunnel-btn:hover{background:#22aa66;color:#fff}
.tunnel-btn.active{background:#22aa66;color:#fff;border-color:#22aa66}
.logout{color:#cc3333;font-size:11px;font-weight:900;text-decoration:none;letter-spacing:1px;border:1.5px solid #ffcccc;padding:4px 10px;border-radius:6px;transition:all .2s}
.logout:hover{background:#cc3333;color:#fff}

/* ════ LAYOUT ════ */
.wrap{max-width:1080px;margin:0 auto;padding:22px 16px}

/* ════ PANELS ════ */
.panel{background:#fff;border:2px solid #dde1ea;border-radius:14px;padding:22px;margin-bottom:18px;box-shadow:0 2px 12px rgba(0,0,0,.05)}
.panel-title{color:#4455ff;font-size:10px;letter-spacing:3px;font-weight:900;margin-bottom:12px;text-transform:uppercase}

/* ════ FORMAT HINT ════ */
.fmt{background:#f0f2ff;border:2px solid #d0d4ff;border-radius:10px;padding:13px 16px;margin-bottom:14px;font-size:11px;color:#333;line-height:2}
.fmt b{color:#1a1a2e;font-weight:900}
.fmt code{color:#4455ff;background:#e8ebff;padding:2px 8px;border-radius:4px;font-weight:900;font-size:10.5px}
.fmt .dim{color:#aaa;font-weight:700}

/* ════ TEXTAREA ════ */
textarea{width:100%;height:100px;background:#f9fafc;border:2px solid #dde1ea;border-radius:10px;color:#1a1a2e;font-size:11.5px;font-family:inherit;font-weight:700;padding:12px;resize:vertical;outline:none;transition:border-color .2s}
textarea:focus{border-color:#4455ff}
textarea::placeholder{color:#ccc;font-weight:700}

/* ════ CONTROLS ════ */
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
select{padding:9px 13px;background:#f9fafc;border:2px solid #dde1ea;border-radius:8px;color:#1a1a2e;font-size:11.5px;font-family:inherit;font-weight:900;outline:none;cursor:pointer}
.btn{padding:10px 22px;background:#4455ff;border:none;border-radius:8px;color:#fff;font-size:11.5px;font-family:inherit;cursor:pointer;letter-spacing:1.5px;font-weight:900;transition:background .2s;white-space:nowrap}
.btn:hover{background:#3344dd}
.btn:disabled{background:#aab;cursor:not-allowed}
.btn-green{background:#22aa66}.btn-green:hover{background:#1a8850}
.btn-red{background:#cc3333}.btn-red:hover{background:#aa2222}
.btn-grey{background:#888;border:none}.btn-grey:hover{background:#666}
.btn-sm{padding:6px 13px;font-size:10px;letter-spacing:1px}

/* ════ PROGRESS ════ */
.prog-wrap{background:#fff;border:2px solid #dde1ea;border-radius:12px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 8px rgba(0,0,0,.05)}
.prog-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.prog-label{color:#4455ff;font-size:11px;font-weight:900;letter-spacing:2px}
.prog-nums{color:#1a1a2e;font-size:13px;font-weight:900}
.prog-bar-bg{background:#eef0ff;border-radius:8px;height:9px;overflow:hidden}
.prog-bar{background:linear-gradient(90deg,#4455ff,#22aa66);height:9px;border-radius:8px;transition:width .4s ease;width:0%}
.prog-status{color:#666;font-size:10.5px;margin-top:7px;font-weight:700}

/* ════ SUMMARY ════ */
.summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.chip{padding:7px 16px;border-radius:20px;font-size:11px;font-weight:900;letter-spacing:.5px}
.chip-blue{background:#eef0ff;color:#4455ff;border:2px solid #c0c8ff}
.chip-green{background:#e6fff3;color:#22aa66;border:2px solid #99ddbb}
.chip-red{background:#fff5f5;color:#cc3333;border:2px solid #ffcccc}
.chip-grey{background:#f5f6fa;color:#666;border:2px solid #dde1ea}

/* ════ SECTION TITLE ════ */
.sec-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.sec-title{color:#4455ff;font-size:10px;letter-spacing:3px;font-weight:900;text-transform:uppercase}

/* ════ DESKTOP TABLE ════ */
.tbl-desktop{display:block}
.tbl-wrap{background:#fff;border:2px solid #dde1ea;border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.06)}
.tbl-head{
  display:grid;
  grid-template-columns:36px 52px minmax(140px,1fr) 110px minmax(120px,1.2fr) 86px 130px;
  background:#f0f2ff;border-bottom:2px solid #dde1ea;
  padding:10px 14px;font-size:9px;color:#4455ff;letter-spacing:2px;font-weight:900;text-transform:uppercase
}
.row-item{
  display:grid;
  grid-template-columns:36px 52px minmax(140px,1fr) 110px minmax(120px,1.2fr) 86px 130px;
  padding:11px 14px;border-bottom:1.5px solid #f0f1f6;align-items:center;
  transition:background .15s;gap:4px
}
.row-item:last-child{border-bottom:none}
.row-item:hover{background:#fafbff}

/* ════ STATUS BADGES ════ */
.st{display:inline-block;padding:3px 9px;border-radius:10px;font-size:9px;font-weight:900;letter-spacing:.5px;white-space:nowrap;text-transform:uppercase}
.st-pending{background:#f5f6fa;color:#bbb;border:1.5px solid #dde1ea}
.st-fetching{background:#fff8e6;color:#cc8800;border:1.5px solid #ffddaa}
.st-found{background:#e6fff3;color:#22aa66;border:1.5px solid #99ddbb}
.st-none{background:#f5f6fa;color:#999;border:1.5px solid #dde1ea}
.st-err{background:#fff5f5;color:#cc3333;border:1.5px solid #ffcccc}

/* ════ CELL TYPES ════ */
.num-cell{color:#aaa;font-size:12px;font-weight:900}
.uid-cell{font-size:11px;font-weight:900;color:#1a1a2e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.email-cell{font-size:11px;font-weight:900;color:#333;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.otp-cell{font-size:18px;font-weight:900;color:#4455ff;letter-spacing:3px;cursor:pointer;user-select:none;transition:color .15s}
.otp-cell:hover{color:#2233bb}
.otp-none{color:#ccc;font-size:12px;font-weight:900;letter-spacing:0}
.otp-err{color:#cc3333;font-size:9px;font-weight:700;word-break:break-all;line-height:1.4}
.subj-cell{color:#666;font-size:10px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.date-cell{color:#999;font-size:10px;font-weight:700}

/* ════ ACTION CELL ════ */
.act-cell{display:flex;flex-direction:column;gap:5px}
.copy-otp-btn{padding:4px 10px;background:#eef0ff;border:2px solid #c0c8ff;border-radius:6px;color:#4455ff;font-size:9.5px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap}
.copy-otp-btn:hover{background:#4455ff;color:#fff;border-color:#4455ff}
.copy-otp-btn:disabled{background:#f5f6fa;border-color:#dde1ea;color:#ccc;cursor:default}
.copy-uid-btn{padding:4px 10px;background:#fff8e6;border:2px solid #ffddaa;border-radius:6px;color:#cc8800;font-size:9.5px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap}
.copy-uid-btn:hover{background:#cc8800;color:#fff;border-color:#cc8800}
.copy-uid-btn:disabled{background:#f5f6fa;border-color:#dde1ea;color:#ccc;cursor:default}
.fetch-row-btn{padding:4px 10px;background:#e6fff3;border:2px solid #99ddbb;border-radius:6px;color:#22aa66;font-size:9.5px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap}
.fetch-row-btn:hover{background:#22aa66;color:#fff;border-color:#22aa66}
.fetch-row-btn:disabled{background:#f5f6fa;border-color:#dde1ea;color:#ccc;cursor:default}
.fetch-row-btn.fetching{background:#fff8e6;border-color:#ffddaa;color:#cc8800;cursor:not-allowed}

/* ════ MOBILE CARDS ════ */
.tbl-mobile{display:none}
.m-card{background:#fff;border:2px solid #dde1ea;border-radius:12px;padding:14px;margin-bottom:10px;box-shadow:0 2px 8px rgba(0,0,0,.05)}
.m-card:hover{border-color:#4455ff}
.m-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.m-num{color:#4455ff;font-size:20px;font-weight:900;line-height:1;margin-right:10px}
.m-emails{flex:1;min-width:0}
.m-uid{color:#1a1a2e;font-size:12px;font-weight:900;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m-email{color:#666;font-size:11px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
.m-st-wrap{flex-shrink:0;margin-left:8px}
.m-otp-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;padding:10px;background:#f9fafc;border-radius:8px;border:1.5px solid #eef0ff}
.m-otp-lbl{color:#aaa;font-size:9px;font-weight:900;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:2px}
.m-otp-val{font-size:22px;font-weight:900;color:#4455ff;letter-spacing:4px;cursor:pointer}
.m-otp-val:hover{color:#2233bb}
.m-otp-none{font-size:14px;font-weight:900;color:#ccc;letter-spacing:0}
.m-otp-err{font-size:10px;font-weight:700;color:#cc3333;word-break:break-all}
.m-subj{color:#666;font-size:10.5px;font-weight:700;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m-date{color:#aaa;font-size:10px;font-weight:700;margin-bottom:10px}
.m-btns{display:flex;gap:8px}

/* ════ TOAST ════ */
.toast{position:fixed;bottom:20px;right:20px;background:#1a1a2e;color:#fff;padding:11px 22px;border-radius:10px;font-size:12px;font-weight:900;opacity:0;transition:opacity .3s;pointer-events:none;box-shadow:0 4px 20px rgba(0,0,0,.25);letter-spacing:1px;z-index:999}
.toast.show{opacity:1}

/* ════ ACTION ROW ════ */
#actionRow{margin-top:14px}

/* ════ RESPONSIVE ════ */
@media(max-width:700px){
  .tbl-desktop{display:none}
  .tbl-mobile{display:block}
  .wrap{padding:14px 10px}
  header{padding:12px 14px}
  .brand{font-size:13px;letter-spacing:2px}
  .btn{padding:10px 16px;font-size:11px}
  .summary{gap:7px}
  .chip{padding:6px 12px;font-size:10px}
}
</style></head>
<body>
<header>
  <span class="brand">🔐 OTP READER</span>
  <div class="hright">
    <span class="ver">V3.0</span>
    <button class="net-btn" onclick="toggleQR()" title="Same WiFi access">📱 WIFI</button>
    <button class="net-btn tunnel-btn" id="tunnelBtn" onclick="toggleTunnel()" title="Access from any network">🌐 PUBLIC URL</button>
    <a href="/admin" class="net-btn" style="text-decoration:none;background:#fff8e6;border-color:#ffddaa;color:#cc8800" title="Admin Panel">🛡️ ADMIN</a>
    <a href="/logout" class="logout">LOGOUT</a>
  </div>
</header>
<div id="qrPanel" style="display:none;background:#fff;border-bottom:2px solid #dde1ea;padding:18px 24px;box-shadow:0 2px 8px rgba(0,0,0,.07)">
  <div style="max-width:900px;margin:0 auto">
    <div style="font-size:12px;font-weight:900;color:#4455ff;letter-spacing:2px;margin-bottom:14px;text-align:center">📱 OPEN ON ANY DEVICE — ANY NETWORK</div>

    <!-- Two column layout -->
    <div style="display:flex;gap:20px;flex-wrap:wrap;justify-content:center">

      <!-- LEFT: Same WiFi (Local) -->
      <div style="flex:1;min-width:260px;background:#f0f2ff;border:2px solid #c0c8ff;border-radius:12px;padding:16px">
        <div style="font-size:10px;font-weight:900;color:#4455ff;letter-spacing:2px;margin-bottom:10px">📶 SAME WIFI (LOCAL)</div>
        <div style="font-size:11px;font-weight:700;color:#555;margin-bottom:8px">PC aur mobile ek hi WiFi pe hon</div>
        <a id="netUrl" href="#" style="display:block;color:#4455ff;font-size:15px;font-weight:900;margin-bottom:10px;word-break:break-all" target="_blank">Loading...</a>
        <div id="localQR" style="text-align:center"></div>
        <div style="font-size:9.5px;color:#cc3333;font-weight:700;margin-top:10px">Firewall block? CMD (Admin) mein run karo:<br>
        <code style="background:#fff5f5;padding:2px 5px;border-radius:4px;font-size:9px;word-break:break-all">netsh advfirewall firewall add rule name="OTP5000" dir=in action=allow protocol=TCP localport=5000</code></div>
      </div>

      <!-- RIGHT: Any Network (ngrok) -->
      <div style="flex:1;min-width:260px;background:#e6fff3;border:2px solid #99ddbb;border-radius:12px;padding:16px">
        <div style="font-size:10px;font-weight:900;color:#22aa66;letter-spacing:2px;margin-bottom:10px">🌐 ANY NETWORK (NGROK TUNNEL)</div>
        <div style="font-size:11px;font-weight:700;color:#555;margin-bottom:10px">Koi bhi network — internet se anywhere</div>
        <div id="tunnelStatus" style="margin-bottom:10px">
          <span class="st st-pending" style="font-size:10px">⏳ CHECKING...</span>
        </div>
        <div id="tunnelUrl" style="margin-bottom:10px"></div>
        <div id="ngrokQR" style="text-align:center;margin-bottom:10px"></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button onclick="startTunnel()" id="startTunnelBtn" class="btn btn-green btn-sm" style="flex:1">🚀 START TUNNEL</button>
          <button onclick="stopTunnel()" id="stopTunnelBtn" class="btn btn-red btn-sm" style="flex:1;display:none">⏹ STOP</button>
        </div>
        <div style="font-size:9.5px;color:#888;font-weight:700;margin-top:10px">
          <b style="color:#1a1a2e">Ngrok install nahi?</b><br>
          1. <a href="https://ngrok.com/download" target="_blank" style="color:#4455ff">ngrok.com/download</a> se download karo<br>
          2. <code style="background:#f5fff9;padding:1px 5px;border-radius:3px">ngrok.exe</code> ko same folder mein rakh do<br>
          3. START TUNNEL click karo ✅
        </div>
      </div>

    </div>
  </div>
</div>
<!-- ── TUNNEL PANEL ── -->
<div id="tunnelPanel" style="display:none;background:#f0fff8;border-bottom:2px solid #99ddbb;padding:16px 24px;box-shadow:0 2px 8px rgba(0,0,0,.07)">
  <div style="font-size:11px;font-weight:900;color:#22aa66;letter-spacing:2px;margin-bottom:10px">🌐 PUBLIC TUNNEL — ACCESS FROM ANY NETWORK</div>

  <div id="tunnelIdle">
    <div style="font-size:12px;font-weight:700;color:#555;margin-bottom:12px">
      Cloudflare Tunnel — free, no account needed, any network se access hoga.<br>
      <b style="color:#cc3333">Step 1:</b> <a href="https://github.com/cloudflare/cloudflared/releases/latest" target="_blank" style="color:#4455ff;font-weight:900">cloudflared.exe download karo</a> aur <b>server.py ke saath same folder mein rakho</b><br>
      <b style="color:#22aa66">Step 2:</b> Neeche "START TUNNEL" button dabao
    </div>
    <button class="btn btn-green" onclick="startTunnel()" style="font-size:11px;padding:8px 20px">⚡ START TUNNEL</button>
  </div>

  <div id="tunnelStarting" style="display:none">
    <div style="font-size:13px;font-weight:900;color:#cc8800;margin-bottom:8px">⏳ Tunnel start ho raha hai... (10-20 seconds)</div>
    <div class="prog-bar-bg" style="max-width:300px"><div class="prog-bar" id="tunnelProgBar" style="width:0%"></div></div>
  </div>

  <div id="tunnelRunning" style="display:none">
    <div style="font-size:11px;font-weight:900;color:#22aa66;letter-spacing:1px;margin-bottom:6px">✅ TUNNEL ACTIVE — Share this URL:</div>
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px">
      <a id="tunnelUrl" href="#" target="_blank" style="font-size:17px;font-weight:900;color:#4455ff;letter-spacing:1px;text-decoration:none;word-break:break-all"></a>
      <button class="btn btn-sm" style="background:#4455ff;padding:7px 14px" onclick="copyTunnelUrl()">📋 COPY</button>
    </div>
    <div id="tunnelQR" style="display:inline-block;padding:10px;background:#fff;border:2px solid #dde1ea;border-radius:10px;margin-bottom:8px"></div>
    <br>
    <button class="btn btn-red btn-sm" onclick="stopTunnel()" style="margin-top:8px">⏹ STOP TUNNEL</button>
  </div>

  <div id="tunnelError" style="display:none">
    <div style="font-size:12px;font-weight:900;color:#cc3333;margin-bottom:8px">❌ Tunnel Error: <span id="tunnelErrMsg"></span></div>
    <div style="font-size:11px;color:#555;font-weight:700;margin-bottom:10px">
      Make sure <b>cloudflared.exe</b> server.py ke saath same folder mein hai.<br>
      Download: <a href="https://github.com/cloudflare/cloudflared/releases/latest" target="_blank" style="color:#4455ff;font-weight:900">github.com/cloudflare/cloudflared/releases</a>
    </div>
    <button class="btn btn-green btn-sm" onclick="startTunnel()">🔄 RETRY</button>
  </div>
</div>

<div class="wrap">

  <!-- ── INPUT PANEL ── -->
  <div class="panel" id="inputPanel">
    <div class="panel-title">📋 ACCOUNTS INPUT</div>
    <div class="fmt">
      <b>NEW FORMAT (6 parts):</b><br>
      <code>uid|fbpass|email|emailpass|refresh_token|client_id</code><br>
      <b>OLD FORMAT (3-4 parts):</b><br>
      <code>email|emailpass|refresh_token</code> &nbsp;·&nbsp; <code>email|emailpass|refresh_token|client_id</code><br>
      <span class="dim">Multiple accounts: har line pe ek account &nbsp;·&nbsp; Ctrl+Enter = Enlist</span>
    </div>
    <textarea id="accts" placeholder="100023212086654|ToiLaToi1YDE|user@hotmail.com|emailpass|M.C532_SN1.0...|client_id&#10;100023212086655|PassWord123|user2@hotmail.com|emailpass2|M.C532_SN1.0..."></textarea>
    <div class="row">
      <select id="lm">
        <option value="10">Last 10 emails</option>
        <option value="20" selected>Last 20 emails</option>
        <option value="50">Last 50 emails</option>
        <option value="100">Last 100 emails</option>
      </select>
      <button class="btn" id="enlistBtn" onclick="enlistAccounts()">📋 ENLIST ACCOUNTS</button>
      <button class="btn" style="background:#22aa66" onclick="loadSaved()">📂 LOAD SAVED</button>
      <button class="btn" style="background:#cc8800" onclick="saveToAdmin()">💾 SAVE DATA</button>
      <span id="savedCountBadge" style="font-size:10px;color:#22aa66;font-weight:900"></span>
    </div>
  </div>

  <!-- ── PROGRESS ── -->
  <div class="prog-wrap" id="progWrap" style="display:none">
    <div class="prog-top">
      <span class="prog-label">⚡ FETCHING OTPs ONE BY ONE...</span>
      <span class="prog-nums" id="progNums">0 / 0</span>
    </div>
    <div class="prog-bar-bg"><div class="prog-bar" id="progBar"></div></div>
    <div class="prog-status" id="progStatus">Starting...</div>
  </div>

  <!-- ── SUMMARY ── -->
  <div class="summary" id="summaryBar" style="display:none"></div>

  <!-- ── DESKTOP TABLE ── -->
  <div id="tableWrap" style="display:none">
    <div class="tbl-desktop">
      <div class="sec-head">
        <span class="sec-title">RESULTS TABLE</span>
      </div>
      <div class="tbl-wrap">
        <div class="tbl-head">
          <div>#</div>
          <div>UID</div>
          <div>EMAIL</div>
          <div>LATEST OTP</div>
          <div>SUBJECT</div>
          <div>DATE</div>
          <div>ACTION</div>
        </div>
        <div id="tableBody"></div>
      </div>
    </div>

    <!-- ── MOBILE CARDS ── -->
    <div class="tbl-mobile">
      <div class="sec-head">
        <span class="sec-title">RESULTS</span>
      </div>
      <div id="mobileBody"></div>
    </div>
  </div>

  <!-- ── ACTION BUTTONS ── -->
  <div class="row" id="actionRow" style="display:none;margin-top:14px">
    <button class="btn btn-green" id="fetchAllBtn" onclick="fetchAllOTPs()">⚡ FETCH ALL OTPs</button>
    <button class="btn btn-grey btn-sm" onclick="resetAll()">🔄 RESET</button>
  </div>

</div>
<div class="toast" id="toast"></div>

<script>
let accounts  = [];
let results   = {};
let isFetching = false;
let stopFlag   = false;

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function showToast(msg, dur=2000){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), dur);
}
function copyText(txt, label){
  if(!txt) return;
  navigator.clipboard.writeText(txt).then(()=>showToast('✓ COPIED: '+(label||txt)));
}

// ── ENLIST ────────────────────────────────────────────────────────────────────
function enlistAccounts(){
  const raw = document.getElementById('accts').value.trim();
  if(!raw){ alert('Accounts paste karo pehle'); return; }
  const lines = raw.split('\n').map(l=>l.trim())
    .filter(l=> l && l.includes('|') && l.split('|').length >= 3);
  if(!lines.length){ alert('Valid format nahi mila'); return; }

  accounts = lines.map((line,i)=>{
    const p = line.split('|');
    let uid='', email='';
    if(p.length >= 5 && !p[0].includes('@')){
      uid=p[0].trim(); email=p[2].trim();
    } else {
      email=p[0].trim();
    }
    return {idx:i, uid, email, line};
  });
  results = {}; stopFlag = false;

  buildDesktopTable();
  buildMobileCards();

  document.getElementById('tableWrap').style.display  = 'block';
  document.getElementById('actionRow').style.display  = 'flex';
  document.getElementById('summaryBar').style.display = 'none';
  document.getElementById('progWrap').style.display   = 'none';

  updateSummary();
  showToast(`✓ ${accounts.length} ACCOUNTS ENLISTED`);
  document.getElementById('tableWrap').scrollIntoView({behavior:'smooth',block:'start'});
}

// ── BUILD DESKTOP TABLE ───────────────────────────────────────────────────────
function buildDesktopTable(){
  document.getElementById('tableBody').innerHTML = accounts.map(a=>`
    <div class="row-item" id="row_${a.idx}">
      <div class="num-cell">${a.idx+1}</div>
      <div class="uid-cell" title="${esc(a.uid)}">${a.uid ? esc(a.uid) : '<span style="color:#ccc">—</span>'}</div>
      <div class="email-cell" title="${esc(a.email)}">${esc(a.email)}</div>
      <div id="otp_${a.idx}" class="otp-none">—</div>
      <div id="subj_${a.idx}" class="subj-cell"></div>
      <div id="date_${a.idx}" class="date-cell"></div>
      <div class="act-cell">
        <button class="fetch-row-btn" id="fetchbtn_${a.idx}" onclick="fetchSingle(${a.idx})">⚡ FETCH OTP</button>
        <button class="copy-otp-btn" id="copybtn_${a.idx}" disabled onclick="copyOTP(${a.idx})">📋 COPY OTP</button>
        <button class="copy-uid-btn" id="copyuidbtn_${a.idx}" ${a.uid?'':'disabled'} onclick="copyUID(${a.idx})">👤 COPY UID|PASS</button>
        <span class="st st-pending" id="st_${a.idx}">PENDING</span>
      </div>
    </div>`).join('');
}

// ── BUILD MOBILE CARDS ────────────────────────────────────────────────────────
function buildMobileCards(){
  document.getElementById('mobileBody').innerHTML = accounts.map(a=>`
    <div class="m-card" id="mrow_${a.idx}">
      <div class="m-top">
        <div class="m-num">${a.idx+1}</div>
        <div class="m-emails">
          ${a.uid ? `<div class="m-uid">👤 ${esc(a.uid)}</div>` : ''}
          <div class="m-email">✉ ${esc(a.email)}</div>
        </div>
        <div class="m-st-wrap"><span class="st st-pending" id="mst_${a.idx}">PENDING</span></div>
      </div>
      <div class="m-otp-row">
        <div style="flex:1">
          <div class="m-otp-lbl">LATEST OTP</div>
          <div id="motp_${a.idx}" class="m-otp-none">—</div>
        </div>
      </div>
      <div class="m-subj" id="msubj_${a.idx}"></div>
      <div class="m-date" id="mdate_${a.idx}"></div>
      <div class="m-btns">
        <button class="fetch-row-btn btn-sm" id="mfetchbtn_${a.idx}" onclick="fetchSingle(${a.idx})" style="flex:1">⚡ FETCH</button>
        <button class="copy-otp-btn btn-sm" id="mcopybtn_${a.idx}" disabled onclick="copyOTP(${a.idx})" style="flex:1">📋 COPY OTP</button>
        <button class="copy-uid-btn btn-sm" id="mcopyuidbtn_${a.idx}" ${a.uid?'':'disabled'} onclick="copyUID(${a.idx})" style="flex:1">👤 UID|PASS</button>
      </div>
    </div>`).join('');
}

// ── FETCH ALL ─────────────────────────────────────────────────────────────────
async function fetchAllOTPs(){
  if(isFetching){ stopFetch(); return; }
  isFetching=true; stopFlag=false;

  const btn = document.getElementById('fetchAllBtn');
  btn.textContent='⏹ STOP FETCH'; btn.classList.replace('btn-green','btn-red');

  const prog=document.getElementById('progWrap');
  const progBar=document.getElementById('progBar');
  const progNums=document.getElementById('progNums');
  const progStat=document.getElementById('progStatus');
  const limit=parseInt(document.getElementById('lm').value);

  prog.style.display='block';
  let done=0;

  for(const acct of accounts){
    if(stopFlag) break;
    setStatus(acct.idx,'fetching','⏳ FETCHING');
    progStat.textContent=`Fetching: ${acct.email} (${done+1}/${accounts.length})`;
    progNums.textContent=`${done} / ${accounts.length}`;
    progBar.style.width=`${Math.round(done/accounts.length*100)}%`;

    try{
      const res = await fetch('/fetch_latest',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({line:acct.line,limit})
      }).then(r=>r.json());

      // Store uid/fbpass from response if available
      if(res.uid) acct.uid=res.uid;
      if(res.fbpass) acct.fbpass=res.fbpass;
      results[acct.idx]=res;

      if(res.error){
        setStatus(acct.idx,'err','❌ ERROR');
        setOTP(acct.idx,null,res.error,'','');
      } else if(res.otp){
        setStatus(acct.idx,'found','✅ FOUND');
        setOTP(acct.idx,res.otp,'',res.subject||'',res.date||'');
      } else {
        setStatus(acct.idx,'none','📭 NO OTP');
        setOTP(acct.idx,null,'','','');
      }
    } catch(e){
      results[acct.idx]={error:e.message};
      setStatus(acct.idx,'err','❌ ERROR');
      setOTP(acct.idx,null,e.message,'','');
    }
    done++;
  }

  progBar.style.width=stopFlag?progBar.style.width:'100%';
  progNums.textContent=`${done} / ${accounts.length}`;
  progStat.textContent=stopFlag?`⏹ Stopped at ${done}/${accounts.length}`:`✅ Done! ${done} accounts processed`;

  isFetching=false;
  btn.textContent='⚡ FETCH ALL OTPs';
  btn.classList.replace('btn-red','btn-green');
  updateSummary();
}

function stopFetch(){
  stopFlag=true; isFetching=false;
  const btn=document.getElementById('fetchAllBtn');
  btn.textContent='⚡ FETCH ALL OTPs';
  btn.classList.replace('btn-red','btn-green');
}

// ── FETCH SINGLE ROW ──────────────────────────────────────────────────────────
async function fetchSingle(idx){
  const acct = accounts[idx];
  if(!acct) return;

  // Disable this row's fetch button while running
  const fb  = document.getElementById(`fetchbtn_${idx}`);
  const mfb = document.getElementById(`mfetchbtn_${idx}`);
  if(fb){  fb.disabled=true;  fb.textContent='⏳ ...'; fb.classList.add('fetching'); }
  if(mfb){ mfb.disabled=true; mfb.textContent='⏳'; mfb.classList.add('fetching'); }

  setStatus(idx,'fetching','⏳ FETCHING');
  const limit = parseInt(document.getElementById('lm').value);

  try{
    const res = await fetch('/fetch_latest',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({line:acct.line, limit})
    }).then(r=>r.json());

    if(res.uid)    acct.uid    = res.uid;
    if(res.fbpass) acct.fbpass = res.fbpass;
    results[idx] = res;

    if(res.error){
      setStatus(idx,'err','❌ ERROR');
      setOTP(idx,null,res.error,'','');
    } else if(res.otp){
      setStatus(idx,'found','✅ FOUND');
      setOTP(idx,res.otp,'',res.subject||'',res.date||'');
    } else {
      setStatus(idx,'none','📭 NO OTP');
      setOTP(idx,null,'','','');
    }
  } catch(e){
    results[idx]={error:e.message};
    setStatus(idx,'err','❌ ERROR');
    setOTP(idx,null,e.message,'','');
  }

  // Re-enable fetch button
  if(fb){  fb.disabled=false; fb.textContent='⚡ FETCH OTP'; fb.classList.remove('fetching'); }
  if(mfb){ mfb.disabled=false; mfb.textContent='⚡ FETCH';   mfb.classList.remove('fetching'); }

  updateSummary();
}

// ── ROW UPDATERS ──────────────────────────────────────────────────────────────
function setStatus(idx,type,label){
  ['st_','mst_'].forEach(pre=>{
    const el=document.getElementById(pre+idx);
    if(el){el.className=`st st-${type}`;el.textContent=label;}
  });
}

function setOTP(idx, otp, errMsg, subject, date){
  // Desktop
  const otpEl  = document.getElementById(`otp_${idx}`);
  const subjEl = document.getElementById(`subj_${idx}`);
  const dateEl = document.getElementById(`date_${idx}`);
  const cpBtn  = document.getElementById(`copybtn_${idx}`);
  const uidBtn = document.getElementById(`copyuidbtn_${idx}`);

  // Mobile
  const motpEl  = document.getElementById(`motp_${idx}`);
  const msubjEl = document.getElementById(`msubj_${idx}`);
  const mdateEl = document.getElementById(`mdate_${idx}`);
  const mcpBtn  = document.getElementById(`mcopybtn_${idx}`);
  const muidBtn = document.getElementById(`mcopyuidbtn_${idx}`);

  const acct = accounts[idx];

  if(otp){
    if(otpEl){otpEl.className='otp-cell';otpEl.textContent=otp;otpEl.onclick=()=>copyOTP(idx);}
    if(motpEl){motpEl.className='m-otp-val';motpEl.textContent=otp;motpEl.onclick=()=>copyOTP(idx);}
    if(cpBtn){cpBtn.disabled=false;}
    if(mcpBtn){mcpBtn.disabled=false;}
  } else if(errMsg){
    if(otpEl){otpEl.className='otp-err';otpEl.textContent=errMsg.slice(0,100);otpEl.onclick=null;}
    if(motpEl){motpEl.className='m-otp-err';motpEl.textContent=errMsg.slice(0,100);motpEl.onclick=null;}
  } else {
    if(otpEl){otpEl.className='otp-none';otpEl.textContent='—';otpEl.onclick=null;}
    if(motpEl){motpEl.className='m-otp-none';motpEl.textContent='—';motpEl.onclick=null;}
  }

  const hasUID = acct && acct.uid;
  if(uidBtn)  uidBtn.disabled  = !hasUID;
  if(muidBtn) muidBtn.disabled = !hasUID;

  if(subjEl)  subjEl.textContent  = subject||'';
  if(msubjEl) msubjEl.textContent = subject ? ('📧 '+subject.slice(0,80)) : '';
  if(dateEl)  dateEl.textContent  = date||'';
  if(mdateEl) mdateEl.textContent = date ? ('🕐 '+date) : '';
}

// ── COPY FUNCTIONS ────────────────────────────────────────────────────────────
function copyOTP(idx){
  const r = results[idx];
  if(r && r.otp) copyText(r.otp, 'OTP '+r.otp);
}

function copyUID(idx){
  const acct = accounts[idx];
  if(!acct || !acct.uid) return;
  // Get fbpass from the original line
  const p = acct.line.split('|');
  const fbpass = p.length >= 5 ? p[1].trim() : '';
  const txt = acct.uid + '|' + fbpass;
  copyText(txt, 'UID|PASS');
}

// ── SUMMARY ───────────────────────────────────────────────────────────────────
function updateSummary(){
  const total   = accounts.length;
  const found   = Object.values(results).filter(r=>r.otp).length;
  const noOTP   = Object.values(results).filter(r=>!r.error&&!r.otp).length;
  const errored = Object.values(results).filter(r=>r.error).length;
  const pending = total - found - noOTP - errored;
  const bar = document.getElementById('summaryBar');
  bar.style.display='flex';
  bar.innerHTML=`
    <div class="chip chip-blue">📋 TOTAL: ${total}</div>
    <div class="chip chip-green">✅ OTP FOUND: ${found}</div>
    <div class="chip chip-grey">📭 NO OTP: ${noOTP}</div>
    <div class="chip chip-red">❌ ERROR: ${errored}</div>
    ${pending>0?`<div class="chip chip-grey">⏳ PENDING: ${pending}</div>`:''}
  `;
}

// ── RESET ─────────────────────────────────────────────────────────────────────
function resetAll(){
  if(isFetching) stopFetch();
  accounts=[]; results={};
  ['tableWrap','actionRow','progWrap','summaryBar'].forEach(id=>{
    document.getElementById(id).style.display='none';
  });
  document.getElementById('accts').value='';
  document.getElementById('accts').focus();
}

document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='Enter')enlistAccounts();});

// ── SAVED ACCOUNTS INTEGRATION ────────────────────────────────────────────────
async function loadSaved(){
  const r = await fetch('/admin/list').then(r=>r.json()).catch(()=>({accounts:[]}));
  const accts = r.accounts || [];
  if(!accts.length){ showToast('📭 Koi saved account nahi hai — Admin se pehle save karo'); return; }
  const lines = accts.map(a=>a.line).join('\n');
  document.getElementById('accts').value = lines;
  showToast(`✅ ${accts.length} saved accounts loaded!`);
  refreshSavedCount();
}

async function saveToAdmin(){
  const raw = document.getElementById('accts').value.trim();
  if(!raw){ showToast('Pehle textarea mein data paste karo'); return; }
  const lines = raw.split('\n').map(l=>l.trim()).filter(l=>l&&l.includes('|')&&l.split('|').length>=3);
  if(!lines.length){ showToast('Valid format nahi mila'); return; }
  const r = await fetch('/admin/save',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({lines})
  }).then(r=>r.json()).catch(()=>({error:'Failed'}));
  if(r.error){ showToast('❌ '+r.error); return; }
  showToast(`💾 ${r.added} new saved! Total: ${r.total}`);
  refreshSavedCount();
}

async function refreshSavedCount(){
  try{
    const r = await fetch('/admin/list').then(r=>r.json());
    const badge = document.getElementById('savedCountBadge');
    if(badge) badge.textContent = r.total>0 ? `(${r.total} saved)` : '';
  } catch{}
}

// Check if redirected from Admin with loaded data
(function(){
  const loaded = sessionStorage.getItem('loadedAccounts');
  if(loaded){ document.getElementById('accts').value=loaded; sessionStorage.removeItem('loadedAccounts'); showToast('✅ Saved accounts loaded from Admin!'); }
  refreshSavedCount();
})();


// ── TUNNEL JS ─────────────────────────────────────────────────────────────────
let tunnelPollTimer = null;
let tunnelProgVal   = 0;

function toggleTunnel(){
  const p = document.getElementById('tunnelPanel');
  const shown = p.style.display !== 'none';
  p.style.display = shown ? 'none' : 'block';
  document.getElementById('tunnelBtn').classList.toggle('active', !shown);
  if(!shown) pollTunnelState();
}

function tunnelShow(section){
  ['tunnelIdle','tunnelStarting','tunnelRunning','tunnelError'].forEach(id=>{
    document.getElementById(id).style.display = id===section ? 'block' : 'none';
  });
}

async function startTunnel(){
  tunnelShow('tunnelStarting');
  tunnelProgVal = 0;
  const bar = document.getElementById('tunnelProgBar');
  // animate progress while waiting
  tunnelPollTimer = setInterval(async ()=>{
    tunnelProgVal = Math.min(tunnelProgVal + 3, 90);
    bar.style.width = tunnelProgVal + '%';
    const state = await fetch('/tunnel_state').then(r=>r.json());
    if(state.status === 'running'){
      clearInterval(tunnelPollTimer);
      bar.style.width = '100%';
      showTunnelRunning(state.public_url);
    } else if(state.status === 'error'){
      clearInterval(tunnelPollTimer);
      document.getElementById('tunnelErrMsg').textContent = state.error;
      tunnelShow('tunnelError');
    }
  }, 800);

  await fetch('/tunnel_start', {method:'POST'});
}

function showTunnelRunning(url){
  tunnelShow('tunnelRunning');
  document.getElementById('tunnelUrl').href = url;
  document.getElementById('tunnelUrl').textContent = url;
  document.getElementById('tunnelBtn').classList.add('active');
  // QR
  const qrUrl = `https://chart.googleapis.com/chart?chs=160x160&cht=qr&chl=${encodeURIComponent(url)}&choe=UTF-8`;
  document.getElementById('tunnelQR').innerHTML = `<img src="${qrUrl}" style="width:160px;height:160px;display:block" alt="QR">`;
}

function copyTunnelUrl(){
  const url = document.getElementById('tunnelUrl').textContent;
  navigator.clipboard.writeText(url).then(()=>showToast('✓ TUNNEL URL COPIED'));
}

async function stopTunnel(){
  clearInterval(tunnelPollTimer);
  await fetch('/tunnel_stop', {method:'POST'});
  tunnelShow('tunnelIdle');
  document.getElementById('tunnelBtn').classList.remove('active');
}

async function pollTunnelState(){
  const state = await fetch('/tunnel_state').then(r=>r.json()).catch(()=>({status:'idle'}));
  if(state.status === 'running')       showTunnelRunning(state.public_url);
  else if(state.status === 'starting') tunnelShow('tunnelStarting');
  else if(state.status === 'error'){
    document.getElementById('tunnelErrMsg').textContent = state.error;
    tunnelShow('tunnelError');
  } else tunnelShow('tunnelIdle');
}


// ── NETWORK / QR ───────────────────────────────────────────────────────────────
let qrGenerated = false;
function toggleQR(){
  const panel = document.getElementById('qrPanel');
  const shown = panel.style.display !== 'none';
  panel.style.display = shown ? 'none' : 'block';
  if(!shown && !qrGenerated){
    generateQR();
    qrGenerated = true;
  }
}

function generateQR(){
  // Get server IP from meta tag injected by server
  const ip   = document.getElementById('serverIp').content;
  const port = document.getElementById('serverPort').content;
  const url  = `http://${ip}:${port}/`;
  document.getElementById('netUrl').href = url;
  document.getElementById('netUrl').textContent = url;

  // Build QR using google charts API (no install needed)
  const qrUrl = `https://chart.googleapis.com/chart?chs=180x180&cht=qr&chl=${encodeURIComponent(url)}&choe=UTF-8`;
  const img = document.createElement('img');
  img.src = qrUrl;
  img.style.cssText = 'width:180px;height:180px;display:block';
  img.alt = 'QR Code';
  img.onerror = () => {
    // Fallback: show plain URL if google charts fails
    document.getElementById('qrcode').innerHTML =
      `<div style="font-size:11px;color:#888;font-weight:700">QR load nahi hua.<br>URL copy karo aur mobile mein kholo.</div>`;
  };
  document.getElementById('qrcode').innerHTML = '';
  document.getElementById('qrcode').appendChild(img);
}
</script>
</body></html>"""


# ─── Cloudflare Tunnel Manager ────────────────────────────────────────────────
class TunnelManager:
    proc       = None
    public_url = None
    status     = "idle"
    error_msg  = ""

    @classmethod
    def find_cloudflared(cls):
        base = os.path.dirname(os.path.abspath(__file__))
        for name in ["cloudflared.exe","cloudflared"]:
            for path in [os.path.join(base,name), name]:
                try:
                    r = subprocess.run([path,"--version"],capture_output=True,timeout=5)
                    if r.returncode == 0: return path
                except: pass
        return None

    @classmethod
    def start(cls):
        if cls.status == "running": return
        cls.status = "starting"; cls.public_url = None; cls.error_msg = ""
        def run():
            binary = cls.find_cloudflared()
            if not binary:
                cls.status = "error"; cls.error_msg = "cloudflared.exe not found in server folder"; return
            try:
                cls.proc = subprocess.Popen(
                    [binary,"tunnel","--url",f"http://localhost:{port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                for line in cls.proc.stdout:
                    m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                    if m:
                        cls.public_url = m.group(0); cls.status = "running"
                    if cls.proc.poll() is not None: break
                if cls.status != "running":
                    cls.status = "error"; cls.error_msg = "Tunnel failed — check cloudflared.exe"
            except Exception as e:
                cls.status = "error"; cls.error_msg = str(e)
        threading.Thread(target=run, daemon=True).start()

    @classmethod
    def stop(cls):
        if cls.proc: cls.proc.terminate(); cls.proc = None
        cls.status = "idle"; cls.public_url = None

    @classmethod
    def state(cls):
        return {"status": cls.status, "public_url": cls.public_url, "error": cls.error_msg}


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass

    def is_authed(self):
        for p in self.headers.get("Cookie","").split(";"):
            if p.strip().startswith("sess=") and p.strip().split("=",1)[1] in SESSIONS:
                return True
        return False

    def send_html(self, html, code=200, extra=None):
        b = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(b))
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)

    def send_json(self, obj):
        b = __import__('json').dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(b))
        self.end_headers(); self.wfile.write(b)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length",0)))

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/logout":
            for part in self.headers.get("Cookie","").split(";"):
                if part.strip().startswith("sess="):
                    SESSIONS.discard(part.strip().split("=",1)[1])
            self.send_html("",302,{"Location":"/","Set-Cookie":"sess=; Max-Age=0; Path=/"})
            return
        if p == "/tunnel_state":
            if not self.is_authed():
                self.send_json({"status":"idle"}); return
            self.send_json(TunnelManager.state())
            return

        if p == "/admin/list":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            self.send_json({"accounts": AccountStore.all(), "total": AccountStore.count()})
            return

        if p == "/admin":
            if not self.is_authed():
                self.send_html(LOGIN_HTML.format(err="")); return
            html = ADMIN_HTML.replace("__SERVER_IP__", LOCAL_IP).replace("__SERVER_PORT__", str(port))
            self.send_html(html)
            return

        if not self.is_authed():
            self.send_html(LOGIN_HTML.format(err="")); return
        # Inject real server IP and port into HTML
        html = APP_HTML.replace("__SERVER_IP__", LOCAL_IP).replace("__SERVER_PORT__", str(port))
        self.send_html(html)

    def do_POST(self):
        p = urlparse(self.path).path

        if p == "/login":
            params = parse_qs(self.body().decode())
            pw = params.get("site_pass",[""])[0]
            if pw == CONFIG["site_password"]:
                tok = __import__('hashlib').sha256(__import__('os').urandom(32)).hexdigest()
                SESSIONS.add(tok)
                self.send_html("",302,{"Location":"/",
                    "Set-Cookie":f"sess={tok}; Path=/; HttpOnly"})
            else:
                self.send_html(LOGIN_HTML.format(
                    err='<div class="err">❌ Wrong password</div>'))
            return

        if p == "/fetch_latest":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            try:
                payload = __import__('json').loads(self.body())
                line    = payload.get("line","").strip()
                limit   = int(payload.get("limit",20))

                acct = parse_account_line(line)
                if not acct:
                    self.send_json({"error":"Invalid format",
                        "email": line.split("|")[0] if "|" in line else line}); return

                res, err = fetch_latest_otp(acct, limit)
                if err:
                    self.send_json({"error": err, "email": acct["email"]}); return

                self.send_json({
                    "email":    acct["email"],
                    "uid":      acct.get("uid",""),
                    "fbpass":   acct.get("fbpass",""),
                    "otp":      res.get("otp"),
                    "subject":  res.get("subject",""),
                    "sender":   res.get("sender",""),
                    "date":     res.get("date",""),
                    "endpoint": res.get("endpoint",""),
                    "client":   res.get("client",""),
                })
            except Exception as e:
                self.send_json({"error": str(e), "email":"?"})
            return

        if p == "/api/tunnel-status":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            self.send_json({
                "status":     NgrokTunnel.status or "idle",
                "url":        NgrokTunnel.url or "",
                "local_ip":   LOCAL_IP,
                "port":       port,
            }); return

        if p == "/api/tunnel-start":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            NgrokTunnel.start(port)
            self.send_json({"ok": True, "msg": "Starting..."}); return

        if p == "/api/tunnel-stop":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            NgrokTunnel.stop()
            self.send_json({"ok": True}); return

        if p == "/tunnel_start":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            TunnelManager.start()
            self.send_json({"ok": True})
            return

        if p == "/tunnel_stop":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            TunnelManager.stop()
            self.send_json({"ok": True})
            return

        # ── ADMIN: Save accounts ──────────────────────────────────────────
        if p == "/admin/save":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            try:
                payload = json.loads(self.body())
                lines   = payload.get("lines", [])
                added   = AccountStore.add_lines(lines)
                total   = AccountStore.count()
                self.send_json({"ok": True, "added": added, "total": total})
            except Exception as e:
                self.send_json({"error": str(e)})
            return

        if p == "/admin/delete":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            try:
                payload = json.loads(self.body())
                acc_id  = payload.get("id","")
                deleted = AccountStore.delete_by_id(acc_id)
                self.send_json({"ok": True, "deleted": deleted, "total": AccountStore.count()})
            except Exception as e:
                self.send_json({"error": str(e)})
            return

        if p == "/admin/delete_all":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            AccountStore.delete_all()
            self.send_json({"ok": True, "total": 0})
            return

        self.send_html("Not found",404)


ADMIN_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — OTP Reader</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f0f2f8;color:#1a1a2e;font-family:'Courier New',monospace;font-weight:700;min-height:100vh}
header{background:#1a1a2e;border-bottom:2px solid #333;padding:13px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.brand{color:#fff;font-size:14px;font-weight:900;letter-spacing:3px}
.hright{display:flex;gap:12px;align-items:center}
.abtn{padding:6px 14px;border-radius:7px;font-size:11px;font-family:inherit;cursor:pointer;font-weight:900;letter-spacing:1px;border:none;transition:all .2s}
.abtn-blue{background:#4455ff;color:#fff}.abtn-blue:hover{background:#3344dd}
.abtn-red{background:#cc3333;color:#fff}.abtn-red:hover{background:#aa2222}
.abtn-grey{background:#555;color:#fff}.abtn-grey:hover{background:#333}
.abtn-green{background:#22aa66;color:#fff}.abtn-green:hover{background:#1a8850}
.wrap{max-width:1100px;margin:0 auto;padding:22px 16px}
.panel{background:#fff;border:2px solid #dde1ea;border-radius:14px;padding:22px;margin-bottom:18px;box-shadow:0 2px 12px rgba(0,0,0,.05)}
.panel-title{color:#4455ff;font-size:10px;letter-spacing:3px;font-weight:900;margin-bottom:14px;text-transform:uppercase}

/* Stats row */
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.stat-box{background:#fff;border:2px solid #dde1ea;border-radius:12px;padding:16px 22px;text-align:center;flex:1;min-width:120px}
.stat-num{font-size:32px;font-weight:900;color:#4455ff;line-height:1}
.stat-lbl{font-size:9px;color:#aaa;letter-spacing:2px;margin-top:4px;text-transform:uppercase}

/* Add accounts area */
textarea{width:100%;height:90px;background:#f9fafc;border:2px solid #dde1ea;border-radius:8px;color:#1a1a2e;font-size:11.5px;font-family:inherit;font-weight:700;padding:11px;resize:vertical;outline:none;transition:border-color .2s}
textarea:focus{border-color:#4455ff}
textarea::placeholder{color:#ccc}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}

/* Search */
.search-box{width:100%;padding:10px 14px;background:#f9fafc;border:2px solid #dde1ea;border-radius:8px;color:#1a1a2e;font-size:12px;font-family:inherit;font-weight:700;outline:none;margin-bottom:14px;transition:border-color .2s}
.search-box:focus{border-color:#4455ff}

/* Table */
.tbl{width:100%;border-collapse:collapse;font-size:11px}
.tbl th{background:#f0f2ff;color:#4455ff;font-size:9px;letter-spacing:2px;text-transform:uppercase;padding:10px 12px;text-align:left;border-bottom:2px solid #dde1ea;position:sticky;top:60px;z-index:10}
.tbl td{padding:10px 12px;border-bottom:1px solid #f0f1f6;vertical-align:middle}
.tbl tr:hover td{background:#fafbff}
.tbl tr:last-child td{border-bottom:none}
.email-td{font-weight:900;color:#1a1a2e;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.uid-td{color:#4455ff;font-weight:900;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.date-td{color:#aaa;font-size:10px;white-space:nowrap}
.line-td{color:#888;font-size:9.5px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.line-td:hover{color:#4455ff}
.del-btn{padding:4px 10px;background:#fff5f5;border:1.5px solid #ffcccc;border-radius:5px;color:#cc3333;font-size:9.5px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap}
.del-btn:hover{background:#cc3333;color:#fff}

/* Bulk actions */
.bulk-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:12px 16px;background:#fff8f8;border:2px solid #ffcccc;border-radius:10px;margin-bottom:14px}
.sel-count{color:#cc3333;font-size:11px;font-weight:900}
.chk-all{accent-color:#4455ff;width:15px;height:15px;cursor:pointer}

/* Pagination */
.pager{display:flex;gap:8px;align-items:center;margin-top:14px;flex-wrap:wrap}
.page-btn{padding:5px 12px;background:#f0f2ff;border:1.5px solid #c0c8ff;border-radius:6px;color:#4455ff;font-size:10.5px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s}
.page-btn:hover{background:#4455ff;color:#fff}
.page-btn.active{background:#4455ff;color:#fff}
.page-info{color:#888;font-size:10.5px;font-weight:700}

/* Toast */
.toast{position:fixed;bottom:20px;right:20px;background:#1a1a2e;color:#fff;padding:11px 22px;border-radius:10px;font-size:12px;font-weight:900;opacity:0;transition:opacity .3s;pointer-events:none;box-shadow:0 4px 20px rgba(0,0,0,.25);z-index:999}
.toast.show{opacity:1}

/* Empty state */
.empty{text-align:center;padding:50px 0;color:#bbb;font-size:13px;font-weight:700}
</style></head>
<body>
<header>
  <span class="brand">🛡️ ADMIN PANEL</span>
  <div class="hright">
    <a href="/" style="color:#aaa;font-size:11px;font-weight:900;text-decoration:none">← BACK TO APP</a>
    <a href="/logout" style="color:#cc3333;font-size:11px;font-weight:900;text-decoration:none;border:1.5px solid #cc3333;padding:4px 10px;border-radius:6px">LOGOUT</a>
  </div>
</header>
<div class="wrap">

  <!-- Stats -->
  <div class="stats" id="statsRow">
    <div class="stat-box"><div class="stat-num" id="statTotal">—</div><div class="stat-lbl">Total Accounts</div></div>
    <div class="stat-box"><div class="stat-num" id="statSelected" style="color:#cc8800">0</div><div class="stat-lbl">Selected</div></div>
    <div class="stat-box"><div class="stat-num" id="statFiltered" style="color:#22aa66">—</div><div class="stat-lbl">Showing</div></div>
  </div>

  <!-- Add Accounts -->
  <div class="panel">
    <div class="panel-title">➕ ADD NEW ACCOUNTS</div>
    <textarea id="addInput" placeholder="uid|fbpass|email|emailpass|refresh_token|client_id&#10;Ya old format: email|emailpass|refresh_token&#10;&#10;Multiple accounts: har line pe ek"></textarea>
    <div class="row">
      <button class="abtn abtn-green" onclick="saveAccounts()">💾 SAVE ACCOUNTS</button>
      <span id="saveMsg" style="font-size:11px;color:#22aa66;font-weight:900"></span>
    </div>
  </div>

  <!-- Accounts Table -->
  <div class="panel">
    <div class="panel-title">📋 SAVED ACCOUNTS</div>
    <input class="search-box" id="searchBox" placeholder="🔍 Search by email or UID..." oninput="applyFilter()">

    <!-- Bulk actions bar -->
    <div class="bulk-bar" id="bulkBar" style="display:none">
      <span class="sel-count" id="selCount">0 selected</span>
      <button class="abtn abtn-red" style="font-size:10px;padding:5px 12px" onclick="deleteSelected()">🗑️ DELETE SELECTED</button>
      <button class="abtn abtn-grey" style="font-size:10px;padding:5px 12px" onclick="clearSelection()">✕ CLEAR</button>
    </div>

    <div style="overflow-x:auto">
      <table class="tbl">
        <thead>
          <tr>
            <th><input type="checkbox" class="chk-all" id="checkAll" onchange="toggleAll(this)"></th>
            <th>#</th>
            <th>EMAIL</th>
            <th>UID</th>
            <th>LINE PREVIEW</th>
            <th>ADDED</th>
            <th>ACTION</th>
          </tr>
        </thead>
        <tbody id="tblBody"></tbody>
      </table>
    </div>
    <div class="empty" id="emptyMsg" style="display:none">📭 No accounts saved yet. Add some above!</div>

    <!-- Pager -->
    <div class="pager" id="pager"></div>

    <!-- Delete all -->
    <div style="margin-top:16px;padding-top:14px;border-top:1.5px solid #f0f1f6;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="abtn abtn-red" onclick="deleteAll()">🗑️ DELETE ALL ACCOUNTS</button>
      <button class="abtn abtn-blue" onclick="loadToApp()">⚡ LOAD ALL TO APP</button>
      <span style="font-size:10px;color:#aaa;font-weight:700">Load to App = saved accounts textarea mein paste ho jayenge</span>
    </div>
  </div>

</div>
<div class="toast" id="toast"></div>

<script>
let ALL_ACCOUNTS = [];
let FILTERED     = [];
let SELECTED     = new Set();
const PAGE_SIZE  = 50;
let currentPage  = 1;

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, dur=2200){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),dur);
}

// ── Load accounts from server ─────────────────────────────────────────────────
async function loadAccounts(){
  const r = await fetch('/admin/list').then(r=>r.json());
  ALL_ACCOUNTS = r.accounts || [];
  applyFilter();
  updateStats();
}

function applyFilter(){
  const q = document.getElementById('searchBox').value.trim().toLowerCase();
  FILTERED = q
    ? ALL_ACCOUNTS.filter(a=>
        (a.email||'').toLowerCase().includes(q) ||
        (a.uid||'').toLowerCase().includes(q))
    : [...ALL_ACCOUNTS];
  currentPage = 1;
  renderPage();
  updateStats();
}

function renderPage(){
  const tbody   = document.getElementById('tblBody');
  const empty   = document.getElementById('emptyMsg');
  const start   = (currentPage-1)*PAGE_SIZE;
  const slice   = FILTERED.slice(start, start+PAGE_SIZE);

  if(!FILTERED.length){
    tbody.innerHTML=''; empty.style.display='block';
    document.getElementById('pager').innerHTML=''; return;
  }
  empty.style.display='none';

  tbody.innerHTML = slice.map((a,i)=>`
    <tr id="row_${a.id}">
      <td><input type="checkbox" class="chk-all" data-id="${a.id}"
          ${SELECTED.has(a.id)?'checked':''} onchange="toggleSel('${a.id}',this)"></td>
      <td style="color:#aaa;font-size:11px">${start+i+1}</td>
      <td class="email-td" title="${esc(a.email)}">${esc(a.email)}</td>
      <td class="uid-td">${a.uid ? esc(a.uid) : '<span style="color:#ddd">—</span>'}</td>
      <td class="line-td" title="${esc(a.line)}" onclick="copyLine('${a.id}')">${esc((a.line||'').slice(0,60))}...</td>
      <td class="date-td">${esc(a.added_at||'')}</td>
      <td>
        <button class="del-btn" onclick="deleteSingle('${a.id}')">🗑️ DEL</button>
      </td>
    </tr>`).join('');

  renderPager();
}

function renderPager(){
  const total = FILTERED.length;
  const pages = Math.ceil(total/PAGE_SIZE);
  const pager = document.getElementById('pager');
  if(pages<=1){pager.innerHTML='';return;}
  let html = `<span class="page-info">${total} accounts · Page ${currentPage}/${pages}</span>`;
  if(currentPage>1) html+=`<button class="page-btn" onclick="goPage(${currentPage-1})">← PREV</button>`;
  for(let p=Math.max(1,currentPage-2);p<=Math.min(pages,currentPage+2);p++)
    html+=`<button class="page-btn ${p===currentPage?'active':''}" onclick="goPage(${p})">${p}</button>`;
  if(currentPage<pages) html+=`<button class="page-btn" onclick="goPage(${currentPage+1})">NEXT →</button>`;
  pager.innerHTML=html;
}

function goPage(p){ currentPage=p; renderPage(); }

// ── Stats ─────────────────────────────────────────────────────────────────────
function updateStats(){
  document.getElementById('statTotal').textContent    = ALL_ACCOUNTS.length;
  document.getElementById('statFiltered').textContent = FILTERED.length;
  document.getElementById('statSelected').textContent = SELECTED.size;
  const bulk = document.getElementById('bulkBar');
  bulk.style.display = SELECTED.size>0 ? 'flex' : 'none';
  document.getElementById('selCount').textContent = `${SELECTED.size} selected`;
}

// ── Selection ─────────────────────────────────────────────────────────────────
function toggleSel(id, el){
  if(el.checked) SELECTED.add(id); else SELECTED.delete(id);
  updateStats();
}

function toggleAll(el){
  FILTERED.forEach(a=> el.checked ? SELECTED.add(a.id) : SELECTED.delete(a.id));
  renderPage(); updateStats();
}

function clearSelection(){
  SELECTED.clear(); renderPage(); updateStats();
}

// ── Save ──────────────────────────────────────────────────────────────────────
async function saveAccounts(){
  const raw = document.getElementById('addInput').value.trim();
  if(!raw){ alert('Koi data nahi hai'); return; }
  const lines = raw.split('\n').map(l=>l.trim()).filter(l=>l&&l.includes('|'));
  if(!lines.length){ alert('Valid format nahi mila'); return; }
  const r = await fetch('/admin/save',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({lines})
  }).then(r=>r.json());
  if(r.error){ toast('❌ '+r.error); return; }
  document.getElementById('addInput').value='';
  document.getElementById('saveMsg').textContent=`✅ ${r.added} added · Total: ${r.total}`;
  setTimeout(()=>document.getElementById('saveMsg').textContent='',3000);
  toast(`✅ ${r.added} new accounts saved! Total: ${r.total}`);
  await loadAccounts();
}

// ── Delete ────────────────────────────────────────────────────────────────────
async function deleteSingle(id){
  if(!confirm('Delete this account?')) return;
  const r = await fetch('/admin/delete',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id})
  }).then(r=>r.json());
  if(r.ok){ toast('🗑️ Deleted · Remaining: '+r.total); SELECTED.delete(id); await loadAccounts(); }
  else toast('❌ '+r.error);
}

async function deleteSelected(){
  if(!SELECTED.size){ alert('Koi account select nahi hai'); return; }
  if(!confirm(`${SELECTED.size} accounts delete karo?`)) return;
  for(const id of [...SELECTED]){
    await fetch('/admin/delete',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id})
    });
    SELECTED.delete(id);
  }
  toast(`🗑️ ${SELECTED.size} accounts deleted`);
  await loadAccounts();
}

async function deleteAll(){
  if(!confirm('⚠️ SAARE accounts delete ho jayenge! Sure?')) return;
  if(!confirm('Bilkul sure? Ye undo nahi hoga!')) return;
  await fetch('/admin/delete_all',{method:'POST'});
  SELECTED.clear();
  toast('🗑️ All accounts deleted');
  await loadAccounts();
}

// ── Load to App ───────────────────────────────────────────────────────────────
function loadToApp(){
  if(!ALL_ACCOUNTS.length){ alert('Koi saved account nahi hai'); return; }
  const lines = ALL_ACCOUNTS.map(a=>a.line).join('\n');
  // Store in sessionStorage for app page to pick up
  sessionStorage.setItem('loadedAccounts', lines);
  toast('✅ App pe redirect ho raha hai...');
  setTimeout(()=>{ window.location.href='/'; }, 800);
}

// ── Copy line ─────────────────────────────────────────────────────────────────
function copyLine(id){
  const a = ALL_ACCOUNTS.find(x=>x.id===id);
  if(!a) return;
  navigator.clipboard.writeText(a.line).then(()=>toast('📋 Line copied!'));
}

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── Init ──────────────────────────────────────────────────────────────────────
loadAccounts();
</script>
</body></html>"""


port = 5000   # global — used in do_GET for URL injection

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"""
╔══════════════════════════════════════════════════════════╗
║   Hotmail OTP Reader v3.0 — Server Started               ║
╠══════════════════════════════════════════════════════════╣
║  URL    : http://localhost:{port}                          ║
║  NETWORK: http://{LOCAL_IP}:{port} (Android/other PC)  ║
║  Pass   : {CONFIG['site_password']:<46}  ║
╠══════════════════════════════════════════════════════════╣
║  New in v3:                                              ║
║  ✅ Enlist all accounts first (table view)               ║
║  ✅ Fetch OTP one-by-one sequentially                    ║
║  ✅ Latest OTP per account                               ║
║  ✅ Live progress bar                                    ║
║  ✅ Copy button per row                                  ║
╚══════════════════════════════════════════════════════════╝
Ctrl+C to stop.
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
