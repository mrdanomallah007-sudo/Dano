#!/usr/bin/env python3
"""
Hotmail OTP Reader v3.8.7
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


# ─── Distribution / Token Store ───────────────────────────────────────────────
DIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "distributions.json")

class DistributionStore:
    """Manage user tokens and their assigned accounts."""
    _lock = threading.Lock()

    @classmethod
    def _load(cls):
        try:
            with open(DIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: return []

    @classmethod
    def _save(cls, data):
        with open(DIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def all(cls):
        with cls._lock: return cls._load()

    @classmethod
    def active(cls):
        """Return non-expired distributions."""
        now = _time.time()
        with cls._lock:
            data = cls._load()
            return [d for d in data if d.get("expires_at") is None or d["expires_at"] > now]

    @classmethod
    def get_by_token(cls, token):
        """Get distribution by token (None if not found or expired)."""
        now = _time.time()
        with cls._lock:
            for d in cls._load():
                if d["token"] == token:
                    if d.get("expires_at") and d["expires_at"] < now:
                        return None  # expired
                    return d
        return None

    @classmethod
    def create(cls, distributions: list) -> list:
        """
        Create new distributions. Each item: {name, account_ids, expires_at}
        Clears previous distributions first (new set each time).
        Returns list of created tokens.
        """
        import secrets
        now = _time.time()
        new_dists = []
        for item in distributions:
            token = secrets.token_urlsafe(16)
            new_dists.append({
                "token":       token,
                "user_name":   item["name"],
                "account_ids": item["account_ids"],
                "created_at":  now,
                "expires_at":  item.get("expires_at"),   # None = permanent
                "count":       len(item["account_ids"]),
            })
        with cls._lock:
            cls._save(new_dists)
        return new_dists

    @classmethod
    def revoke(cls, token):
        with cls._lock:
            data = [d for d in cls._load() if d["token"] != token]
            cls._save(data)

    @classmethod
    def clear_all(cls):
        with cls._lock: cls._save([])




# ─── User Token Store ─────────────────────────────────────────────────────────
import secrets, datetime as _dt

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_tokens.json")

class TokenStore:
    """Manage user distribution tokens."""
    _lock = threading.Lock()

    @classmethod
    def _load(cls):
        try:
            with open(TOKEN_FILE,"r",encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d,list) else []
        except: return []

    @classmethod
    def _save(cls, data):
        with open(TOKEN_FILE,"w",encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def all(cls):
        with cls._lock: return cls._load()

    @classmethod
    def get_by_token(cls, token):
        for t in cls._load():
            if t.get("token") == token:
                return t
        return None

    @classmethod
    def is_valid(cls, token):
        t = cls.get_by_token(token)
        if not t: return False, "Token not found"
        if t.get("expiry_type") == "permanent": return True, t
        exp = t.get("expires_at","")
        if not exp: return True, t
        try:
            exp_dt = _dt.datetime.fromisoformat(exp)
            if _dt.datetime.now() > exp_dt:
                return False, "Token expired"
        except: pass
        return True, t

    @classmethod
    def create(cls, user_name, account_ids, expiry_type="48h"):
        with cls._lock:
            data = cls._load()
            token = secrets.token_urlsafe(20)
            now   = _dt.datetime.now()
            if expiry_type == "48h":
                expires_at = (now + _dt.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
            elif expiry_type == "7d":
                expires_at = (now + _dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
            else:
                expires_at = None  # permanent
            entry = {
                "token":       token,
                "user_name":   user_name,
                "account_ids": account_ids,
                "created_at":  now.strftime("%Y-%m-%d %H:%M"),
                "expires_at":  expires_at,
                "expiry_type": expiry_type,
            }
            data.append(entry)
            cls._save(data)
            return entry

    @classmethod
    def delete(cls, token):
        with cls._lock:
            data = cls._load()
            new  = [t for t in data if t.get("token") != token]
            cls._save(new)
            return len(data) - len(new)

    @classmethod
    def delete_all(cls):
        with cls._lock: cls._save([])

    @classmethod
    def refresh_token(cls, old_token, account_ids=None, expiry_type=None):
        """Regenerate token string + optionally new accounts + reset expiry."""
        with cls._lock:
            data  = cls._load()
            entry = next((t for t in data if t.get("token")==old_token), None)
            if not entry: return None
            new_tok = secrets.token_urlsafe(20)
            entry["token"] = new_tok
            if account_ids is not None:
                entry["account_ids"] = account_ids
            if expiry_type is not None:
                entry["expiry_type"] = expiry_type
            now = _dt.datetime.now()
            entry["created_at"] = now.strftime("%Y-%m-%d %H:%M")
            et = entry["expiry_type"]
            if et == "48h":
                entry["expires_at"] = (now + _dt.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
            elif et == "7d":
                entry["expires_at"] = (now + _dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
            else:
                entry["expires_at"] = None
            cls._save(data)
            return entry


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


# ─── TOTP 2FA Generator ────────────────────────────────────────────────────────
import hmac, struct, base64, time as _time_mod, hashlib as _hashlib

def generate_totp(secret_key: str, digits: int = 6, period: int = 30) -> dict:
    """Generate 6-digit TOTP from base32 secret key (Google Authenticator style)."""
    try:
        # Clean key — remove spaces, dashes, uppercase
        key = secret_key.strip().upper().replace(" ", "").replace("-", "")
        # Pad if needed
        pad = len(key) % 8
        if pad: key += "=" * (8 - pad)
        key_bytes = base64.b32decode(key)

        # TOTP counter
        ts       = int(_time_mod.time())
        counter  = ts // period
        remaining = period - (ts % period)

        # HMAC-SHA1
        msg    = struct.pack(">Q", counter)
        h      = hmac.new(key_bytes, msg, _hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code   = struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF
        otp    = str(code % (10 ** digits)).zfill(digits)

        return {"otp": otp, "remaining": remaining, "ok": True}
    except Exception as e:
        return {"error": str(e), "ok": False}


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
  grid-template-columns:28px 36px 52px minmax(140px,1fr) 110px minmax(120px,1.2fr) 86px 130px;
  background:#f0f2ff;border-bottom:2px solid #dde1ea;
  padding:10px 14px;font-size:9px;color:#4455ff;letter-spacing:2px;font-weight:900;text-transform:uppercase
}
.row-item{
  display:grid;
  grid-template-columns:28px 36px 52px minmax(140px,1fr) 110px minmax(120px,1.2fr) 86px 130px;
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
    <span class="ver">V3.7</span>
    <button class="net-btn" onclick="toggleQR()" title="Same WiFi access">📱 WIFI</button>
    <button class="net-btn" onclick="toggle2FAPanel()" id="btn2FA" title="Generate 2FA TOTP code from secret key" style="background:#fff0ff;border-color:#ddaaff;color:#8833bb">🔑 GET 2FA</button>
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


<!-- ── 2FA PANEL ── -->
<div id="panel2FA" style="display:none;background:#fff5ff;border-bottom:2px solid #ddaaff;padding:16px 24px;box-shadow:0 2px 8px rgba(0,0,0,.07)">
  <div style="max-width:900px;margin:0 auto">
    <div style="font-size:11px;font-weight:900;color:#8833bb;letter-spacing:2px;margin-bottom:12px">🔑 2FA CODE GENERATOR — 6-DIGIT TOTP FROM SECRET KEY</div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
      <input id="tfaKeyInput" type="text" placeholder="Paste 2FA secret key here (e.g. U3KOCP2GHFNBXHLULXOJ)"
        style="flex:1;min-width:220px;padding:10px 14px;background:#fff;border:2px solid #ddaaff;border-radius:8px;color:#1a1a2e;font-size:12px;font-family:inherit;font-weight:700;outline:none"
        onkeydown="if(event.key==='Enter')gen2FA()">
      <button class="btn" onclick="gen2FA()" style="background:#8833bb;padding:10px 20px;white-space:nowrap">⚡ GENERATE</button>
      <button class="btn btn-grey btn-sm" onclick="clearTFA()">✕ CLEAR</button>
    </div>
    <div id="tfaResult" style="display:none;background:#fff;border:2px solid #ddaaff;border-radius:10px;padding:14px 20px;display:flex;align-items:center;gap:20px;flex-wrap:wrap">
      <div>
        <div style="font-size:9px;font-weight:900;color:#8833bb;letter-spacing:2px;margin-bottom:4px">6-DIGIT CODE</div>
        <div id="tfaCode" style="font-size:38px;font-weight:900;color:#8833bb;letter-spacing:8px;cursor:pointer;user-select:none" onclick="copyTFA()" title="Click to copy">——————</div>
      </div>
      <div>
        <div style="font-size:9px;font-weight:900;color:#aaa;letter-spacing:2px;margin-bottom:4px">EXPIRES IN</div>
        <div id="tfaTimer" style="font-size:22px;font-weight:900;color:#cc8800;letter-spacing:2px">—s</div>
        <div style="margin-top:6px;background:#eee;border-radius:4px;height:5px;width:120px;overflow:hidden">
          <div id="tfaTimerBar" style="background:linear-gradient(90deg,#8833bb,#cc55ff);height:5px;width:100%;transition:width 1s linear"></div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <button class="btn btn-sm" onclick="copyTFA()" style="background:#8833bb">📋 COPY CODE</button>
        <button class="btn btn-sm btn-grey" onclick="gen2FA()">🔄 REFRESH</button>
      </div>
    </div>
    <div style="font-size:10px;color:#888;font-weight:700;margin-top:8px">
      💡 Key format: <code style="background:#f5e6ff;padding:1px 5px;border-radius:3px">U3KOCP2GHFNBXHLULXOJ</code> &nbsp;·&nbsp; Auto-refreshes every 30s &nbsp;·&nbsp; Click code to copy
    </div>
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
          <div><input type="checkbox" id="chkAll" onchange="toggleAllChk(this)" title="Select all"></div>
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
  <div id="actionRow" style="display:none;margin-top:14px">
    <!-- Row 1: Fetch + Reset -->
    <div class="row" style="margin-bottom:8px">
      <button class="btn btn-green" id="fetchAllBtn" onclick="fetchAllOTPs()">⚡ FETCH ALL OTPs</button>
      <button class="btn btn-grey btn-sm" onclick="resetAll()">🔄 RESET</button>
    </div>
    <!-- Row 2: Bulk copy + select -->
    <div class="row" style="gap:8px;flex-wrap:wrap">
      <span style="font-size:10px;color:#888;font-weight:900;letter-spacing:1px;align-self:center">BULK:</span>
      <button class="btn btn-sm" onclick="copyAllUidPass()" style="background:#cc8800">👥 COPY ALL UID|PASS</button>
      <button class="btn btn-sm" onclick="copyAllOTPs()" style="background:#22aa66">📋 COPY ALL OTPs</button>
      <button class="btn btn-sm" onclick="selectFirst(50)" style="background:#4455ff">✅ SELECT 50</button>
      <button class="btn btn-sm" onclick="selectFirst(100)" style="background:#4455ff">✅ SELECT 100</button>
      <button class="btn btn-sm" onclick="selectFirst(200)" style="background:#4455ff">✅ SELECT 200</button>
      <button class="btn btn-sm btn-grey" onclick="selectAll()">☑ ALL</button>
      <button class="btn btn-sm btn-grey" onclick="clearSel()">✕ CLEAR SEL</button>
    </div>
    <!-- Row 3: Selection copy -->
    <div class="row" id="selActionRow" style="display:none;margin-top:6px;padding:10px;background:#f0f2ff;border-radius:8px;border:1.5px solid #c0c8ff">
      <span id="selCountLabel" style="font-size:11px;color:#4455ff;font-weight:900;align-self:center">0 selected</span>
      <button class="btn btn-sm" onclick="copySelectedUidPass()" style="background:#cc8800">👥 COPY SEL UID|PASS</button>
      <button class="btn btn-sm" onclick="copySelectedOTPs()" style="background:#22aa66">📋 COPY SEL OTPs</button>
    </div>
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
      <div><input type="checkbox" class="row-chk" data-idx="${a.idx}" onchange="onRowChk(${a.idx},this)"></div>
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



// ── SELECTION STATE ───────────────────────────────────────────────────────────
let selectedIdxs = new Set();

function onRowChk(idx, el){
  if(el.checked) selectedIdxs.add(idx); else selectedIdxs.delete(idx);
  updateSelRow();
}

function toggleAllChk(el){
  document.querySelectorAll('.row-chk').forEach(c=>{
    c.checked = el.checked;
    const i = parseInt(c.dataset.idx);
    if(el.checked) selectedIdxs.add(i); else selectedIdxs.delete(i);
  });
  updateSelRow();
}

function selectFirst(n){
  selectedIdxs.clear();
  accounts.slice(0, n).forEach(a=> selectedIdxs.add(a.idx));
  document.querySelectorAll('.row-chk').forEach(c=>{
    const i = parseInt(c.dataset.idx);
    c.checked = selectedIdxs.has(i);
  });
  updateSelRow();
  showToast(`☑ ${Math.min(n, accounts.length)} accounts selected`);
}

function selectAll(){
  selectedIdxs.clear();
  accounts.forEach(a=> selectedIdxs.add(a.idx));
  document.querySelectorAll('.row-chk').forEach(c=>{ c.checked=true; });
  updateSelRow();
  showToast(`☑ All ${accounts.length} selected`);
}

function clearSel(){
  selectedIdxs.clear();
  document.querySelectorAll('.row-chk').forEach(c=>{ c.checked=false; });
  const ca = document.getElementById('chkAll');
  if(ca) ca.checked=false;
  updateSelRow();
}

function updateSelRow(){
  const row = document.getElementById('selActionRow');
  const lbl = document.getElementById('selCountLabel');
  if(selectedIdxs.size > 0){
    row.style.display='flex';
    if(lbl) lbl.textContent = `${selectedIdxs.size} accounts selected`;
  } else {
    row.style.display='none';
  }
}

// ── BULK COPY ALL UID|PASS ────────────────────────────────────────────────────
function copyAllUidPass(){
  const lines = accounts
    .map(a=>{
      const p = a.line.split('|');
      const uid = a.uid || (p.length>=5 ? p[0].trim() : '');
      const pass = p.length>=5 ? p[1].trim() : (p.length>=2 ? p[1].trim() : '');
      return uid && pass ? uid+'|'+pass : null;
    })
    .filter(Boolean);
  if(!lines.length){ showToast('❌ Koi UID|PASS nahi mila'); return; }
  navigator.clipboard.writeText(lines.join('\n')).then(()=>
    showToast(`✅ ${lines.length} UID|PASS copied!`));
}

function copyAllOTPs(){
  const lines = accounts
    .map(a=>{ const r=results[a.idx]; return r&&r.otp ? r.otp : null; })
    .filter(Boolean);
  if(!lines.length){ showToast('❌ Koi OTP nahi mila — pehle FETCH karo'); return; }
  navigator.clipboard.writeText(lines.join('\n')).then(()=>
    showToast(`✅ ${lines.length} OTPs copied!`));
}

// ── BULK COPY SELECTED ────────────────────────────────────────────────────────
function copySelectedUidPass(){
  if(!selectedIdxs.size){ showToast('Koi account select nahi — SELECT N pehle click karo'); return; }
  const lines = [...selectedIdxs].sort((a,b)=>a-b).map(idx=>{
    const a = accounts[idx];
    if(!a) return null;
    const p = a.line.split('|');
    const uid  = a.uid || (p.length>=5 ? p[0].trim() : '');
    const pass = p.length>=5 ? p[1].trim() : (p.length>=2 ? p[1].trim() : '');
    return uid && pass ? uid+'|'+pass : null;
  }).filter(Boolean);
  if(!lines.length){ showToast('❌ Koi UID|PASS nahi mila'); return; }
  navigator.clipboard.writeText(lines.join('\n')).then(()=>
    showToast(`✅ ${lines.length} selected UID|PASS copied!`));
}

function copySelectedOTPs(){
  if(!selectedIdxs.size){ showToast('Koi account select nahi'); return; }
  const lines = [...selectedIdxs].sort((a,b)=>a-b).map(idx=>{
    const r = results[idx]; return r&&r.otp ? r.otp : null;
  }).filter(Boolean);
  if(!lines.length){ showToast('❌ Selected accounts mein OTP nahi mila'); return; }
  navigator.clipboard.writeText(lines.join('\n')).then(()=>
    showToast(`✅ ${lines.length} selected OTPs copied!`));
}

// ── 2FA PANEL ─────────────────────────────────────────────────────────────────
let tfa2FaAutoTimer = null;

function toggle2FAPanel(){
  const p = document.getElementById('panel2FA');
  p.style.display = p.style.display==='none' ? 'block' : 'none';
  if(p.style.display==='block') document.getElementById('tfaKeyInput').focus();
}

async function gen2FA(){
  const key = document.getElementById('tfaKeyInput').value.trim();
  if(!key){ showToast('❌ 2FA key paste karo pehle'); return; }

  const res = await fetch('/get_2fa',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key})
  }).then(r=>r.json()).catch(e=>({error:e.message}));

  const resultBox = document.getElementById('tfaResult');
  resultBox.style.display='flex';

  if(res.error){
    document.getElementById('tfaCode').textContent='ERROR';
    document.getElementById('tfaCode').style.color='#cc3333';
    document.getElementById('tfaTimer').textContent=res.error.slice(0,40);
    showToast('❌ '+res.error);
    return;
  }

  document.getElementById('tfaCode').textContent = res.otp;
  document.getElementById('tfaCode').style.color = '#8833bb';
  document.getElementById('tfaTimer').textContent = res.remaining+'s';
  document.getElementById('tfaTimerBar').style.width = (res.remaining/30*100)+'%';
  showToast('✅ 2FA code: '+res.otp+' — Click to copy!', 3000);

  // Auto countdown
  if(tfa2FaAutoTimer) clearInterval(tfa2FaAutoTimer);
  let rem = res.remaining;
  tfa2FaAutoTimer = setInterval(async ()=>{
    rem--;
    if(rem <= 0){
      clearInterval(tfa2FaAutoTimer);
      // Auto refresh new code
      await gen2FA();
      return;
    }
    document.getElementById('tfaTimer').textContent = rem+'s';
    document.getElementById('tfaTimerBar').style.width = (rem/30*100)+'%';
    // Color warning
    const c = document.getElementById('tfaCode');
    c.style.color = rem<=7 ? '#cc3333' : rem<=15 ? '#cc8800' : '#8833bb';
  }, 1000);
}

function copyTFA(){
  const code = document.getElementById('tfaCode').textContent;
  if(!code || code==='——————' || code==='ERROR') return;
  navigator.clipboard.writeText(code).then(()=>showToast('📋 2FA Code copied: '+code));
}

function clearTFA(){
  document.getElementById('tfaKeyInput').value='';
  document.getElementById('tfaResult').style.display='none';
  if(tfa2FaAutoTimer) clearInterval(tfa2FaAutoTimer);
}

// 2FA from per-row 2FA key (if accounts have 2fa field)
function gen2FAForRow(idx){
  const a = accounts[idx];
  if(!a) return;
  const p = a.line.split('|');
  // Format: uid|fbpass|email|emailpass|refresh_token|client_id|2fa_key (7th field)
  const twoFaKey = p.length >= 7 ? p[6].trim() : '';
  if(!twoFaKey){ showToast('❌ Yeh account mein 2FA key nahi hai (7th field)'); return; }
  document.getElementById('tfaKeyInput').value = twoFaKey;
  document.getElementById('panel2FA').style.display='block';
  gen2FA();
}


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

        if p == "/admin/tokens":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            self.send_json({"tokens": TokenStore.all()})
            return

        if p.startswith("/u/"):
            token = p[3:]
            ok, result = TokenStore.is_valid(token)
            if not ok:
                self.send_html(f"""<!DOCTYPE html><html><body style='font-family:monospace;background:#f0f2f8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0'>
                <div style='background:#fff;border:2px solid #ffcccc;border-radius:14px;padding:40px;text-align:center;max-width:400px'>
                <div style='font-size:48px;margin-bottom:16px'>🔒</div>
                <div style='font-size:18px;font-weight:900;color:#cc3333;margin-bottom:10px'>ACCESS DENIED</div>
                <div style='color:#888;font-size:13px;font-weight:700'>{result}</div>
                </div></body></html>""", 403)
                return
            tok_data = result
            acct_ids = tok_data.get("account_ids", [])
            all_accts = AccountStore.all()
            user_accts = [a for a in all_accts if a.get("id") in acct_ids]
            lines_json = json.dumps([a["line"] for a in user_accts])
            user_name  = tok_data.get("user_name","User")
            expires    = tok_data.get("expires_at") or "Never"
            html = USER_HTML \
                .replace("__USER_NAME__", user_name) \
                .replace("__TOKEN__", token) \
                .replace("__EXPIRES__", expires) \
                .replace("__ACC_COUNT__", str(len(user_accts))) \
                .replace("__LINES_JSON__", lines_json)
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

        elif p == "/get_2fa":
            body  = json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
            key   = body.get("key","").strip()
            if not key:
                self.send_json({"error":"2FA key missing"})
            else:
                self.send_json(generate_totp(key))
            return

        # ── Token Management ──────────────────────────────────────────────
        if p == "/admin/token/create":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            try:
                payload     = json.loads(self.body())
                user_name   = payload.get("user_name","").strip() or "User"
                count       = int(payload.get("count", 50))
                expiry_type = payload.get("expiry_type","48h")  # 48h | 7d | permanent
                shuffle     = payload.get("shuffle", True)

                # Get already-assigned account IDs from existing tokens
                existing_tokens = TokenStore.all()
                assigned_ids    = set()
                for t in existing_tokens:
                    assigned_ids.update(t.get("account_ids",[]))

                all_accts = AccountStore.all()
                # Unassigned accounts only
                available = [a for a in all_accts if a.get("id") not in assigned_ids]

                if shuffle:
                    import random; random.shuffle(available)

                selected = available[:count]
                if not selected:
                    self.send_json({"error": f"No unassigned accounts available (total={len(all_accts)}, assigned={len(assigned_ids)})"}); return

                acct_ids = [a["id"] for a in selected]
                entry    = TokenStore.create(user_name, acct_ids, expiry_type)
                self.send_json({"ok":True, "token": entry, "count": len(acct_ids)})
            except Exception as e:
                self.send_json({"error": str(e)})
            return

        if p == "/admin/token/delete":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            try:
                payload = json.loads(self.body())
                token   = payload.get("token","")
                deleted = TokenStore.delete(token)
                self.send_json({"ok":True, "deleted":deleted})
            except Exception as e:
                self.send_json({"error":str(e)})
            return

        if p == "/admin/token/refresh":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            try:
                payload     = json.loads(self.body())
                old_token   = payload.get("token","")
                new_count   = payload.get("count", None)
                expiry_type = payload.get("expiry_type", None)
                new_ids     = None

                if new_count is not None:
                    # Reassign fresh unassigned accounts
                    existing  = TokenStore.all()
                    # assigned by others (not this token)
                    assigned  = set()
                    for t in existing:
                        if t.get("token") != old_token:
                            assigned.update(t.get("account_ids",[]))
                    available = [a for a in AccountStore.all() if a.get("id") not in assigned]
                    import random; random.shuffle(available)
                    selected  = available[:int(new_count)]
                    new_ids   = [a["id"] for a in selected]

                entry = TokenStore.refresh_token(old_token, new_ids, expiry_type)
                if not entry:
                    self.send_json({"error":"Token not found"}); return
                self.send_json({"ok":True, "token": entry})
            except Exception as e:
                self.send_json({"error":str(e)})
            return

        if p == "/admin/token/delete_all":
            if not self.is_authed():
                self.send_json({"error":"Not authenticated"}); return
            TokenStore.delete_all()
            self.send_json({"ok":True})
            return

        # ── User token fetch (no admin session needed — token is auth) ────
        if p.startswith("/u/") and p.endswith("/fetch"):
            token = p[3:-6]
            ok, result = TokenStore.is_valid(token)
            if not ok:
                self.send_json({"error": result}); return
            try:
                payload = json.loads(self.body())
                line    = payload.get("line","").strip()
                limit   = int(payload.get("limit",20))
                tok_data= result
                acct_ids= tok_data.get("account_ids",[])
                # Verify this line belongs to this token
                all_accts = AccountStore.all()
                token_lines = {a["line"] for a in all_accts if a.get("id") in acct_ids}
                if line not in token_lines:
                    self.send_json({"error":"Account not in your token"}); return
                acct = parse_account_line(line)
                if not acct:
                    self.send_json({"error":"Invalid format"}); return
                res, err = fetch_latest_otp(acct, limit)
                if err:
                    self.send_json({"error":err,"email":acct["email"]}); return
                self.send_json({
                    "email":   acct["email"], "uid": acct.get("uid",""),
                    "otp":     res.get("otp"), "subject": res.get("subject",""),
                    "sender":  res.get("sender",""), "date": res.get("date",""),
                })
            except Exception as e:
                self.send_json({"error":str(e)})
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

/* Distribute */
.dist-row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:12px;padding:12px;background:#f9fafc;border:1.5px solid #dde1ea;border-radius:10px}
.dist-field{display:flex;flex-direction:column;gap:4px;flex:1;min-width:120px}
.dist-field label{font-size:9px;color:#4455ff;letter-spacing:1.5px;font-weight:900;text-transform:uppercase}
.dist-field input,.dist-field select{padding:8px 11px;background:#fff;border:2px solid #dde1ea;border-radius:7px;color:#1a1a2e;font-size:11.5px;font-family:inherit;font-weight:700;outline:none;transition:border-color .2s}
.dist-field input:focus,.dist-field select:focus{border-color:#4455ff}
.tok-tbl{width:100%;border-collapse:collapse;font-size:11px;margin-top:4px}
.tok-tbl th{background:#f0f2ff;color:#4455ff;font-size:9px;letter-spacing:2px;text-transform:uppercase;padding:9px 11px;text-align:left;border-bottom:2px solid #dde1ea}
.tok-tbl td{padding:10px 11px;border-bottom:1px solid #f0f1f6;vertical-align:middle}
.tok-tbl tr:hover td{background:#fafbff}
.tok-url{font-size:10.5px;color:#4455ff;font-weight:900;word-break:break-all;cursor:pointer;text-decoration:underline}
.tok-name{font-weight:900;color:#1a1a2e;white-space:nowrap}
.tok-exp-ok{color:#22aa66;font-size:10px;font-weight:900}
.tok-exp-no{color:#cc3333;font-size:10px;font-weight:900}
.tok-cnt{color:#4455ff;font-weight:900;font-size:13px}
.tbtn{padding:4px 9px;border-radius:5px;font-size:9.5px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap;border:none}
.tbtn-b{background:#eef0ff;color:#4455ff;border:1.5px solid #c0c8ff}.tbtn-b:hover{background:#4455ff;color:#fff}
.tbtn-o{background:#fff8e6;color:#cc8800;border:1.5px solid #ffddaa}.tbtn-o:hover{background:#cc8800;color:#fff}
.tbtn-r{background:#fff5f5;color:#cc3333;border:1.5px solid #ffcccc}.tbtn-r:hover{background:#cc3333;color:#fff}
.tbtn-g{background:#e6fff3;color:#22aa66;border:1.5px solid #99ddbb}.tbtn-g:hover{background:#22aa66;color:#fff}
.avail-badge{display:inline-block;padding:4px 12px;background:#e6fff3;border:1.5px solid #99ddbb;border-radius:8px;font-size:11px;color:#22aa66;font-weight:900;margin-bottom:10px}
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

  <!-- ── DISTRIBUTE PANEL ─────────────────────────────────────── -->
  <div class="panel">
    <div class="panel-title">🔗 USER DISTRIBUTION — TOKEN MANAGER</div>
    <div class="avail-badge" id="availBadge">⟳ Loading...</div>

    <!-- Create new token -->
    <div class="dist-row">
      <div class="dist-field">
        <label>User Name</label>
        <input id="distName" type="text" placeholder="e.g. Ahmed / User 1">
      </div>
      <div class="dist-field" style="max-width:100px">
        <label>Accounts #</label>
        <input id="distCount" type="number" value="50" min="1" max="9999">
      </div>
      <div class="dist-field" style="max-width:150px">
        <label>Expiry</label>
        <select id="distExpiry">
          <option value="48h">48 Hours</option>
          <option value="7d">7 Days</option>
          <option value="permanent">Permanent</option>
        </select>
      </div>
      <div class="dist-field" style="max-width:140px">
        <label>Order</label>
        <select id="distShuffle">
          <option value="1">Shuffle (Random)</option>
          <option value="0">Sequential</option>
        </select>
      </div>
      <button class="abtn abtn-green" onclick="createToken()" style="align-self:flex-end">➕ CREATE TOKEN</button>
    </div>

    <!-- Tokens table -->
    <div style="overflow-x:auto">
      <table class="tok-tbl">
        <thead><tr>
          <th>#</th><th>USER</th><th>ACC</th><th>EXPIRES</th><th>ACCESS URL (click to copy)</th><th>ACTIONS</th>
        </tr></thead>
        <tbody id="tokBody"></tbody>
      </table>
    </div>
    <p class="empty" id="tokEmpty" style="display:none;padding:20px 0">No tokens yet. Create one above!</p>
    <div class="row" style="margin-top:12px">
      <button class="abtn abtn-red" onclick="deleteAllTokens()" style="font-size:10px">🗑️ DELETE ALL TOKENS</button>
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


// ── TOKEN MANAGER JS ──────────────────────────────────────────────────────────
const SERVER_BASE = window.location.origin;

async function loadTokens(){
  try{
    const r = await fetch('/admin/tokens').then(r=>r.json());
    renderTokens(r.tokens||[]);
    loadAvailableBadge();
  } catch(e){ console.error('loadTokens error',e); }
}

async function loadAvailableBadge(){
  try{
    const [ar, tr] = await Promise.all([
      fetch('/admin/list').then(r=>r.json()),
      fetch('/admin/tokens').then(r=>r.json())
    ]);
    const total    = (ar.accounts||[]).length;
    const tokens   = tr.tokens||[];
    const assigned = new Set(tokens.flatMap(t=>t.account_ids||[]));
    const avail    = total - assigned.size;
    const badge    = document.getElementById('availBadge');
    if(badge) badge.textContent = `📦 Total: ${total} · Assigned: ${assigned.size} · Available: ${avail}`;
  } catch{}
}

function renderTokens(tokens){
  const tbody = document.getElementById('tokBody');
  const empty = document.getElementById('tokEmpty');
  if(!tokens.length){ if(tbody) tbody.innerHTML=''; if(empty) empty.style.display='block'; return; }
  if(empty) empty.style.display='none';

  const now = new Date();
  tbody.innerHTML = tokens.map((t,i)=>{
    const url = `${SERVER_BASE}/u/${t.token}`;
    let expHtml;
    if(t.expiry_type==='permanent'){
      expHtml = '<span class="tok-ok">∞ Permanent</span>';
    } else {
      try{
        const exp = new Date(t.expires_at.replace(' ','T'));
        const expired = now > exp;
        const diffH   = Math.round((exp-now)/3600000);
        expHtml = expired
          ? '<span class="tok-ex">❌ EXPIRED</span>'
          : `<span class="tok-ok">✅ ${diffH}h left<br><span style="font-size:9px;color:#aaa">${t.expires_at}</span></span>`;
      } catch{ expHtml = t.expires_at||'—'; }
    }
    return `<tr id="tokrow_${t.token.slice(0,8)}">
      <td style="color:#aaa;font-size:12px">${i+1}</td>
      <td><b style="color:#1a1a2e">${esc(t.user_name)}</b><br><span style="font-size:9px;color:#aaa">${esc(t.created_at||'')}</span></td>
      <td class="tok-cnt">${(t.account_ids||[]).length}</td>
      <td>${expHtml}</td>
      <td>
        <span class="tok-url" onclick="copyTokUrl('${t.token}')" title="Click to copy">${url.slice(0,50)}...</span>
      </td>
      <td style="white-space:nowrap">
        <button class="tbtn tbtn-b" onclick="copyTokUrl('${t.token}')">📋 URL</button>
        <button class="tbtn tbtn-o" onclick="refreshToken('${t.token}')">🔄 REGEN</button>
        <button class="tbtn tbtn-r" onclick="deleteToken('${t.token}')">🗑️ DEL</button>
      </td>
    </tr>`;
  }).join('');
}

function copyTokUrl(token){
  const url = `${SERVER_BASE}/u/${token}`;
  navigator.clipboard.writeText(url).then(()=>toast('📋 URL copied: '+url, 2500));
}

async function createToken(){
  const name   = document.getElementById('distName').value.trim() || 'User';
  const count  = parseInt(document.getElementById('distCount').value)||50;
  const expiry = document.getElementById('distExpiry').value;
  const shuffle= document.getElementById('distShuffle').value === '1';

  const r = await fetch('/admin/token/create',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_name:name, count, expiry_type:expiry, shuffle})
  }).then(r=>r.json()).catch(e=>({error:e.message}));

  if(r.error){ toast('❌ '+r.error, 3500); return; }

  const url = `${SERVER_BASE}/u/${r.token.token}`;
  toast(`✅ Token created! ${r.count} accounts for ${name}`, 3000);
  // Auto-copy URL
  navigator.clipboard.writeText(url).then(()=>toast('📋 URL auto-copied! Share with '+name, 3000));
  document.getElementById('distName').value='';
  await loadTokens();
}

async function refreshToken(token){
  const newCount = prompt('New account count? (leave empty = keep same)');
  let count = null;
  if(newCount && newCount.trim()) count = parseInt(newCount.trim());

  const r = await fetch('/admin/token/refresh',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({token, count})
  }).then(r=>r.json()).catch(e=>({error:e.message}));

  if(r.error){ toast('❌ '+r.error); return; }
  const url = `${SERVER_BASE}/u/${r.token.token}`;
  navigator.clipboard.writeText(url).then(()=>toast('✅ Token regenerated! New URL copied', 3000));
  await loadTokens();
}

async function deleteToken(token){
  if(!confirm('Delete this token? User ka access band ho jayega.')) return;
  const r = await fetch('/admin/token/delete',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({token})
  }).then(r=>r.json());
  if(r.ok){ toast('🗑️ Token deleted'); await loadTokens(); }
  else toast('❌ '+r.error);
}

async function deleteAllTokens(){
  if(!confirm('ALL tokens delete karo? Saare users ka access band ho jayega!')) return;
  await fetch('/admin/token/delete_all',{method:'POST'});
  toast('🗑️ All tokens deleted');
  await loadTokens();
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
loadTokens();
</script>
</body></html>"""


