#!/usr/bin/env python3
"""Interaktiver Mail-Recherche-Agent für juristische Aktenrecherche.

Orchestriert qmd-Suchen, bewertet Treffer per LLM-Hypothese, baut
inkrementell eine Akte auf. Sessions werden persistent gespeichert.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import qmd

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:32b")
DEFAULT_OUT = Path.home() / "marmalade-fall" / "output"
SESSIONS_DIR = Path(
    os.environ.get("CHATBOT_SESSIONS_DIR")
    or (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "mini-chatbot" / "sessions")
)

SYSTEM_PROMPT = """Du bist ein juristischer Recherche-Assistent. Du hilfst
Joscha, aus seinen privaten E-Mails einen zeitlichen Ablauf zu rekonstruieren.

Wichtig:
- Joscha kennt den Ablauf NICHT mehr. Stelle keine Bestätigungsfragen
  ("Stimmt es, dass am X passiert ist?") - er weiß es nicht. Bilde
  Hypothesen aus den Mailinhalten und stelle sie zur Diskussion.
- Datum, Absender, Empfänger immer aus dem YAML-Frontmatter, nie aus
  dem Mail-Text raten.
- Wenn ein Feld unklar ist: leerlassen oder "?". Lieber nachfragen als
  raten.
- Antworte präzise auf Deutsch, sachlich, juristisch nüchtern.
- Wenn JSON gefordert ist: NUR valides JSON, keine Einleitung."""


# ───────────── Ollama ─────────────

def _post_chat(model: str, messages: list[dict], *, stream: bool,
               json_mode: bool, num_ctx: int):
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "options": {"num_ctx": num_ctx},
    }
    if json_mode:
        payload["format"] = "json"
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req)


def chat(model: str, messages: list[dict], *, json_mode: bool = False,
         num_ctx: int = 32768) -> str:
    with _post_chat(model, messages, stream=False, json_mode=json_mode,
                    num_ctx=num_ctx) as resp:
        obj = json.loads(resp.read())
    return obj.get("message", {}).get("content", "")


def chat_stream(model: str, messages: list[dict], *, num_ctx: int = 32768) -> str:
    out = []
    with _post_chat(model, messages, stream=True, json_mode=False,
                    num_ctx=num_ctx) as resp:
        for line in resp:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunk = obj.get("message", {}).get("content", "")
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                out.append(chunk)
            if obj.get("done"):
                break
    print()
    return "".join(out)


# ───────────── Session ─────────────

def session_path(name: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", name)
    return SESSIONS_DIR / f"recherche_{safe}.json"


def load_session(name: str) -> dict:
    p = session_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {
        "name": name,
        "case_description": "",
        "executed_searches": [],
        "candidates": {},
        "last_suggestions": [],
    }


def list_sessions() -> list[dict]:
    """Liefert Liste aller recherche_*-Sessions im SESSIONS_DIR."""
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for p in SESSIONS_DIR.glob("recherche_*.json"):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        counts = {"accepted": 0, "pending": 0, "rejected": 0}
        for c in data.get("candidates", {}).values():
            counts[c.get("status", "pending")] = counts.get(
                c.get("status", "pending"), 0) + 1
        out.append({
            "name": data.get("name", p.stem.removeprefix("recherche_")),
            "path": p,
            "mtime": p.stat().st_mtime,
            "case": data.get("case_description", ""),
            "counts": counts,
        })
    return sorted(out, key=lambda s: s["mtime"], reverse=True)


def print_sessions() -> None:
    sessions = list_sessions()
    if not sessions:
        print(f"Keine Sessions in {SESSIONS_DIR}.")
        return
    print(f"\n{len(sessions)} Session(s) in {SESSIONS_DIR}:\n")
    for s in sessions:
        ts = datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
        c = s["counts"]
        print(f"  {s['name']}")
        print(f"    zuletzt: {ts}  |  ✓{c['accepted']} ?{c['pending']} ✗{c['rejected']}")
        if s["case"]:
            print(f"    Fall:    {s['case'][:80]}")
        print()


def save_session(state: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_path(state["name"]).write_text(
        json.dumps(state, ensure_ascii=False, indent=2)
    )


# ───────────── Candidates ─────────────

def make_candidate(uri: str) -> dict:
    text = qmd.fetch(uri, full=True)
    meta, body = qmd.parse_frontmatter(text)
    parts = meta.get("participants", [])
    if isinstance(parts, str):
        parts = [parts]
    return {
        "uri": uri,
        "status": "pending",
        "subject": meta.get("subject", ""),
        "participants": parts,
        "date_start": qmd.normalize_date(meta.get("date_start", "")),
        "date_last": qmd.normalize_date(meta.get("date_last", "")),
        "message_count": meta.get("message_count", "1"),
        "body_excerpt": body[:2500],
        "summary": "",
        "rejection_reason": "",
    }


def status_counts(state: dict) -> dict:
    counts = {"accepted": 0, "pending": 0, "rejected": 0}
    for c in state["candidates"].values():
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return counts


# ───────────── Commands ─────────────

def cmd_search(state: dict, query: str, top_k: int) -> None:
    if not query:
        print("Suchbegriff fehlt.")
        return
    print(f"→ qmd-Suche: {query!r}")
    try:
        uris = qmd.search(query, top_k, all_results=True, min_score=0.2)
    except subprocess.CalledProcessError as e:
        print(f"qmd-Fehler: {e.stderr}")
        return
    new = 0
    for u in uris:
        if u not in state["candidates"]:
            state["candidates"][u] = make_candidate(u)
            new += 1
    state["executed_searches"].append(query)
    print(f"   {len(uris)} Treffer ({new} neu, {len(uris) - new} bekannt).")


def cmd_add(state: dict, arg: str) -> None:
    """URIs manuell hinzufügen. Format:
        /add qmd://threads/a.md qmd://threads/b.md
        /add @/pfad/zu/datei.txt    (jede Zeile darf eine URI enthalten)
    """
    if not arg:
        print("Usage: /add <uri> [<uri> ...]   oder   /add @datei")
        return
    raw_lines: list[str] = []
    for tok in arg.split():
        if tok.startswith("@"):
            path = Path(tok[1:]).expanduser()
            if not path.exists():
                print(f"Datei nicht gefunden: {path}")
                continue
            raw_lines.extend(path.read_text().splitlines())
        else:
            raw_lines.append(tok)
    uris, seen = [], set()
    for line in raw_lines:
        m = qmd.URI_RE.search(line)
        if m and m.group(0) not in seen:
            seen.add(m.group(0))
            uris.append(m.group(0))
    if not uris:
        print("Keine qmd://…-URIs erkannt.")
        return
    new = 0
    for u in uris:
        if u not in state["candidates"]:
            try:
                state["candidates"][u] = make_candidate(u)
                new += 1
            except subprocess.CalledProcessError as e:
                print(f"qmd get fehlgeschlagen für {u}: {e.stderr}")
    print(f"   {len(uris)} URIs verarbeitet ({new} neu, "
          f"{len(uris) - new} bereits bekannt).")


def cmd_suggest(state: dict, model: str) -> None:
    accepted = [c for c in state["candidates"].values() if c["status"] == "accepted"]
    timeline = "\n".join(
        f"- {c['date_start']}: {c['subject']} — {c.get('summary', '')}"
        for c in sorted(accepted, key=lambda x: x["date_start"])
    ) or "(noch keine bestätigten Mails)"
    searches = "\n".join(f"- {s}" for s in state["executed_searches"]) or "(noch keine)"
    prompt = f"""Fall: {state['case_description']}

