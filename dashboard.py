#!/usr/bin/env python3
"""Build a local dashboard from the data downloaded by health.py.

    python3 dashboard.py                      # chart only, reuses any cached analysis
    python3 dashboard.py --analyze            # also ask OpenRouter to read your sleep
    python3 dashboard.py --analyze --lang it  # analysis written in Italian

The dashboard is a single self-contained HTML file with your data inlined, so it
works offline by just opening it. It therefore contains personal health data:
it is git-ignored on purpose.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "health-data"
OUT = HERE / "dashboard.html"
CACHE = DATA_DIR / "analysis.json"
KEY_FILE = Path("/Users/pavelfilingeri/Documents/sadkjnsacizsagc/key")
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"

NIGHT_MIN = 180        # a session shorter than this counts as a nap, not a night


def die(msg):
    print(f"\n[error] {msg}", file=sys.stderr)
    sys.exit(1)


def latest_data_file():
    files = sorted(DATA_DIR.glob("health-*.json"))
    if not files:
        die(f"no data in {DATA_DIR}. Run first:  python3 health.py fetch --days 30")
    return files[-1]


def rollup_day(rec):
    c = rec["civilStartTime"]["date"]
    return date(c["year"], c["month"], c["day"])


def local_end_date(interval):
    """The calendar day a sleep session ended, in the wearer's own timezone."""
    end = datetime.fromisoformat(interval["endTime"].replace("Z", "+00:00"))
    offset = int((interval.get("endUtcOffset") or "0s").rstrip("s"))
    return (end + timedelta(seconds=offset)).date()


def build(raw):
    """Turn the API payload into the compact series the page draws."""
    d = raw["data"]
    steps = {rollup_day(r): int(r["steps"]["countSum"]) for r in d.get("steps", [])}

    nights = {}
    naps = 0
    for rec in d.get("sleep", []):
        s = rec["sleep"]
        summary = s.get("summary") or {}
        asleep = int(summary.get("minutesAsleep") or 0)
        if asleep < NIGHT_MIN:
            naps += 1
            continue
        day = local_end_date(s["interval"])
        stages = {x["type"]: int(x["minutes"]) for x in (summary.get("stagesSummary") or [])}
        # keep the longest session of the night if the band split it
        if day not in nights or asleep > nights[day]["asleep"]:
            nights[day] = {
                "asleep": asleep,
                "awake": int(summary.get("minutesAwake") or 0),
                "toFallAsleep": int(summary.get("minutesToFallAsleep") or 0),
                "deep": stages.get("DEEP", 0),
                "rem": stages.get("REM", 0),
                "light": stages.get("LIGHT", 0) + stages.get("ASLEEP", 0),
                "device": (s.get("metadata") or {}).get("deviceName")
                          or (rec.get("dataSource", {}).get("device") or {}).get("displayName", ""),
            }

    days = sorted(set(steps) | set(nights))
    series = [{
        "date": day.isoformat(),
        "steps": steps.get(day),
        "sleep": nights.get(day),
    } for day in days]
    return {"series": series, "naps": naps,
            "range": raw.get("range", {}), "generated_at": raw.get("generated_at")}


def averages(series):
    st = [x["steps"] for x in series if x["steps"] is not None]
    sl = [x["sleep"] for x in series if x["sleep"]]
    hours = [x["asleep"] / 60 for x in sl]
    out = {
        "days": len(series),
        "steps_avg": sum(st) / len(st) if st else 0,
        "steps_max": max(st) if st else 0,
        "nights": len(sl),
        "sleep_avg": sum(hours) / len(hours) if hours else 0,
        "sleep_min": min(hours) if hours else 0,
        "sleep_max": max(hours) if hours else 0,
    }
    tot = sum(x["asleep"] for x in sl) or 1
    out["deep_pct"] = 100 * sum(x["deep"] for x in sl) / tot
    out["rem_pct"] = 100 * sum(x["rem"] for x in sl) / tot
    return out


# ---------------------------------------------------------------- analysis

