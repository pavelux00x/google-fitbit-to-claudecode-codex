#!/usr/bin/env python3
"""google-health-cli - your Fitbit / Google Health data on your own machine.

The old Fitbit Web API was shut down on 30 October 2026. The only way to read
your own data is now the Google Health API (health.googleapis.com).

No Python dependencies: standard library only (3.9+). `gcloud` is needed for
the initial setup.

    python3 health.py setup       # Cloud project, API, OAuth, login
    python3 health.py fetch       # download data into ./health-data/
    python3 health.py whoami      # check that the token works
    python3 health.py selfcheck   # offline test of the internal logic
"""

import argparse
import base64
import getpass
import hashlib
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://health.googleapis.com/v4"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SERVICE = "health.googleapis.com"
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/callback"
ME = Path(__file__).name

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

# dataType -> maximum number of days per single rollup call (Google's limit).
ROLLUP_TYPES = {
    "steps": 90,
    "distance": 90,
    "weight": 90,
    "active-minutes": 14,
    "heart-rate": 14,
    "total-calories": 14,
}

CONSOLE_URLS = [
    "https://console.cloud.google.com/auth/overview?project={p}",
    "https://console.cloud.google.com/auth/audience?project={p}",
    "https://console.cloud.google.com/auth/scopes?project={p}",
    "https://console.cloud.google.com/auth/clients?project={p}",
]

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "google-health" / "config.json"

LANG = "en"
YES = ("y", "yes", "s", "si", "sì")