Bestätigte Mails (chronologisch):
{timeline}

Bereits ausgeführte Suchen:
{searches}

Schlage 3-5 weitere qmd-Suchanfragen vor, um den Ablauf zu vervollständigen.

WICHTIG zum Format der Suchanfragen:
- NUR reiner Stichworttext (2-5 Wörter, deutsche Begriffe oder Eigennamen).
- KEINE Boolean-Operatoren (AND/OR), KEINE Anführungszeichen, KEINE
  Datumsbereiche, KEINE Klammern, KEIN Präfix wie "qmd query:".
- qmd kann nicht nach Datum filtern — gib also keine Datumsangaben mit.
- Beispiel-richtig: "Wolf Warnken Kündigung", "Coaching Eric Fischer".
- Beispiel-falsch: "'2024-06-20..2024-10-08' AND (X OR Y)".

Antworte als JSON: {{"reasoning": "...", "suggestions": ["...", ...]}}"""
    raw = chat(model, [{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": prompt}], json_mode=True)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("Konnte Vorschlag nicht parsen, Rohausgabe:\n", raw)
        return
    print(f"\n{data.get('reasoning', '')}\n")
    sugg = data.get("suggestions", [])
    for i, q in enumerate(sugg, 1):
        print(f"  [{i}] {q}")
    print("\nMit  :1  :2  …  direkt ausführen, oder /search <text>.")
    state["last_suggestions"] = sugg


def cmd_reject(state: dict, pattern: str) -> None:
    """Bulk-Reject aller pending Mails, deren Betreff oder Teilnehmer
    den Pattern (case-insensitive Substring) enthalten."""
    if not pattern:
        print("Usage: /reject <pattern>   (matcht Betreff und Teilnehmer)")
        return
    pat = pattern.lower()
    matches = []
    for c in state["candidates"].values():
        if c["status"] != "pending":
            continue
        haystack = (c["subject"] + " " + " ".join(c["participants"])).lower()
        if pat in haystack:
            matches.append(c)
    if not matches:
        print(f"Keine pending Mail matcht {pattern!r}.")
        return
    print(f"\n{len(matches)} pending Mail(s) matchen {pattern!r}:")
    for c in sorted(matches, key=lambda x: x["date_start"]):
        parts = ", ".join(c["participants"][:2])
        print(f"  {c['date_start'] or '???':11s}  {c['subject'][:55]:<55s}  {parts}")
    try:
        ans = input(f"\nAlle {len(matches)} aussortieren? [j/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans not in {"j", "ja", "y", "yes"}:
        print("Abgebrochen.")
        return
    for c in matches:
        c["status"] = "rejected"
        c["rejection_reason"] = f"bulk-reject: {pattern}"
    print(f"✗ {len(matches)} Mail(s) auf 'rejected' gesetzt.")


def cmd_review(state: dict, model: str, use_llm: bool = True) -> None:
    pending = [u for u, c in state["candidates"].items() if c["status"] == "pending"]
    if not pending:
        print("Keine Mails zum Review.")
        return
    print(f"\n{len(pending)} pending. (Strg-C bricht ab, Status bleibt erhalten.)")
    for uri in pending:
        c = state["candidates"][uri]
        print("\n" + "─" * 70)
        print(f"📧 {c['subject']}")
        date = c["date_start"] + (f" – {c['date_last']}"
                                  if c["date_last"] and c["date_last"] != c["date_start"]
                                  else "")
        print(f"   {date}  |  {c['message_count']} Nachricht(en)")
        print(f"   {', '.join(c['participants'][:3])}")
        print(f"   {uri}")

        llm = {"likely_relevant": None, "reasoning": "", "summary": ""}
        if use_llm:
            prompt = f"""Mail-Frontmatter:
- Betreff: {c['subject']}
- Datum: {c['date_start']}
- Teilnehmer: {', '.join(c['participants'])}

Auszug:
{c['body_excerpt']}

Fall: {state['case_description']}

Bewerte Relevanz und formuliere – falls relevant – eine juristisch
nüchterne Kernaussage (1 Satz, deutsch).
JSON: {{"likely_relevant": true|false, "reasoning": "...", "summary": "..."}}"""
            try:
                llm = json.loads(chat(model, [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ], json_mode=True))
            except KeyboardInterrupt:
                save_session(state)
                print("\nReview abgebrochen (während LLM-Anfrage). "
                      "Session gespeichert, Thread bleibt pending.")
                return
            except (json.JSONDecodeError, urllib.error.URLError) as e:
                print(f"   (LLM-Fehler: {e})")

            verdict = ("wahrscheinlich RELEVANT" if llm.get("likely_relevant")
                       else "wahrscheinlich nicht relevant")
            print(f"\n💡 {verdict}")
            if llm.get("reasoning"):
                print(f"   {llm['reasoning']}")
            if llm.get("summary"):
                print(f"   Kernaussage: {llm['summary']}")
        else:
            print(f"\n   {c['body_excerpt'][:600]}…")

        while True:
            try:
                ans = input("\n[a]kzeptieren / [r]aus / [s]kip / [b]ody / [q]uit: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                save_session(state)
                print("\nReview abgebrochen, Session gespeichert.")
                return
            if ans == "b":
                print("\n" + qmd.fetch(uri, full=True)[:6000])
                continue
            if ans == "q":
                save_session(state)
                return
            if ans == "s":
                break
            if ans == "a":
                default = llm.get("summary", "")
                summary = input(f"Kernaussage [{default}]: ").strip() or default
                c["summary"] = summary
                c["status"] = "accepted"
                break
            if ans == "r":
                c["rejection_reason"] = input("Grund (optional): ").strip()
                c["status"] = "rejected"
                break
            print("Optionen: a r s b q")
        save_session(state)
    print("\nReview abgeschlossen.")


def cmd_list(state: dict) -> None:
    by = {"accepted": [], "pending": [], "rejected": []}
    for c in state["candidates"].values():
        by[c["status"]].append(c)
    for status, label in [("accepted", "✓ Akzeptiert"),
                          ("pending", "? Pending"),
                          ("rejected", "✗ Aussortiert")]:
        rows = sorted(by[status], key=lambda c: c["date_start"])
        print(f"\n{label} ({len(rows)}):")
        for c in rows:
            extra = f" — {c['summary']}" if status == "accepted" and c["summary"] else ""
            print(f"  {c['date_start'] or '???':11s}  {c['subject'][:60]}{extra}")


def cmd_gaps(state: dict, model: str) -> None:
    accepted = sorted(
        [c for c in state["candidates"].values() if c["status"] == "accepted"],
        key=lambda c: c["date_start"],
    )
    if len(accepted) < 2:
        print("Zu wenige bestätigte Mails für Lückenanalyse.")
        return
    timeline = "\n".join(f"- {c['date_start']}: {c['subject']} — {c['summary']}"
                        for c in accepted)
    prompt = f"""Bestätigte Mails:
{timeline}