def sleep_digest(series, avg, naps, lang):
    """A compact, human-readable brief. We send this, never the raw JSON."""
    lines = []
    for x in series:
        s = x["sleep"]
        if not s:
            lines.append(f"{x['date']}: no sleep recorded"
                         + (f", {x['steps']} steps" if x["steps"] is not None else ""))
            continue
        lines.append(
            f"{x['date']}: {s['asleep']/60:.1f}h asleep "
            f"(deep {s['deep']}min, rem {s['rem']}min, light {s['light']}min, "
            f"awake {s['awake']}min, {s['toFallAsleep']}min to fall asleep)"
            + (f", {x['steps']} steps" if x["steps"] is not None else ""))
    head = (f"{avg['nights']} nights recorded out of {avg['days']} days, plus {naps} naps. "
            f"Average {avg['sleep_avg']:.1f}h (min {avg['sleep_min']:.1f}, max {avg['sleep_max']:.1f}). "
            f"Deep {avg['deep_pct']:.0f}% of sleep, REM {avg['rem_pct']:.0f}%. "
            f"Average {avg['steps_avg']:.0f} steps/day.")
    return head + "\n\n" + "\n".join(lines)


def read_key():
    if not KEY_FILE.is_file():
        die(f"OpenRouter key not found at {KEY_FILE}")
    key = KEY_FILE.read_text().strip()
    if not key:
        die(f"{KEY_FILE} is empty")
    return key


def analyze(series, avg, naps, model, lang):
    """Ask OpenRouter to read the sleep data. Returns the text."""
    language = {"it": "italiano", "en": "English"}[lang]
    prompt = (
        f"Sei un analista di dati del sonno. Scrivi in {language}.\n"
        "Qui sotto ci sono i dati reali di un utente Fitbit. Analizza SOLO quello che vedi.\n\n"
        "Regole:\n"
        "- massimo 250 parole, niente elenchi puntati lunghi\n"
        "- cita numeri concreti presi dai dati\n"
        "- indica se vedi una tendenza, un giorno anomalo, o una relazione fra passi e sonno\n"
        "- distingui chiaramente i dati mancanti (notti non registrate) da notti di poco sonno\n"
        "- non dare diagnosi mediche e non prescrivere terapie; se qualcosa merita attenzione, "
        "suggerisci di parlarne con un medico\n\n"
        "DATI:\n" + sleep_digest(series, avg, naps, lang))

    body = json.dumps({
        "model": model,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(OPENROUTER, data=body, headers={
        "Authorization": "Bearer " + read_key(),
        "Content-Type": "application/json",
        "X-Title": "google-health-cli dashboard",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        die(f"OpenRouter refused the request (HTTP {e.code}): "
            f"{e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        die(f"cannot reach OpenRouter: {e}")

    if "error" in out:
        die(f"OpenRouter: {str(out['error'])[:300]}")
    text = (out["choices"][0]["message"].get("content") or "").strip()
    if not text:
        die(f"model {model} returned empty content. Some reasoning models spend the whole "
            f"budget thinking: try --model {DEFAULT_MODEL}")
    usage = out.get("usage") or {}
    return {"text": text, "model": model, "lang": lang,
            "at": datetime.now().isoformat(timespec="seconds"),
            "tokens": f"{usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')}"}


# ---------------------------------------------------------------- render

def render(payload, analysis):
    tpl = (HERE / "dashboard_template.html").read_text()
    return (tpl
            .replace("__DATA__", json.dumps(payload))
            .replace("__ANALYSIS__", json.dumps(analysis)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyze", action="store_true", help="call OpenRouter for a sleep reading")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenRouter model (default {DEFAULT_MODEL})")
    ap.add_argument("--lang", choices=("en", "it"), default="en", help="language of the analysis")
    ap.add_argument("--input", help="a specific health-*.json instead of the newest")
    args = ap.parse_args()

    src = Path(args.input) if args.input else latest_data_file()
    raw = json.loads(src.read_text())
    payload = build(raw)
    payload["avg"] = averages(payload["series"])
    payload["source"] = src.name

    analysis = None
    if args.analyze:
        print(f"Asking {args.model} to read your sleep...")
        analysis = analyze(payload["series"], payload["avg"], payload["naps"],
                           args.model, args.lang)
        CACHE.write_text(json.dumps(analysis, indent=2))
        print(f"  done ({analysis['tokens']} tokens)")
    elif CACHE.exists():
        analysis = json.loads(CACHE.read_text())
        print(f"Reusing the cached analysis from {analysis['at']}")

    OUT.write_text(render(payload, analysis))
    print(f"\nWrote {OUT}")
    print(f"  {payload['avg']['days']} days, {payload['avg']['nights']} nights, "
          f"{payload['naps']} naps, from {src.name}")


if __name__ == "__main__":
    main()