TEXT = {
    "en": {
        "s1": "Google account",
        "s1_login": "No active gcloud account: opening the login.",
        "s1_acct": "Account: {a}",
        "s1_warn": "IMPORTANT: this must be the Google account your Fitbit is linked to.",
        "s1_ask": "Is that the right one? [y/N] ",
        "s1_abort": "run `gcloud auth login` with the right account, then run setup again.",
        "s2": "Google Cloud project",
        "s2_using": "Using existing project: {p}",
        "s2_creating": "Creating project {p} ...",
        "s2_failed": ("could not create the project:\n{e}\n"
                      "        Usually the project quota is full. Create one by hand at\n"
                      "        https://console.cloud.google.com/projectcreate and run again with\n"
                      "        python3 {f} setup --project NAME"),
        "s3": "Enabling the Google Health API",
        "s3_ok": "{s} enabled.",
        "s3_failed": ("enable failed:\n{e}\n"
                      "        If it mentions billing, link a billing account at\n"
                      "        https://console.cloud.google.com/billing/linkedaccount?project={p}\n"
                      "        then run setup again with --project {p}"),
        "found": "\nFound credentials ({src}). Checking them with Google, no browser needed...",
        "redo": "The OAuth client already exists. Redo the console steps anyway? [y/N] ",
        "s4": "Four manual steps in the console (Google exposes no API for these)",
        "s4_intro": "One page at a time. Do what it says, then press ENTER.",
        "s4_enter": "\n  ENTER when done... ",
        "s5": "Client ID and client secret",
        "s5_found": "Found in Downloads: {n}",
        "s5_ask_found": "ENTER to use it, or another path ('m' = type them in): ",
        "s5_ask": "Path to the downloaded JSON ('m' = type them in): ",
        "s5_nofile": ("file not found: {p}\n"
                      "        Drag the file into the terminal, or paste the path without quotes."),
        "s5_nocreds": "no client_id / client_secret inside {n}.",
        "s5_nouri": "[warning] this client is missing the redirect URI {u}",
        "s5_uris": "         it has instead: {u}",
        "s5_read": "Read from {n}",
        "s5_cid": "Client ID: ",
        "s5_csec": "Client secret (will not be echoed): ",
        "s5_warn_cid": "[warning] the client ID does not end with .apps.googleusercontent.com, check it.",
        "s5_warn_proj": "[warning] this client belongs to another project: it says {a}, the project is {b}.",
        "s5_verify": "Checking the client with Google...",
        "s5_continue": "Continue anyway? [y/N] ",
        "s5_abort": "aborted. Fix the OAuth client in the console and run again.",
        "s6": "Login and consent",
        "s6_open": "\nOpening the browser. If it does not open, paste this link:\n\n{u}\n",
        "s6_tunnel": (
            "\nThere is no browser on this machine.\n\n"
            "  From your own computer, reconnect forwarding the port:\n\n"
            "      ssh -L {p}:localhost:{p}  user@this-server\n\n"
            "  then, in that same session, run this setup again and open the link\n"
            "  below in your own browser. localhost:{p} on your machine becomes\n"
            "  localhost:{p} here, so the consent comes back through the tunnel.\n\n"
            "  Alternative: run setup on your laptop and copy\n"
            "  ~/.config/google-health/config.json over to this machine.\n\n{u}\n"),
        "s6_wait": "Waiting for consent (up to 5 minutes)...",
        "s6_noresp": "no answer from the browser before the timeout.",
        "s6_state": "state mismatch: possibly a forged request, login aborted.",
        "s6_denied": "consent was not granted: {e}",
        "s6_norefresh": ("Google returned no refresh token.\n"
                         "        Revoke access at https://myaccount.google.com/permissions and log in again."),
        "s6_saved": "Credentials saved to {p} (mode 600).",
        "s7": "Check",
        "s7_ok": "\nAll set.  Now:  python3 {f} fetch",
        "s7_note": ("Note: in Testing mode the login expires after 7 days.\n"
                    "      To make it permanent, publish the app from the Audience page."),
        "page_ok": "Signed in.",
        "page_ko": "Access denied or cancelled.",
        "page_close": "You can close this page and go back to the terminal.",
        "port_busy": "port {p} is busy ({e}). Close whatever is using it and try again.",
        "cl_ok": "OAuth client is valid: it exists on Google and the secret is right",
        "cl_ko": ("Google does not recognise this client: wrong ID or secret, "
                  "or the client was deleted"),
        "cl_weird": "unexpected answer from Google: {b}",
        "cl_nonet": "no connection: {e}",
        "no_config": "configuration not found. Run this first:  python3 {f} setup",
        "bad_grant": ("the refresh token is no longer valid.\n"
                      "        In Testing mode Google expires them after 7 days.\n"
                      "        Log in again:  python3 {f} setup --login-only\n"
                      "        (to stop this from recurring, publish the app from the Audience page)"),
        "token_ko": "token request refused (HTTP {c}):\n{b}",
        "err403": ("403 on {p}.\n"
                   "        Usually a scope is missing on the 'Data access' page, or your\n"
                   "        account is not in the Test users list. See step 4 of setup."),
        "f_skip": "skipped ({e})",
        "f_count": "{n} records",
        "f_unknown": "unsupported type, skipping",
        "f_written": "\nWrote {f}",
        "relogin": "\nDone. Token renewed.",
        "no_gcloud": ("gcloud not found. Install it:  https://cloud.google.com/sdk/docs/install\n"
                      "        on macOS:  brew install --cask gcloud-cli"),
        "gcloud_ko": "`gcloud {a}` failed:\n{e}",
        "interrupted": "interrupted.",
        "pages": [
            ("Google Auth Platform (first time only, on a new project)",
             ["if it says the Google Auth Platform is not configured yet, click 'Get started':",
              "   1 App information -> Name: Google Health CLI | Email: your address",
              "   2 Audience        -> External",
              "   3 Contact info    -> your address",
              "   4 Finish          -> accept the policy, then 'Create'",
              "if the page already shows a configured app, move on without touching anything"]),
            ("Audience (who may use the app)",
             ["'Publishing status' must be 'Testing'",
              "'Test users' -> '+ Add users' -> YOUR Google address (the Fitbit one) -> Save"]),
            ("Data access (the scopes)",
             ["'Add or remove scopes' -> search for 'Google Health API'",
              "tick: profile.readonly, activity_and_fitness.readonly, sleep.readonly,",
              "      health_metrics_and_measurements.readonly",
              "'Update' and then 'Save'"]),
            ("Clients (the OAuth client)",
             ["'Create client' -> Application type: Web application",
              "'Authorized redirect URIs' -> add exactly:  " + REDIRECT_URI,
              "'Create', then download the JSON or copy client ID and secret"]),
        ],
    },
    "it": {
        "s1": "Account Google",
        "s1_login": "Nessun account attivo in gcloud: apro il login.",
        "s1_acct": "Account: {a}",
        "s1_warn": "ATTENZIONE: deve essere l'account Google a cui e' legato il Fitbit.",
        "s1_ask": "E' quello giusto? [s/N] ",
        "s1_abort": "rilancia `gcloud auth login` con l'account giusto, poi ripeti il setup.",
        "s2": "Progetto Google Cloud",
        "s2_using": "Uso il progetto esistente: {p}",
        "s2_creating": "Creo il progetto {p} ...",
        "s2_failed": ("creazione del progetto fallita:\n{e}\n"
                      "        Spesso e' la quota progetti esaurita. Creane uno a mano su\n"
                      "        https://console.cloud.google.com/projectcreate e rilancia con\n"
                      "        python3 {f} setup --project NOME"),
        "s3": "Abilito la Google Health API",
        "s3_ok": "{s} abilitata.",
        "s3_failed": ("enable fallito:\n{e}\n"
                      "        Se parla di billing, collega un account di fatturazione su\n"
                      "        https://console.cloud.google.com/billing/linkedaccount?project={p}\n"
                      "        e rilancia il setup con --project {p}"),
        "found": "\nTrovate credenziali ({src}). Le verifico su Google, senza aprire il browser...",
        "redo": "Il client OAuth esiste gia'. Rifaccio comunque i passaggi in console? [s/N] ",
        "s4": "Quattro passaggi a mano nella console (Google non espone API per questi)",
        "s4_intro": "Apro una pagina alla volta. Fai quello che c'e' scritto, poi premi INVIO.",
        "s4_enter": "\n  INVIO quando fatto... ",
        "s5": "Client ID e client secret",
        "s5_found": "Trovato in Downloads: {n}",
        "s5_ask_found": "INVIO per usarlo, oppure un altro percorso ('m' = a mano): ",
        "s5_ask": "Percorso del JSON scaricato ('m' = a mano): ",
        "s5_nofile": ("file non trovato: {p}\n"
                      "        Trascina il file nel terminale, oppure incolla il percorso senza apici."),
        "s5_nocreds": "in {n} non trovo client_id / client_secret.",
        "s5_nouri": "[avviso] in questo client manca il redirect URI {u}",
        "s5_uris": "         ci sono invece: {u}",
        "s5_read": "Letti da {n}",
        "s5_cid": "Client ID: ",
        "s5_csec": "Client secret (non verra' mostrato): ",
        "s5_warn_cid": "[avviso] il client ID non finisce con .apps.googleusercontent.com, controlla.",
        "s5_warn_proj": "[avviso] questo client appartiene a un altro progetto: dice {a}, il progetto e' {b}.",
        "s5_verify": "Verifico il client su Google...",
        "s5_continue": "Continuo lo stesso? [s/N] ",
        "s5_abort": "interrotto. Sistema il client OAuth in console e rilancia.",
        "s6": "Login e consenso",
        "s6_open": "\nApro il browser. Se non si apre, incolla questo link:\n\n{u}\n",
        "s6_tunnel": (
            "\nSu questa macchina non c'e' un browser.\n\n"
            "  Dal tuo computer, riconnettiti inoltrando la porta:\n\n"
            "      ssh -L {p}:localhost:{p}  utente@questo-server\n\n"
            "  poi, nella stessa sessione, rilancia questo setup e apri il link\n"
            "  qui sotto nel TUO browser. localhost:{p} sulla tua macchina diventa\n"
            "  localhost:{p} qui, quindi il consenso torna indietro dal tunnel.\n\n"
            "  In alternativa: fai il setup sul portatile e copia qui\n"
            "  ~/.config/google-health/config.json\n\n{u}\n"),
        "s6_wait": "In attesa del consenso (max 5 minuti)...",
        "s6_noresp": "nessuna risposta dal browser entro il timeout.",
        "s6_state": "state non corrispondente: possibile richiesta contraffatta, login annullato.",
        "s6_denied": "consenso non concesso: {e}",
        "s6_norefresh": ("Google non ha restituito un refresh token.\n"
                         "        Revoca l'accesso su https://myaccount.google.com/permissions e rifai il login."),
        "s6_saved": "Credenziali salvate in {p} (permessi 600).",
        "s7": "Verifica",
        "s7_ok": "\nTutto pronto.  Ora:  python3 {f} fetch",
        "s7_note": ("Nota: in modalita' Testing il login scade dopo 7 giorni.\n"
                    "      Per renderlo permanente pubblica l'app dalla pagina Audience."),
        "page_ok": "Accesso riuscito.",
        "page_ko": "Accesso negato o annullato.",
        "page_close": "Puoi chiudere questa pagina e tornare al terminale.",
        "port_busy": "la porta {p} e' occupata ({e}). Chiudi il processo che la usa e riprova.",
        "cl_ok": "client OAuth valido: esiste su Google e il secret e' giusto",
        "cl_ko": ("Google non riconosce questo client: ID o secret sbagliati, "
                  "oppure il client e' stato cancellato"),
        "cl_weird": "risposta inattesa da Google: {b}",
        "cl_nonet": "nessuna connessione: {e}",
        "no_config": "configurazione non trovata. Lancia prima:  python3 {f} setup",
        "bad_grant": ("il refresh token non e' piu' valido.\n"
                      "        In modalita' Testing Google li scade dopo 7 giorni.\n"
                      "        Rifai il login:  python3 {f} setup --login-only\n"
                      "        (per non ripetere: pubblica l'app dalla pagina Audience)"),
        "token_ko": "richiesta token rifiutata (HTTP {c}):\n{b}",
        "err403": ("403 su {p}.\n"
                   "        Di solito manca lo scope nella pagina 'Data access', oppure\n"
                   "        il tuo account non e' tra i Test users. Vedi il punto 4 del setup."),
        "f_skip": "salto ({e})",
        "f_count": "{n} record",
        "f_unknown": "tipo non gestito, salto",
        "f_written": "\nScritto {f}",
        "relogin": "\nFatto. Token rinnovato.",
        "no_gcloud": ("gcloud non trovato. Installalo:  https://cloud.google.com/sdk/docs/install\n"
                      "        su macOS:  brew install --cask gcloud-cli"),
        "gcloud_ko": "`gcloud {a}` e' fallito:\n{e}",
        "interrupted": "interrotto.",
        "pages": [
            ("Google Auth Platform (solo la prima volta, su un progetto nuovo)",
             ["se dice 'Google Auth Platform non ancora configurata' clicca 'Inizia' e compila:",
              "   1 Informazioni sull'app -> Nome: Google Health CLI | Email: il tuo indirizzo",
              "   2 Pubblico              -> Esterno",
              "   3 Dati di contatto      -> il tuo indirizzo",
              "   4 Fine                  -> accetta le norme, poi 'Crea'",
              "se la pagina mostra gia' un'app configurata, vai avanti senza toccare nulla"]),
            ("Audience (chi puo' usare l'app)",
             ["'Publishing status' deve essere 'Testing'",
              "'Test users' -> '+ Add users' -> il TUO indirizzo Google (quello del Fitbit) -> Save"]),
            ("Data access (gli scope)",
             ["'Add or remove scopes' -> cerca 'Google Health API'",
              "spunta: profile.readonly, activity_and_fitness.readonly, sleep.readonly,",
              "        health_metrics_and_measurements.readonly",
              "'Update' e poi 'Save'"]),
            ("Clients (il client OAuth)",
             ["'Create client' -> Application type: Web application",
              "'Authorized redirect URIs' -> aggiungi esattamente:  " + REDIRECT_URI,
              "'Create', poi scarica il JSON o copia client ID e secret"]),
        ],
    },
}