Identifiziere zeitliche Lücken (>4 Wochen ohne Mails) oder thematische
Brüche, die auffällig sind. Schlage pro Lücke eine konkrete qmd-Suchanfrage
vor.

WICHTIG zum Feld "search":
- NUR reiner Stichworttext (2-5 deutsche Wörter / Eigennamen).
- KEINE Boolean-Operatoren (AND/OR), KEINE Anführungszeichen, KEINE
  Datumsbereiche, KEINE Klammern, KEIN Präfix wie "qmd query:".
- qmd kann nicht nach Datum filtern. Datum gehört ins "period"-Feld,
  NICHT in "search".
- Beispiel-richtig: "Lohnzahlung Verwaltungspflichten Geschäftsführer".
- Beispiel-falsch: "'2024-06-20..2024-10-08' AND (Lohn OR Abmahnung)".

JSON: {{"gaps": [{{"period": "...", "concern": "...", "search": "..."}}]}}"""
    try:
        data = json.loads(chat(model, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], json_mode=True))
    except json.JSONDecodeError as e:
        print(f"Parse-Fehler: {e}")
        return
    for g in data.get("gaps", []):
        print(f"\n• Zeitraum: {g.get('period', '?')}")
        print(f"  Bedenken: {g.get('concern', '')}")
        print(f"  Suche:    {g.get('search', '')}")


def cmd_export(state: dict, out_dir: str) -> None:
    out = Path(out_dir).expanduser()
    accepted = sorted(
        [c for c in state["candidates"].values() if c["status"] == "accepted"],
        key=lambda c: c["date_start"],
    )
    if not accepted:
        print("Keine akzeptierten Mails — nichts zu exportieren.")
        return
    (out / "mails").mkdir(parents=True, exist_ok=True)

    def esc(s: str) -> str:
        return str(s).replace("|", "\\|").replace("\n", " ")

    md = ["# Zeitlicher Ablauf", "",
          f"_Stand: {datetime.now().strftime('%Y-%m-%d')}, "
          f"{len(accepted)} Mails_", "",
          "| Datum | Betreff | Teilnehmer | Kernaussage | Quelle |",
          "|-------|---------|------------|-------------|--------|"]
    for c in accepted:
        parts = "; ".join(c["participants"][:3])
        md.append(f"| {c['date_start']} | {esc(c['subject'])} | "
                  f"{esc(parts)} | {esc(c['summary'])} | "
                  f"{Path(c['uri']).name} |")
    (out / "zeitlicher_ablauf.md").write_text("\n".join(md) + "\n")

    with (out / "zeitlicher_ablauf.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Datum", "Betreff", "Teilnehmer", "Kernaussage", "Quelle"])
        for c in accepted:
            w.writerow([c["date_start"], c["subject"],
                        "; ".join(c["participants"]), c["summary"],
                        Path(c["uri"]).name])

    for c in accepted:
        body = qmd.fetch(c["uri"], full=True)
        (out / "mails" / Path(c["uri"]).name).write_text(body)

    print(f"\n✓ {out / 'zeitlicher_ablauf.md'}")
    print(f"✓ {out / 'zeitlicher_ablauf.csv'}")
    print(f"✓ {out / 'mails'}/  ({len(accepted)} Dateien)")


def cmd_summary(state: dict, out_dir: str, model: str) -> None:
    accepted = sorted(
        [c for c in state["candidates"].values() if c["status"] == "accepted"],
        key=lambda c: c["date_start"],
    )
    if not accepted:
        print("Keine akzeptierten Mails.")
        return
    timeline = "\n".join(
        f"- {c['date_start']} — {c['subject']} "
        f"({', '.join(c['participants'][:2])}): {c['summary']}"
        for c in accepted
    )
    prompt = f"""Fall: {state['case_description']}

