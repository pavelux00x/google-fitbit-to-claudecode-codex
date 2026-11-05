# google-health-cli

Scarica i tuoi dati **Fitbit / Google Health** sul tuo computer, in JSON, con un comando.

La vecchia Fitbit Web API è stata spenta il **30 ottobre 2026**: tutti gli script e gli MCP
server costruiti su `api.fitbit.com` hanno smesso di funzionare. L'unica strada rimasta è la
[Google Health API](https://developers.google.com/health).

Questo script automatizza tutto ciò che **si può** automatizzare e ti guida nei passaggi che
Google obbliga a fare a mano nella console.

- **Zero dipendenze Python** — solo la libreria standard (3.9+)
- [`gcloud`](https://cloud.google.com/sdk/docs/install) serve solo per il setup iniziale
- Le credenziali restano sul tuo computer, in `~/.config/google-health/config.json` (permessi `600`)
- La guida parla **italiano o inglese** — te lo chiede al primo prompt

## Uso

```bash
git clone https://github.com/pavelux00x/google-fitbit-to-claudecode-codex.git
cd google-fitbit-to-claudecode-codex
python3 health.py setup     # una volta sola: una decina di minuti
python3 health.py fetch     # ogni volta che vuoi dati aggiornati
```

I dati finiscono in `health-data/health-AAAA-MM-GG.json`.

```bash
python3 health.py fetch --days 30                # ultimi 30 giorni
python3 health.py fetch --types steps sleep      # solo alcuni tipi
python3 health.py whoami                         # verifica che il token funzioni
python3 health.py selfcheck                      # test offline, senza rete
python3 health.py --lang it setup                # salta la domanda sulla lingua
```

## Cosa fa `setup`

| Passaggio | Chi lo fa |
|---|---|
| Login Google | `gcloud auth login` |
| Crea il progetto Cloud | automatico |
| Abilita `health.googleapis.com` | automatico |
| Google Auth Platform: wizard iniziale (progetti nuovi) | **tu**, pagina aperta dallo script |
| Audience: Testing + test user | **tu**, pagina aperta dallo script |
| Data access: selezione degli scope | **tu**, pagina aperta dallo script |
| Crea il client OAuth + redirect URI | **tu**, pagina aperta dallo script |
| Verifica che il client OAuth esista davvero | automatico, **senza aprire il browser** |
| Login OAuth, consenso, refresh token | automatico (loopback + PKCE) |
| Verifica con `users.getIdentity` | automatico |

**Perché quattro passaggi a mano?** Google non espone nessuna API per creare un client
OAuth. La IAP OAuth Admin API, l'ultima che lo permetteva, è stata chiusa il 19 marzo 2026.
Nessuno script al mondo può fare quei click al posto tuo: chi promette il contrario usa
Terraform, che per autenticarsi ha comunque bisogno di un client OAuth già esistente.

Se rilanci `setup` quando il client OAuth esiste già — credenziali salvate, oppure un
`client_secret_*.json` in `~/Downloads` — lo script lo verifica contro l'endpoint token di
Google e ti propone di saltare i passaggi in console invece di riaprirti quattro pagine. La
verifica non costa nulla: chiede un refresh con un token volutamente falso e guarda se
Google risponde `invalid_grant` (client buono) oppure `invalid_client` (client inesistente o
secret sbagliato).

## Su un server

### Login su una VPS senza browser

Il consenso OAuth avviene nel browser, e il browser deve tornare su
`http://localhost:8765/callback`. Su una VPS quel link è inutile se lo apri dal portatile:
il *tuo* localhost non è quello del server, quindi il consenso non raggiunge mai lo script.

Un tunnel SSH fa coincidere i due. Dal tuo computer:

```bash
ssh -L 8765:localhost:8765 utente@tua-vps
```

Poi, dentro quella sessione:

```bash
python3 health.py setup
```

Lo script si accorge di non avere un browser, stampa il link invece di tentare di aprirne
uno, e aspetta. Apri quel link sul tuo portatile: il browser contatta il *tuo*
`localhost:8765`, SSH porta la connessione fino alla VPS, e lo script riceve il consenso.
Non cambia nient'altro — il redirect URI resta `http://localhost:8765/callback`, quindi il
client OAuth non va riconfigurato.

Attenzione alla direzione: serve `-L` (locale), non `-R` (remoto).

Per forzare quel comportamento ovunque c'è `--no-browser`.

### Oppure copia la configurazione

Se preferisci non usare il tunnel, fai il `setup` sul portatile e copia il risultato:

```bash
scp ~/.config/google-health/config.json utente@server:~/.config/google-health/config.json
ssh utente@server chmod 600 ~/.config/google-health/config.json
```

### Poi tienilo acceso

**Prima di tutto, togli la scadenza a 7 giorni**, altrimenti il server si ferma dopo una
settimana: console → Branding (compila i campi obbligatori) → Audience → *Pubblica app*.
Resta un'app non verificata, con il tetto di 100 utenti: irrilevante se sei solo tu.

Poi un timer systemd ogni 15 minuti:

```ini
# ~/.config/systemd/user/health.service
[Service]
Type=oneshot
WorkingDirectory=%h/google-fitbit-to-claudecode-codex
ExecStart=/usr/bin/python3 health.py fetch --days 2
```

```ini
# ~/.config/systemd/user/health.timer
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now health.timer
```

In cron è una riga sola:

```
*/15 * * * * cd ~/google-fitbit-to-claudecode-codex && /usr/bin/python3 health.py fetch --days 2
```

### Perché 15 minuti e non 5

Non è un problema di quota: il limite è 300 richieste al minuto per utente e questo script
ne fa 7 per giro. Il punto è che **a monte i dati non esistono più freschi di così**. Il
Fitbit sincronizza col telefono via Bluetooth circa ogni 15 minuti, e solo se l'app Google
Health è aperta e il telefono è nel raggio. Interrogare l'API ogni 5 minuti restituisce tre
volte di fila lo stesso identico contenuto.

### Se ti serve davvero il tempo reale: webhook

L'API sa notificarti quando i dati cambiano, invece di farteli cercare. Registri un
*subscriber* con un endpoint HTTPS pubblico e Google ci manda un POST a ogni aggiornamento.
Il costo onesto di questa strada:

- serve un endpoint HTTPS pubblico, TLS 1.2+, raggiungibile da Google
- alla registrazione Google manda due POST di verifica con body `{"type": "verification"}`:
  quello **con** header `Authorization` deve rispondere 200/201, quello **senza** 401/403.
  Se ne sbagli uno, la create fallisce con `FAILED_PRECONDITION`
- ogni notifica è firmata ECDSA P-256, firma nell'header `GOOGLE-HEALTH-API-SIGNATURE`, da
  verificare sul corpo grezzo. Le chiavi ruotano ogni 30 giorni, quindi il keyset va
  scaricato, non scritto nel codice
- **la notifica non contiene i dati**: indica solo utente, tipo di dato e intervallo. I
  valori li prendi comunque tu con `dataPoints:reconcile`
- devi rispondere `204` subito e lavorare in asincrono. Se rispondi altro, Google ritenta
  con backoff e tiene le notifiche per 7 giorni
- gestire i subscriber richiede lo scope `cloud-platform`, quindi un service account e non
  il token utente che usa questo script

## Dashboard

```bash
python3 health.py fetch --days 30
python3 dashboard.py --analyze --lang it
open dashboard.html
```

`dashboard.py` costruisce un unico `dashboard.html` autosufficiente: passi giornalieri,
notti impilate per fase del sonno (profondo / REM / leggero / sveglio), qualche KPI, e il
cinturino Fitbit blu in tessuto in 3D che puoi girare col cursore — trama intrecciata,
passante di metallo, e gli ultimi dodici giorni disegnati sul suo schermino. I dati sono
incorporati nel file, quindi si apre offline senza server — e per lo stesso motivo
`dashboard.html` è gitignorato: contiene i tuoi dati sanitari.

`--analyze` manda un riassunto testuale compatto (mai il JSON grezzo) a
[OpenRouter](https://openrouter.ai) e ne incorpora la risposta. Il modello predefinito è
`openai/gpt-4o-mini`, cambiabile con `--model`. Attenzione: diversi modelli economici sono
reasoning model e con un budget di token piccolo lo consumano tutto a ragionare,
restituendo contenuto vuoto — lo script te lo dice invece di scrivere un pannello bianco.
La risposta viene messa in cache in `health-data/analysis.json` e riusata finché non passi
di nuovo `--analyze`.

La chiave API viene letta da un file su disco, non passa mai dalla riga di comando e non
viene mai stampata. Modifica `KEY_FILE` in cima a `dashboard.py` per puntare alla tua.

Considera l'analisi una lettura dei numeri, non un parere medico.

## Limiti da sapere prima di iniziare

- **Usa lo stesso account Google del tuo Fitbit.** Gli account Google Workspace non sono supportati da Fitbit.
- **In modalità Testing il refresh token scade dopo 7 giorni.** Lo script te lo dice quando
  succede: `python3 health.py setup --login-only`. Per renderlo permanente pubblica l'app
  dalla pagina Audience (resta non verificata, tetto di 100 utenti — irrilevante per uso personale).
- **Non esiste un ambiente sandbox**: il progetto che crei lavora sui tuoi dati reali. Tutti gli scope richiesti sono `.readonly`.
- Alcuni tipi hanno un massimo di 14 giorni per singola chiamata (`heart-rate`,
  `active-minutes`, `total-calories`), 90 per gli altri. Lo script spezza le richieste da solo.

## Tipi di dato scaricati

Via `dailyRollUp` (un valore al giorno): `steps`, `distance`, `weight`, `active-minutes`,
`heart-rate`, `total-calories`. Via `list` (un record per sessione): `sleep`.

L'elenco completo dei tipi è nel
[discovery document](https://health.googleapis.com/$discovery/rest?version=v4).

## Se qualcosa va storto

| Errore | Causa quasi certa |
|---|---|
| `403` su una chiamata | uno scope non selezionato in *Data access*, oppure il tuo indirizzo non è tra i *Test users* |
| `invalid_grant` | refresh token scaduto (i 7 giorni) → `setup --login-only` |
| `redirect_uri_mismatch` | nel client OAuth manca esattamente `http://localhost:8765/callback` |
| `enable` fallisce parlando di billing | collega un account di fatturazione al progetto e rilancia con `--project NOME` |
| creazione del progetto fallita | quota progetti esaurita: creane uno a mano e usa `--project NOME` |

## Licenza

MIT. Non affiliato a Google né a Fitbit.