def t(key, **kw):
    s = TEXT[LANG][key]
    return s.format(**kw) if kw else s


def choose_language():
    """The very first prompt: which language the walkthrough should speak."""
    print("Language / Lingua:  [1] English   [2] Italiano")
    try:
        a = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "en"
    return "it" if a in ("2", "it", "ita", "italiano") else "en"


# ---------------------------------------------------------------- helpers

def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce_challenge(verifier):
    """S256: SHA-256 of the verifier, base64url without padding (RFC 7636)."""
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def civil(d):
    """A CivilDateTime at midnight, the shape dailyRollUp expects."""
    return {
        "date": {"year": d.year, "month": d.month, "day": d.day},
        "time": {"hours": 0, "minutes": 0, "seconds": 0, "nanos": 0},
    }


def chunks(start, end, max_days):
    """Split [start, end) into contiguous ranges of at most max_days days."""
    out = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=max_days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def clean_path(raw):
    """A pasted path often carries quotes (Finder) or escaped spaces (drag & drop)."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    return s.replace("\\ ", " ").strip()


def headless(env=None, platform=None):
    """True when there is no browser to open here: a VPS over SSH, typically."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform == "darwin" or platform.startswith("win"):
        return False
    return not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def die(msg, code=1):
    print(f"\n[error] {msg}", file=sys.stderr)
    sys.exit(code)