USER_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTP Reader — __USER_NAME__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f0f2f8;color:#1a1a2e;font-family:'Courier New',monospace;font-weight:700;min-height:100vh}
header{background:#1a1a2e;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.brand{color:#fff;font-size:15px;font-weight:900;letter-spacing:3px}
.user-badge{background:#22aa66;color:#fff;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:900;letter-spacing:1px}
.exp-badge{font-size:10px;color:#aaa;font-weight:700;letter-spacing:1px}
.wrap{max-width:1000px;margin:0 auto;padding:20px 16px}
.panel{background:#fff;border:2px solid #dde1ea;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,.05)}
.panel-title{color:#4455ff;font-size:10px;letter-spacing:3px;font-weight:900;margin-bottom:14px}
.btn{padding:10px 20px;background:#4455ff;border:none;border-radius:8px;color:#fff;font-size:11px;font-family:inherit;cursor:pointer;font-weight:900;letter-spacing:1px;transition:background .2s;white-space:nowrap}
.btn:hover{background:#3344dd}
.btn-green{background:#22aa66}.btn-green:hover{background:#1a8850}
.btn-red{background:#cc3333}.btn-red:hover{background:#aa2222}
.btn-sm{padding:6px 12px;font-size:10px}
.btn-gold{background:#cc8800}.btn-gold:hover{background:#aa6600}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
select{padding:8px 12px;background:#f9fafc;border:2px solid #dde1ea;border-radius:8px;color:#1a1a2e;font-size:11px;font-family:inherit;font-weight:900;outline:none}
.prog-wrap{background:#fff;border:2px solid #dde1ea;border-radius:12px;padding:14px 18px;margin-bottom:14px}
.prog-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.prog-label{color:#4455ff;font-size:11px;font-weight:900;letter-spacing:2px}
.prog-nums{color:#1a1a2e;font-size:13px;font-weight:900}
.prog-bar-bg{background:#eef0ff;border-radius:8px;height:8px;overflow:hidden}
.prog-bar{background:linear-gradient(90deg,#4455ff,#22aa66);height:8px;border-radius:8px;transition:width .4s ease;width:0%}
.prog-status{color:#666;font-size:10px;margin-top:6px;font-weight:700}
.summary{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.chip{padding:6px 14px;border-radius:20px;font-size:10px;font-weight:900}
.chip-blue{background:#eef0ff;color:#4455ff;border:2px solid #c0c8ff}
.chip-green{background:#e6fff3;color:#22aa66;border:2px solid #99ddbb}
.chip-red{background:#fff5f5;color:#cc3333;border:2px solid #ffcccc}
.chip-grey{background:#f5f6fa;color:#666;border:2px solid #dde1ea}
.tbl-wrap{background:#fff;border:2px solid #dde1ea;border-radius:12px;overflow:hidden}
.tbl-head{display:grid;grid-template-columns:40px minmax(100px,1fr) minmax(150px,1.5fr) 110px 90px 120px;background:#f0f2ff;border-bottom:2px solid #dde1ea;padding:9px 14px;font-size:9px;color:#4455ff;letter-spacing:2px;font-weight:900}
.row-item{display:grid;grid-template-columns:40px minmax(100px,1fr) minmax(150px,1.5fr) 110px 90px 120px;padding:10px 14px;border-bottom:1.5px solid #f0f1f6;align-items:center;gap:4px}
.row-item:last-child{border-bottom:none}
.row-item:hover{background:#fafbff}
.num-c{color:#aaa;font-size:12px;font-weight:900}
.uid-c{font-size:11px;font-weight:900;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.email-c{font-size:11px;color:#555;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.otp-c{font-size:18px;font-weight:900;color:#4455ff;letter-spacing:3px;cursor:pointer}
.otp-none{color:#ccc;font-size:12px;font-weight:900}
.otp-err{color:#cc3333;font-size:9px;word-break:break-all}
.st{display:inline-block;padding:3px 8px;border-radius:10px;font-size:9px;font-weight:900;white-space:nowrap}
.st-pending{background:#f5f6fa;color:#bbb;border:1.5px solid #dde1ea}
.st-fetching{background:#fff8e6;color:#cc8800;border:1.5px solid #ffddaa}
.st-found{background:#e6fff3;color:#22aa66;border:1.5px solid #99ddbb}
.st-none{background:#f5f6fa;color:#999;border:1.5px solid #dde1ea}
.st-err{background:#fff5f5;color:#cc3333;border:1.5px solid #ffcccc}
.act-c{display:flex;flex-direction:column;gap:4px}
.cb{padding:3px 8px;border-radius:5px;font-size:9px;font-family:inherit;cursor:pointer;font-weight:900;border:1.5px solid;transition:all .15s;white-space:nowrap}
.cb-otp{background:#eef0ff;border-color:#c0c8ff;color:#4455ff}
.cb-otp:hover{background:#4455ff;color:#fff}
.cb-uid{background:#fff8e6;border-color:#ffddaa;color:#cc8800}
.cb-uid:hover{background:#cc8800;color:#fff}
.cb-otp:disabled,.cb-uid:disabled{background:#f5f6fa;border-color:#eee;color:#ccc;cursor:default}
.toast{position:fixed;bottom:20px;right:20px;background:#1a1a2e;color:#fff;padding:11px 20px;border-radius:10px;font-size:12px;font-weight:900;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
@media(max-width:600px){
  .tbl-head,.row-item{grid-template-columns:30px 1fr 80px 60px}
  .email-col,.subj-col{display:none}
  .wrap{padding:14px 10px}
}
</style></head>
<body>
<header>
  <span class="brand">🔐 OTP READER</span>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="user-badge">👤 __USER_NAME__</span>
    <span class="exp-badge">__EXP_LABEL__</span>
  </div>
</header>
<div class="wrap">

  <div class="panel">
    <div class="panel-title">⚡ FETCH OTPs — YOUR __ACC_COUNT__ ACCOUNTS</div>
    <div class="row">
      <select id="lm">
        <option value="10">Last 10 emails</option>
        <option value="20" selected>Last 20 emails</option>
        <option value="50">Last 50 emails</option>
      </select>
      <button class="btn btn-green" id="fetchAllBtn" onclick="fetchAll()">⚡ FETCH ALL OTPs</button>
      <button class="btn btn-gold btn-sm" onclick="copyAllUid()">👥 COPY ALL UID|PASS</button>
      <button class="btn btn-sm" onclick="copyAllOtp()">📋 COPY ALL OTPs</button>
    </div>
  </div>

  <div class="prog-wrap" id="progWrap" style="display:none">
    <div class="prog-top">
      <span class="prog-label">⚡ FETCHING...</span>
      <span class="prog-nums" id="progNums">0 / 0</span>
    </div>
    <div class="prog-bar-bg"><div class="prog-bar" id="progBar"></div></div>
    <div class="prog-status" id="progStatus">Starting...</div>
  </div>

  <div class="summary" id="summaryBar" style="display:none"></div>

  <div class="tbl-wrap">
    <div class="tbl-head">
      <div>#</div><div>UID</div><div class="email-col">EMAIL</div>
      <div>LATEST OTP</div><div>STATUS</div><div>ACTION</div>
    </div>
    <div id="tableBody"></div>
  </div>

</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN    = '__TOKEN__';
const accounts = __ACCOUNTS_JSON__;
let results    = {};
let isFetching = false, stopFlag = false;

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function toast(m,d=2000){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),d); }
function cp(txt,lbl){ navigator.clipboard.writeText(txt).then(()=>toast('📋 COPIED: '+(lbl||txt))); }

// Build table
document.getElementById('tableBody').innerHTML = accounts.map((a,i)=>`
  <div class="row-item" id="row_${i}">
    <div class="num-c">${i+1}</div>
    <div class="uid-c" title="${esc(a.uid)}">${a.uid||'<span style="color:#ccc">—</span>'}</div>
    <div class="email-c email-col" title="${esc(a.email)}">${esc(a.email)}</div>
    <div id="otp_${i}" class="otp-none">—</div>
    <div><span class="st st-pending" id="st_${i}">PENDING</span></div>
    <div class="act-c">
      <button class="cb cb-otp" id="copybtn_${i}" disabled onclick="cpOTP(${i})">📋 COPY OTP</button>
      <button class="cb cb-uid" ${a.uid?'':'disabled'} onclick="cpUID(${i})">👤 UID|PASS</button>
    </div>
  </div>`).join('');

function setSt(i,type,lbl){
  const el=document.getElementById('st_'+i);
  if(el){ el.className='st st-'+type; el.textContent=lbl; }
}
function setOTP(i,otp,err){
  const el=document.getElementById('otp_'+i);
  const cb=document.getElementById('copybtn_'+i);
  if(otp){ el.className='otp-c'; el.textContent=otp; el.onclick=()=>cpOTP(i); if(cb)cb.disabled=false; }
  else if(err){ el.className='otp-err'; el.textContent=err.slice(0,80); el.onclick=null; }
  else { el.className='otp-none'; el.textContent='—'; el.onclick=null; }
}

async function fetchAll(){
  if(isFetching){ stopFlag=true; return; }
  isFetching=true; stopFlag=false; results={};
  const btn=document.getElementById('fetchAllBtn');
  btn.textContent='⏹ STOP'; btn.style.background='#cc3333';
  document.getElementById('progWrap').style.display='block';
  const limit=parseInt(document.getElementById('lm').value);
  let done=0;
  for(let i=0;i<accounts.length;i++){
    if(stopFlag) break;
    const a=accounts[i];
    setSt(i,'fetching','⏳ FETCHING');
    document.getElementById('progNums').textContent=`${done}/${accounts.length}`;
    document.getElementById('progBar').style.width=`${Math.round(done/accounts.length*100)}%`;
    document.getElementById('progStatus').textContent=`Fetching: ${a.email}`;
    try{
      const res=await fetch('/u/fetch',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({token:TOKEN,line:a.line,limit})
      }).then(r=>r.json());
      results[i]=res;
      if(res.error){ setSt(i,'err','❌ ERROR'); setOTP(i,null,res.error); }
      else if(res.otp){ setSt(i,'found','✅ FOUND'); setOTP(i,res.otp,null); }
      else { setSt(i,'none','📭 NO OTP'); setOTP(i,null,null); }
    } catch(e){ results[i]={error:e.message}; setSt(i,'err','❌ ERR'); setOTP(i,null,e.message); }
    done++;
  }
  document.getElementById('progNums').textContent=`${done}/${accounts.length}`;
  document.getElementById('progBar').style.width=stopFlag?document.getElementById('progBar').style.width:'100%';
  document.getElementById('progStatus').textContent=stopFlag?`⏹ Stopped at ${done}`:`✅ Done! ${done} processed`;
  isFetching=false; btn.textContent='⚡ FETCH ALL OTPs'; btn.style.background='#22aa66';
  updateSummary();
}

function updateSummary(){
  const total=accounts.length, found=Object.values(results).filter(r=>r.otp).length;
  const noOtp=Object.values(results).filter(r=>!r.error&&!r.otp).length;
  const err=Object.values(results).filter(r=>r.error).length;
  const bar=document.getElementById('summaryBar');
  bar.style.display='flex';
  bar.innerHTML=`<div class="chip chip-blue">📋 TOTAL: ${total}</div><div class="chip chip-green">✅ FOUND: ${found}</div><div class="chip chip-grey">📭 NO OTP: ${noOtp}</div><div class="chip chip-red">❌ ERR: ${err}</div>`;
}

function cpOTP(i){ const r=results[i]; if(r&&r.otp) cp(r.otp,'OTP '+r.otp); }
function cpUID(i){
  const a=accounts[i]; if(!a||!a.uid) return;
  const p=a.line.split('|'); const pass=p.length>=5?p[1].trim():p.length>=2?p[1].trim():'';
  cp(a.uid+'|'+pass,'UID|PASS');
}
function copyAllUid(){
  const lines=accounts.map(a=>{const p=a.line.split('|');const uid=a.uid||(p.length>=5?p[0].trim():'');const pass=p.length>=5?p[1].trim():p.length>=2?p[1].trim():'';return uid&&pass?uid+'|'+pass:null;}).filter(Boolean);
  if(!lines.length){toast('❌ No UID|PASS');return;}
  navigator.clipboard.writeText(lines.join('\n')).then(()=>toast(`✅ ${lines.length} UID|PASS copied!`));
}
function copyAllOtp(){
  const lines=Object.values(results).filter(r=>r.otp).map(r=>r.otp);
  if(!lines.length){toast('❌ No OTPs yet — fetch first');return;}
  navigator.clipboard.writeText(lines.join('\n')).then(()=>toast(`✅ ${lines.length} OTPs copied!`));
}
</script>
</body></html>"""


USER_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTP Reader — __USER_NAME__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f0f2f8;color:#1a1a2e;font-family:'Courier New',monospace;font-weight:700;min-height:100vh}
header{background:#fff;border-bottom:2px solid #dde1ea;padding:13px 22px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.06)}
.brand{font-size:14px;font-weight:900;letter-spacing:3px;color:#1a1a2e}
.hinfo{font-size:10px;color:#888;font-weight:700;text-align:right;line-height:1.7}
.wrap{max-width:1060px;margin:0 auto;padding:20px 14px}
.panel{background:#fff;border:2px solid #dde1ea;border-radius:14px;padding:18px 22px;margin-bottom:16px;box-shadow:0 2px 10px rgba(0,0,0,.05)}
.ptitle{color:#4455ff;font-size:10px;letter-spacing:3px;font-weight:900;margin-bottom:12px;text-transform:uppercase}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
select{padding:8px 12px;background:#f9fafc;border:2px solid #dde1ea;border-radius:7px;color:#1a1a2e;font-size:11px;font-family:inherit;font-weight:900;outline:none}
.btn{padding:10px 20px;background:#4455ff;border:none;border-radius:8px;color:#fff;font-size:11px;font-family:inherit;cursor:pointer;letter-spacing:1.5px;font-weight:900;transition:background .2s;white-space:nowrap}
.btn:hover{background:#3344dd}.btn:disabled{background:#aab;cursor:not-allowed}
.btn-g{background:#22aa66}.btn-g:hover{background:#1a8850}
.btn-o{background:#cc8800}.btn-o:hover{background:#aa6600}
.btn-r{background:#cc3333}.btn-r:hover{background:#aa2222}
.btn-sm{padding:6px 12px;font-size:10px}
.prog-wrap{background:#fff;border:2px solid #dde1ea;border-radius:12px;padding:14px 18px;margin-bottom:14px}
.prog-bar-bg{background:#eef0ff;border-radius:8px;height:8px;overflow:hidden;margin-top:8px}
.prog-bar{background:linear-gradient(90deg,#4455ff,#22aa66);height:8px;border-radius:8px;transition:width .4s;width:0%}
.prog-info{display:flex;justify-content:space-between;font-size:10px;color:#888;margin-top:5px}
.summary{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.chip{padding:6px 14px;border-radius:18px;font-size:10.5px;font-weight:900}
.chip-b{background:#eef0ff;color:#4455ff;border:1.5px solid #c0c8ff}
.chip-g{background:#e6fff3;color:#22aa66;border:1.5px solid #99ddbb}
.chip-r{background:#fff5f5;color:#cc3333;border:1.5px solid #ffcccc}
.chip-gr{background:#f5f6fa;color:#666;border:1.5px solid #dde1ea}
.tbl-wrap{background:#fff;border:2px solid #dde1ea;border-radius:14px;overflow:hidden}
.tbl-head,.row-item{display:grid;grid-template-columns:38px 52px minmax(120px,1fr) 110px minmax(100px,1.2fr) 82px 128px;padding:10px 13px;align-items:center;gap:4px}
.tbl-head{background:#f0f2ff;border-bottom:2px solid #dde1ea;font-size:9px;color:#4455ff;letter-spacing:2px;font-weight:900;text-transform:uppercase}
.row-item{border-bottom:1.5px solid #f0f1f6;transition:background .15s}
.row-item:last-child{border-bottom:none}.row-item:hover{background:#fafbff}
.num-c{color:#aaa;font-size:12px}.uid-c{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.email-c{font-size:11px;color:#333;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.otp-c{font-size:18px;font-weight:900;color:#4455ff;letter-spacing:3px;cursor:pointer}
.otp-c:hover{color:#2233bb}.otp-none{color:#ccc;font-size:12px}.otp-err{color:#cc3333;font-size:9px;word-break:break-all}
.subj-c{color:#666;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.date-c{color:#999;font-size:10px}
.act-c{display:flex;flex-direction:column;gap:4px}
.fb{padding:4px 9px;border:2px solid #99ddbb;border-radius:6px;background:#e6fff3;color:#22aa66;font-size:9px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap}
.fb:hover{background:#22aa66;color:#fff}.fb:disabled{background:#f5f6fa;border-color:#dde1ea;color:#ccc;cursor:default}
.cb{padding:4px 9px;border:2px solid #c0c8ff;border-radius:6px;background:#eef0ff;color:#4455ff;font-size:9px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap}
.cb:hover{background:#4455ff;color:#fff}.cb:disabled{background:#f5f6fa;border-color:#dde1ea;color:#ccc;cursor:default}
.ub{padding:4px 9px;border:2px solid #ffddaa;border-radius:6px;background:#fff8e6;color:#cc8800;font-size:9px;font-family:inherit;cursor:pointer;font-weight:900;transition:all .15s;white-space:nowrap}
.ub:hover{background:#cc8800;color:#fff}.ub:disabled{background:#f5f6fa;border-color:#dde1ea;color:#ccc;cursor:default}
.st{display:inline-block;padding:3px 8px;border-radius:9px;font-size:9px;font-weight:900;text-transform:uppercase;white-space:nowrap}
.st-p{background:#f5f6fa;color:#bbb;border:1.5px solid #dde1ea}
.st-f{background:#fff8e6;color:#cc8800;border:1.5px solid #ffddaa}
.st-ok{background:#e6fff3;color:#22aa66;border:1.5px solid #99ddbb}
.st-no{background:#f5f6fa;color:#999;border:1.5px solid #dde1ea}
.st-e{background:#fff5f5;color:#cc3333;border:1.5px solid #ffcccc}
.toast{position:fixed;bottom:18px;right:18px;background:#1a1a2e;color:#fff;padding:10px 20px;border-radius:10px;font-size:11px;font-weight:900;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
.bulk-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;padding:10px;background:#f0f2ff;border-radius:8px;border:1.5px solid #c0c8ff}
@media(max-width:650px){.tbl-head,.row-item{grid-template-columns:28px 38px 1fr 90px 80px;}.subj-c,.date-c{display:none}}
</style></head>
<body>
<header>
  <span class="brand">🔐 OTP READER</span>
  <div class="hinfo">
    👤 <b>__USER_NAME__</b><br>
    📋 __ACC_COUNT__ accounts &nbsp;·&nbsp; ⏰ Expires: __EXPIRES__
  </div>
</header>
<div class="wrap">

  <div class="panel">
    <div class="ptitle">⚡ YOUR ACCOUNTS — __ACC_COUNT__ total</div>
    <div class="row">
      <select id="lm">
        <option value="10">Last 10 emails</option>
        <option value="20" selected>Last 20 emails</option>
        <option value="50">Last 50 emails</option>
        <option value="100">Last 100 emails</option>
      </select>
      <button class="btn btn-g" id="fetchAllBtn" onclick="fetchAll()">⚡ FETCH ALL OTPs</button>
      <button class="btn btn-sm" style="background:#555" onclick="stopFetch()">⏹ STOP</button>
    </div>
    <div class="bulk-row" style="margin-top:10px">
      <span style="font-size:10px;color:#4455ff;font-weight:900;align-self:center">BULK COPY:</span>
      <button class="btn btn-sm btn-o" onclick="copyAllUID()">👥 ALL UID|PASS</button>
      <button class="btn btn-sm btn-g" onclick="copyAllOTP()">📋 ALL OTPs</button>
      <button class="btn btn-sm btn-o" onclick="copyFoundUID()">👥 FOUND UID|PASS</button>
      <button class="btn btn-sm btn-g" onclick="copyFoundOTP()">📋 FOUND OTPs ONLY</button>
    </div>
  </div>

  <div class="prog-wrap" id="progWrap" style="display:none">
    <div style="display:flex;justify-content:space-between"><span style="font-size:11px;color:#4455ff;font-weight:900">⚡ FETCHING...</span><span id="progNums" style="font-size:12px;font-weight:900">0/0</span></div>
    <div class="prog-bar-bg"><div class="prog-bar" id="progBar"></div></div>
    <div class="prog-info"><span id="progStatus">Starting...</span></div>
  </div>

  <div class="summary" id="summaryBar" style="display:none"></div>

  <div class="tbl-wrap">
    <div class="tbl-head">
      <div>#</div><div>UID</div><div>EMAIL</div><div>LATEST OTP</div><div>SUBJECT</div><div>DATE</div><div>ACTION</div>
    </div>
    <div id="tableBody"></div>
  </div>

</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN     = "__TOKEN__";
const RAW_LINES = __LINES_JSON__;

let accounts  = RAW_LINES.map((line,i)=>{
  const p = line.split('|');
  let uid='',email='';
  if(p.length>=5 && !p[0].includes('@')){ uid=p[0].trim(); email=p[2].trim(); }
  else { email=p[0].trim(); }
  return {idx:i, uid, email, line};
});
let results   = {};
let isFetching= false;
let stopFlag  = false;

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function toast(msg,dur=2200){
  const t=document.getElementById('toast');
  t.textContent=msg;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),dur);
}

// ── Build table ───────────────────────────────────────────────────────
function buildTable(){
  document.getElementById('tableBody').innerHTML = accounts.map(a=>`
    <div class="row-item" id="row_${a.idx}">
      <div class="num-c">${a.idx+1}</div>
      <div class="uid-c" title="${esc(a.uid)}">${a.uid?esc(a.uid):'<span style="color:#ccc">—</span>'}</div>
      <div class="email-c" title="${esc(a.email)}">${esc(a.email)}</div>
      <div id="otp_${a.idx}" class="otp-none">—</div>
      <div id="subj_${a.idx}" class="subj-c"></div>
      <div id="date_${a.idx}" class="date-c"></div>
      <div class="act-c">
        <button class="fb" id="fb_${a.idx}" onclick="fetchOne(${a.idx})">⚡ FETCH</button>
        <button class="cb" id="cb_${a.idx}" disabled onclick="copyOTP(${a.idx})">📋 COPY OTP</button>
        <button class="ub" id="ub_${a.idx}" ${a.uid?'':'disabled'} onclick="copyUID(${a.idx})">👤 UID|PASS</button>
        <span class="st st-p" id="st_${a.idx}">PENDING</span>
      </div>
    </div>`).join('');
}
buildTable();

// ── Fetch helpers ─────────────────────────────────────────────────────
function setStatus(idx,type,label){
  const el=document.getElementById('st_'+idx);
  if(el){el.className='st st-'+type;el.textContent=label;}
}

function setOTP(idx,otp,err,subj,date){
  const oe=document.getElementById('otp_'+idx);
  const se=document.getElementById('subj_'+idx);
  const de=document.getElementById('date_'+idx);
  const cb=document.getElementById('cb_'+idx);
  if(otp){
    if(oe){oe.className='otp-c';oe.textContent=otp;oe.onclick=()=>copyOTP(idx);}
    if(cb) cb.disabled=false;
  } else if(err){
    if(oe){oe.className='otp-err';oe.textContent=err.slice(0,80);oe.onclick=null;}
  } else {
    if(oe){oe.className='otp-none';oe.textContent='—';oe.onclick=null;}
  }
  if(se) se.textContent=subj||'';
  if(de) de.textContent=date||'';
}

async function fetchOne(idx){
  const a=accounts[idx]; if(!a) return;
  const fb=document.getElementById('fb_'+idx);
  if(fb){fb.disabled=true;fb.textContent='⏳';}
  setStatus(idx,'f','⏳ FETCHING');
  const limit=parseInt(document.getElementById('lm').value);
  try{
    const res=await fetch('/u/'+TOKEN+'/fetch',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({line:a.line,limit})
    }).then(r=>r.json());
    results[idx]=res;
    if(res.error){setStatus(idx,'e','❌ ERR');setOTP(idx,null,res.error,'','');}
    else if(res.otp){setStatus(idx,'ok','✅ FOUND');setOTP(idx,res.otp,'',res.subject||'',res.date||'');}
    else{setStatus(idx,'no','📭 NO OTP');setOTP(idx,null,'','','');}
  } catch(e){
    results[idx]={error:e.message};
    setStatus(idx,'e','❌ ERR');setOTP(idx,null,e.message,'','');
  }
  if(fb){fb.disabled=false;fb.textContent='⚡ FETCH';}
  updateSummary();
}

async function fetchAll(){
  if(isFetching) return;
  isFetching=true; stopFlag=false;
  document.getElementById('progWrap').style.display='block';
  const progBar=document.getElementById('progBar');
  const progNums=document.getElementById('progNums');
  const progStatus=document.getElementById('progStatus');
  let done=0;
  for(const a of accounts){
    if(stopFlag) break;
    setStatus(a.idx,'f','⏳');
    progStatus.textContent='Fetching: '+a.email+' ('+(done+1)+'/'+accounts.length+')';
    progNums.textContent=done+'/'+accounts.length;
    progBar.style.width=(done/accounts.length*100)+'%';
    await fetchOne(a.idx);
    done++;
  }
  progBar.style.width=stopFlag?progBar.style.width:'100%';
  progNums.textContent=done+'/'+accounts.length;
  progStatus.textContent=(stopFlag?'⏹ Stopped':'✅ Done!')+' — '+done+' processed';
  isFetching=false;
  updateSummary();
}

function stopFetch(){ stopFlag=true; isFetching=false; }

// ── Copy functions ────────────────────────────────────────────────────
function copyOTP(idx){
  const r=results[idx]; if(r&&r.otp) navigator.clipboard.writeText(r.otp).then(()=>toast('📋 OTP copied: '+r.otp));
}
function copyUID(idx){
  const a=accounts[idx]; if(!a) return;
  const p=a.line.split('|');
  const pass=p.length>=5?p[1].trim():(p.length>=2?p[1].trim():'');
  if(!a.uid||!pass){toast('❌ UID or PASS missing');return;}
  navigator.clipboard.writeText(a.uid+'|'+pass).then(()=>toast('📋 UID|PASS copied!'));
}
function copyAllUID(){
  const lines=accounts.map(a=>{
    const p=a.line.split('|');
    const uid=a.uid||(p.length>=5?p[0].trim():'');
    const pass=p.length>=5?p[1].trim():(p.length>=2?p[1].trim():'');
    return uid&&pass?uid+'|'+pass:null;
  }).filter(Boolean);
  if(!lines.length){toast('❌ Koi UID|PASS nahi');return;}
  navigator.clipboard.writeText(lines.join('\\n')).then(()=>toast('✅ '+lines.length+' UID|PASS copied!'));
}
function copyAllOTP(){
  const lines=accounts.map(a=>{const r=results[a.idx];return r&&r.otp?r.otp:null;}).filter(Boolean);
  if(!lines.length){toast('❌ OTP nahi mila — pehle FETCH karo');return;}
  navigator.clipboard.writeText(lines.join('\\n')).then(()=>toast('✅ '+lines.length+' OTPs copied!'));
}
function copyFoundUID(){
  const lines=accounts.filter(a=>results[a.idx]&&results[a.idx].otp).map(a=>{
    const p=a.line.split('|');
    const uid=a.uid||(p.length>=5?p[0].trim():'');
    const pass=p.length>=5?p[1].trim():'';
    return uid&&pass?uid+'|'+pass:null;
  }).filter(Boolean);
  if(!lines.length){toast('❌ OTP found accounts ka UID|PASS nahi mila');return;}
  navigator.clipboard.writeText(lines.join('\\n')).then(()=>toast('✅ '+lines.length+' Found UID|PASS copied!'));
}
function copyFoundOTP(){
  const lines=accounts.filter(a=>results[a.idx]&&results[a.idx].otp).map(a=>results[a.idx].otp);
  if(!lines.length){toast('❌ Koi OTP found nahi');return;}
  navigator.clipboard.writeText(lines.join('\\n')).then(()=>toast('✅ '+lines.length+' Found OTPs copied!'));
}

// ── Summary ───────────────────────────────────────────────────────────
function updateSummary(){
  const total=accounts.length;
  const found=Object.values(results).filter(r=>r.otp).length;
  const noOTP=Object.values(results).filter(r=>!r.error&&!r.otp).length;
  const err=Object.values(results).filter(r=>r.error).length;
  const pend=total-found-noOTP-err;
  const bar=document.getElementById('summaryBar');
  bar.style.display='flex';
  bar.innerHTML=
    '<div class="chip chip-b">📋 TOTAL: '+total+'</div>'+
    '<div class="chip chip-g">✅ FOUND: '+found+'</div>'+
    '<div class="chip chip-gr">📭 NO OTP: '+noOTP+'</div>'+
    '<div class="chip chip-r">❌ ERR: '+err+'</div>'+
    (pend>0?'<div class="chip chip-gr">⏳ PENDING: '+pend+'</div>':'');
}
updateSummary();
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