Zeitliche Abfolge:
{timeline}

Schreibe eine narrative Zusammenfassung (deutsch, juristisch nüchtern).
Strukturiere nach erkennbaren Phasen. Markiere offene Lücken explizit.
Keine Erfindungen — nur was aus den Mails ersichtlich ist."""
    print("→ Generiere Zusammenfassung...\n")
    text = chat_stream(model, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ])
    out_path = Path(out_dir).expanduser() / "zusammenfassung.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(f"# Zusammenfassung\n\n{text}\n")
    print(f"\n✓ {out_path}")


def cmd_freeform(state: dict, model: str, question: str, num_ctx: int) -> None:
    accepted = sorted(
        [c for c in state["candidates"].values() if c["status"] == "accepted"],
        key=lambda c: c["date_start"],
    )
    ctx = "\n".join(f"- {c['date_start']}: {c['subject']} — {c['summary']}"
                    for c in accepted) or "(nichts)"
    chat_stream(model, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"Fall: {state['case_description']}\n\n"
            f"Bestätigte Mails:\n{ctx}\n\nFrage: {question}"},
    ], num_ctx=num_ctx)


SEARCH_INTENT_RE = re.compile(
    r"^\s*(?:suche?|finde?|search|find)(?:\s+(?:nach|mal))?\s+(.+?)\s*[.!?]?$",
    re.IGNORECASE,
)


HELP = """
Befehle:
  /search <query>           qmd-Suche, fügt Treffer als 'pending' hinzu
  /add <uri> [<uri>...]     URIs direkt aufnehmen (oder /add @datei.txt)
  /suggest                  LLM schlägt 3-5 Suchanfragen vor
  :1  :2  …                 letzten Vorschlag Nr. N direkt ausführen
  /review                   durch pending-Mails iterieren (a/r/s/b/q)
  /review fast              dito, aber ohne LLM-Hypothese (manuell, schnell)
  /reject <pattern>         alle pending mit Pattern in Betreff/Teilnehmer
                            bulk-aussortieren (mit Bestätigung)
  /list                     Status-Übersicht der aktuellen Session
  /sessions                 alle vorhandenen Sessions auflisten
  /gaps                     LLM-Lückenanalyse zwischen bestätigten Mails
  /summary                  narrative Zusammenfassung erzeugen
  /export                   Markdown + CSV + mails/ schreiben
  /case [<text>]            Fallbeschreibung anzeigen / setzen
  /edit <subject-fragment>  Kernaussage einer akzept. Mail ändern
  /undo <subject-fragment>  Mail zurück auf 'pending'
  /help                     diese Hilfe
  /quit                     Beenden (Session ist auto-gespeichert)