def step(n, title):
    print(f"\n\033[1m--- {n}. {title}\033[0m")


def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        die(t("interrupted"))


def yes(answer):
    return answer.strip().lower() in YES


def load_config():
    if not CONFIG.exists():
        die(t("no_config", f=ME))
    return json.loads(CONFIG.read_text())


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG, 0o600)


# ---------------------------------------------------------------- gcloud

def gcloud(*args, check=True):
    exe = shutil.which("gcloud")
    if not exe:
        die(t("no_gcloud"))
    p = subprocess.run([exe, *args], text=True, capture_output=True)
    if check and p.returncode != 0:
        die(t("gcloud_ko", a=" ".join(args), e=p.stderr.strip()))
    return p


def gcloud_account():
    p = gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)", check=False)
    return p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""


# ---------------------------------------------------------------- OAuth

class _Callback(http.server.BaseHTTPRequestHandler):
    query = None

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        if parts.path != "/callback":
            self.send_error(404)
            return
        _Callback.query = dict(urllib.parse.parse_qsl(parts.query))
        msg = t("page_ok") if "code" in _Callback.query else t("page_ko")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(("<html><body style='font:16px sans-serif;padding:3em'>"
                          f"<h2>{msg}</h2><p>{t('page_close')}</p></body></html>").encode())

    def log_message(self, *a):
        pass


def token_request(data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if "invalid_grant" in detail:
            die(t("bad_grant", f=ME))
        die(t("token_ko", c=e.code, b=detail))


def client_check(client_id, client_secret):
    """Does this OAuth client really exist? Without opening a browser.

    We ask for a refresh with an obviously fake token:
      - wrong credentials -> Google answers 'invalid_client'
      - right credentials -> 'invalid_grant' (the token is fake, the client is not)
    """
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": "invalid-probe", "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        urllib.request.urlopen(req, timeout=20)
        return True, t("cl_ok")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "invalid_client" in body:
            return False, t("cl_ko")
        if "invalid_grant" in body:
            return True, t("cl_ok")
        return False, t("cl_weird", b=body[:200])
    except urllib.error.URLError as e:
        return False, t("cl_nonet", e=e)


def do_login(client_id, client_secret, open_browser=True):
    """Authorization-code flow + PKCE over loopback. Returns the refresh token."""
    verifier = b64url(secrets.token_bytes(32))
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    })

    try:
        server = http.server.HTTPServer(("127.0.0.1", PORT), _Callback)
    except OSError as e:
        die(t("port_busy", p=PORT, e=e))
    server.timeout = 300

    if open_browser:
        print(t("s6_open", u=url))
        webbrowser.open(url)
    else:
        print(t("s6_tunnel", p=PORT, u=url))
    print(t("s6_wait"))
    _Callback.query = None
    server.handle_request()
    server.server_close()

    q = _Callback.query
    if not q:
        die(t("s6_noresp"))
    if q.get("state") != state:
        die(t("s6_state"))
    if "code" not in q:
        die(t("s6_denied", e=q.get("error", "unknown")))

    tok = token_request({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": q["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })
    if "refresh_token" not in tok:
        die(t("s6_norefresh"))
    return tok["refresh_token"]