"Suche nach X" / "Finde X"  →  qmd-Suche (wie /search X).
Sonstiger freier Text       →  Frage an das LLM mit aktuellem Stand als Kontext.
"""


def main() -> None:
    p = argparse.ArgumentParser(
        description="Interaktiver Mail-Recherche-Agent (qmd + ollama).")
    p.add_argument("-s", "--session",
                   help="Session-Name. Pflicht außer bei --list-sessions.")
    p.add_argument("--list-sessions", action="store_true",
                   help="Vorhandene Sessions auflisten und beenden.")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL)
    p.add_argument("-n", "--top-k", type=int, default=20,
                   help="qmd Top-N pro Suche (Default 20).")
    p.add_argument("-o", "--out", default=str(DEFAULT_OUT),
                   help=f"Output-Verzeichnis (Default {DEFAULT_OUT}).")
    p.add_argument("-c", "--num-ctx", type=int,
                   default=int(os.environ.get("OLLAMA_NUM_CTX", "32768")))
    args = p.parse_args()

    if args.list_sessions:
        print_sessions()
        return
    if not args.session:
        p.error("--session/-s ist Pflicht (außer mit --list-sessions).")

    state = load_session(args.session)
    if not state["case_description"]:
        print("Kurze Fallbeschreibung (1-3 Sätze, später per /case änderbar):")
        try:
            state["case_description"] = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        save_session(state)

    counts = status_counts(state)
    print(f"\nMail-Recherche — Modell: {args.model}, Session: {args.session}")
    print(f"Output: {args.out}")
    print(f"Status: {counts['accepted']} akzept., {counts['pending']} pending, "
          f"{counts['rejected']} aussortiert. /help für Befehle.\n")

    while True:
        try:
            line = input("⚖  ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            if line in {"/quit", "/exit"}:
                break
            if line == "/help":
                print(HELP); continue
            if line == "/list":
                cmd_list(state); continue
            if line == "/sessions":
                print_sessions()
                print(f"(Aktuell: {args.session}. Wechseln = /quit + neu starten "
                      f"mit -s ANDERER_NAME.)")
                continue
            if line.startswith("/case"):
                rest = line[len("/case"):].strip()
                if rest:
                    state["case_description"] = rest
                    save_session(state)
                    print("Fallbeschreibung aktualisiert.")
                else:
                    print(state["case_description"] or "(leer)")
                continue
            if line.startswith("/search"):
                cmd_search(state, line[len("/search"):].strip(), args.top_k)
                save_session(state); continue
            if line.startswith("/add"):
                cmd_add(state, line[len("/add"):].strip())
                save_session(state); continue
            if line == "/suggest":
                cmd_suggest(state, args.model)
                save_session(state); continue
            if line.startswith(":") and line[1:].isdigit():
                idx = int(line[1:]) - 1
                sugg = state.get("last_suggestions", [])
                if 0 <= idx < len(sugg):
                    cmd_search(state, sugg[idx], args.top_k)
                    save_session(state)
                else:
                    print("Ungültige Nummer.")
                continue
            if line == "/review":
                cmd_review(state, args.model); continue
            if line in {"/review fast", "/review-fast"}:
                cmd_review(state, args.model, use_llm=False); continue
            if line.startswith("/reject"):
                cmd_reject(state, line[len("/reject"):].strip())
                save_session(state); continue
            if line == "/gaps":
                cmd_gaps(state, args.model); continue
            if line == "/summary":
                cmd_summary(state, args.out, args.model); continue
            if line == "/export":
                cmd_export(state, args.out); continue
            if line.startswith("/edit"):
                frag = line[len("/edit"):].strip().lower()
                hits = [c for c in state["candidates"].values()
                        if c["status"] == "accepted" and frag in c["subject"].lower()]
                if not hits:
                    print("Kein Treffer.")
                    continue
                if len(hits) > 1:
                    print(f"Mehrdeutig ({len(hits)} Treffer), bitte präziser.")
                    continue
                c = hits[0]
                new = input(f"Neue Kernaussage [{c['summary']}]: ").strip()
                if new:
                    c["summary"] = new
                    save_session(state)
                continue
            if line.startswith("/undo"):
                frag = line[len("/undo"):].strip().lower()
                for c in state["candidates"].values():
                    if c["status"] != "pending" and frag in c["subject"].lower():
                        c["status"] = "pending"
                        c["summary"] = ""
                        c["rejection_reason"] = ""
                        save_session(state)
                        print(f"→ {c['subject']} zurück auf pending.")
                        break
                else:
                    print("Kein Treffer.")
                continue
            m = SEARCH_INTENT_RE.match(line)
            if m:
                cmd_search(state, m.group(1).strip(), args.top_k)
                save_session(state)
                continue
            cmd_freeform(state, args.model, line, args.num_ctx)
        except urllib.error.URLError as e:
            print(f"Verbindung zu ollama fehlgeschlagen: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