def access_token(cfg):
    """A valid token, refreshed when expired. Updates the cache on disk."""
    now = datetime.now(timezone.utc).timestamp()
    if cfg.get("access_token") and cfg.get("expires_at", 0) > now + 60:
        return cfg["access_token"]
    tok = token_request({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    })
    cfg["access_token"] = tok["access_token"]
    cfg["expires_at"] = now + int(tok.get("expires_in", 3600))
    save_config(cfg)
    return cfg["access_token"]


# ---------------------------------------------------------------- credentials

def _creds_from_json(f):
    try:
        blob = json.loads(Path(f).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    node = blob.get("web") or blob.get("installed") or blob
    return node if node.get("client_id") and node.get("client_secret") else None


def downloaded_clients():
    """client_secret_*.json files in ~/Downloads, newest first."""
    found = list(Path.home().glob("Downloads/client_secret_*.json"))
    return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)


def existing_credentials():
    """Credentials already available: saved config first, then ~/Downloads."""
    if CONFIG.exists():
        try:
            c = json.loads(CONFIG.read_text())
            if c.get("client_id") and c.get("client_secret"):
                return c["client_id"], c["client_secret"], "saved config"
        except (OSError, json.JSONDecodeError):
            pass
    for f in downloaded_clients():
        node = _creds_from_json(f)
        if node:
            return node["client_id"], node["client_secret"], f.name
    return "", "", ""


def read_credentials(project=""):
    """Read client id and secret from the downloaded JSON, or by hand."""
    found = downloaded_clients()
    if found:
        print(t("s5_found", n=found[0].name))
        raw = ask(t("s5_ask_found"))
    else:
        raw = ask(t("s5_ask"))

    if raw.lower() == "m":
        path = None
    elif raw:
        path = Path(clean_path(raw)).expanduser()
    elif found:
        path = found[0]
    else:
        path = None

    if path is not None:
        if not path.is_file():
            die(t("s5_nofile", p=path))
        node = _creds_from_json(path)
        if not node:
            die(t("s5_nocreds", n=path.name))
        cid, csec = node["client_id"], node["client_secret"]
        uris = node.get("redirect_uris") or []
        if uris and REDIRECT_URI not in uris:
            print(t("s5_nouri", u=REDIRECT_URI))
            print(t("s5_uris", u=", ".join(uris)))
        print(t("s5_read", n=path.name))
    else:
        cid = ask(t("s5_cid"))
        csec = getpass.getpass(t("s5_csec")).strip()

    if not cid.endswith(".apps.googleusercontent.com"):
        print(t("s5_warn_cid"))
    if project:
        num = gcloud("projects", "describe", project,
                     "--format=value(projectNumber)", check=False).stdout.strip()
        if num and not cid.startswith(num + "-"):
            print(t("s5_warn_proj", a=cid.split("-")[0], b=num))
    return cid, csec


# ---------------------------------------------------------------- API

def api(cfg, path, params=None, body=None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", "Bearer " + access_token(cfg))
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        if e.code == 403:
            die(t("err403", p=path) + "\n        " + detail)
        raise RuntimeError(f"HTTP {e.code} on {path}: {detail}") from None


def paginate(cfg, path, params):
    """Follow nextPageToken and concatenate every page."""
    items, token = [], None
    while True:
        p = dict(params)
        if token:
            p["pageToken"] = token
        page = api(cfg, path, params=p)
        items += page.get("dataPoints") or []
        token = page.get("nextPageToken")
        if not token:
            return items


def rollup(cfg, data_type, start, end, max_days):
    out = []
    for a, b in chunks(start, end, max_days):
        body = {"range": {"start": civil(a), "end": civil(b)}, "windowSizeDays": 1}
        token = None
        while True:
            if token:
                body["pageToken"] = token
            page = api(cfg, f"users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", body=body)
            out += page.get("rollupDataPoints") or []
            token = page.get("nextPageToken")
            if not token:
                body.pop("pageToken", None)
                break
    return out


def sleep_points(cfg, start):
    # No dataSourceFamily: for 'sleep' the API rejects the parameter even with
    # its default value (INVALID_DATA_POINT_DATA_SOURCE_FAMILY).
    # pageSize is capped at 25 for sleep.
    flt = f'sleep.interval.civil_end_time >= "{start.isoformat()}T00:00:00"'
    return paginate(cfg, "users/me/dataTypes/sleep/dataPoints",
                    {"filter": flt, "pageSize": 25})


# ---------------------------------------------------------------- commands

def cmd_setup(args):
    global LANG
    LANG = args.lang or choose_language()
    no_browser = args.no_browser or headless()
    print("\n" + __doc__.strip().split("\n\n")[0])

    if args.login_only:
        cfg = load_config()
        cfg["refresh_token"] = do_login(cfg["client_id"], cfg["client_secret"],
                                    open_browser=not no_browser)
        cfg.pop("access_token", None)
        save_config(cfg)
        print(t("relogin"))
        return

    step(1, t("s1"))
    acct = gcloud_account()
    if not acct:
        print(t("s1_login"))
        gcloud("auth", "login")
        acct = gcloud_account()
    print(t("s1_acct", a=acct))
    print(t("s1_warn"))
    if not yes(ask(t("s1_ask"))):
        die(t("s1_abort"))

    step(2, t("s2"))
    project = args.project
    if project:
        gcloud("projects", "describe", project)
        print(t("s2_using", p=project))
    else:
        project = "health-" + secrets.token_hex(4)
        print(t("s2_creating", p=project))
        p = gcloud("projects", "create", project, "--name=Google Health CLI", check=False)
        if p.returncode != 0:
            die(t("s2_failed", e=p.stderr.strip(), f=ME))
    gcloud("config", "set", "project", project)

    step(3, t("s3"))
    p = gcloud("services", "enable", SERVICE, f"--project={project}", check=False)
    if p.returncode != 0:
        die(t("s3_failed", e=p.stderr.strip(), p=project))
    print(t("s3_ok", s=SERVICE))

    cid, csec, source = existing_credentials()
    need_console = True
    if cid and csec:
        print(t("found", src=source))
        ok, why = client_check(cid, csec)
        print("  " + why)
        if ok:
            need_console = yes(ask(t("redo")))

    if need_console:
        step(4, t("s4"))
        print(t("s4_intro"))
        for i, ((title, todo), tmpl) in enumerate(zip(TEXT[LANG]["pages"], CONSOLE_URLS), start=1):
            url = tmpl.format(p=project)
            print(f"\n  \033[1m4.{i} {title}\033[0m\n  {url}")
            for line in todo:
                print("     - " + line)
            webbrowser.open(url)
            ask(t("s4_enter"))

        step(5, t("s5"))
        cid, csec = read_credentials(project)
        print(t("s5_verify"))
        ok, why = client_check(cid, csec)
        print("  " + why)
        if not ok and not yes(ask(t("s5_continue"))):
            die(t("s5_abort"))

    step(6, t("s6"))
    refresh = do_login(cid, csec, open_browser=not no_browser)
    save_config({"project": project, "client_id": cid, "client_secret": csec,
                 "refresh_token": refresh, "scopes": SCOPES})
    print(t("s6_saved", p=CONFIG))

    step(7, t("s7"))
    print(json.dumps(api(load_config(), "users/me/identity"), indent=2))
    print("\033[1m" + t("s7_ok", f=ME) + "\033[0m")
    print(t("s7_note"))


def cmd_whoami(args):
    print(json.dumps(api(load_config(), "users/me/identity"), indent=2))


def cmd_fetch(args):
    cfg = load_config()
    end = date.today() + timedelta(days=1)          # exclusive end: includes today
    start = end - timedelta(days=args.days)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "range": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
           "data": {}}

    wanted = args.types or list(ROLLUP_TYPES) + ["sleep"]
    for ty in wanted:
        print(f"  {ty} ...", end="", flush=True)
        try:
            if ty == "sleep":
                points = sleep_points(cfg, start)
            elif ty in ROLLUP_TYPES:
                points = rollup(cfg, ty, start, end, ROLLUP_TYPES[ty])
            else:
                print(" " + t("f_unknown"))
                continue
            out["data"][ty] = points
            print(" " + t("f_count", n=len(points)))
        except RuntimeError as e:
            print(" " + t("f_skip", e=e))
            out["data"][ty] = {"error": str(e)}

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    f = dest / f"health-{date.today().isoformat()}.json"
    f.write_text(json.dumps(out, indent=2))
    print(t("f_written", f=f))


def cmd_selfcheck(args):
    # PKCE: the official test vector, RFC 7636 appendix B.
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", "wrong PKCE S256"
    assert "=" not in b64url(b"\xff\xfe\xfd"), "base64url must be unpadded"

    # CivilDateTime
    c = civil(date(2026, 3, 4))
    assert c == {"date": {"year": 2026, "month": 3, "day": 4},
                 "time": {"hours": 0, "minutes": 0, "seconds": 0, "nanos": 0}}, c

    # chunking: contiguous, never over the cap, covers the whole span
    a, b = date(2026, 1, 1), date(2026, 4, 1)   # 90 days
    ch = chunks(a, b, 14)
    assert ch[0][0] == a and ch[-1][1] == b, ch
    assert all(x[1] == y[0] for x, y in zip(ch, ch[1:])), "ranges are not contiguous"
    assert all((x[1] - x[0]).days <= 14 for x in ch), "range over the cap"
    assert sum((x[1] - x[0]).days for x in ch) == (b - a).days, "days lost or duplicated"
    assert chunks(a, a, 14) == [], "an empty span means zero calls"
    assert len(chunks(a, a + timedelta(days=1), 90)) == 1

    # pasted paths: Finder quotes, drag & drop escaped spaces
    assert clean_path("'/a/b c.json'") == "/a/b c.json"
    assert clean_path('"/a/b.json"  ') == "/a/b.json"
    assert clean_path("/a/b\\ c.json") == "/a/b c.json"
    assert clean_path("/a/b.json") == "/a/b.json"
    assert clean_path("'") == "'", "a lone quote is not a pair"

    # both languages must define exactly the same keys, with the same page count
    assert set(TEXT["en"]) == set(TEXT["it"]), set(TEXT["en"]) ^ set(TEXT["it"])
    assert len(TEXT["en"]["pages"]) == len(TEXT["it"]["pages"]) == len(CONSOLE_URLS)

    # headless detection: a VPS has no DISPLAY, a laptop always has a browser
    assert headless({}, "linux") is True
    assert headless({"DISPLAY": ":0"}, "linux") is False
    assert headless({"WAYLAND_DISPLAY": "wayland-0"}, "linux") is False
    assert headless({}, "darwin") is False
    assert headless({}, "win32") is False

    # token expiry: refresh when less than 60s are left
    now = datetime.now(timezone.utc).timestamp()
    assert not ({"access_token": "x", "expires_at": now + 30}.get("expires_at") > now + 60)

    # rollup caps match Google's documentation
    assert ROLLUP_TYPES["heart-rate"] == 14 and ROLLUP_TYPES["steps"] == 90

    print("selfcheck: ok")


def main():
    global LANG
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", choices=("en", "it"), help="language of the guided setup")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="configure everything from scratch")
    s.add_argument("--project", help="use an existing Cloud project instead of creating one")
    s.add_argument("--no-browser", action="store_true",
                   help="do not try to open a browser (headless server)")
    s.add_argument("--login-only", action="store_true",
                   help="only redo the login (expired refresh token)")
    s.set_defaults(func=cmd_setup)

    f = sub.add_parser("fetch", help="download your data")
    f.add_argument("--days", type=int, default=7, help="days to download (default 7)")
    f.add_argument("--out", default="health-data", help="output directory")
    f.add_argument("--types", nargs="*", help="e.g. steps sleep heart-rate")
    f.set_defaults(func=cmd_fetch)

    sub.add_parser("whoami", help="check the token").set_defaults(func=cmd_whoami)
    sub.add_parser("selfcheck", help="offline test").set_defaults(func=cmd_selfcheck)

    args = ap.parse_args()
    if args.lang:
        LANG = args.lang
    args.func(args)


if __name__ == "__main__":
    main()
