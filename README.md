# simple-ki-chat

Lokale CLI-Tools, um aus einem [`qmd`](https://github.com/tobi/qmd)-Index
über Mail-Threads (Markdown mit YAML-Frontmatter) zu recherchieren — alles
gegen ein lokales [ollama](https://ollama.com/), keine Cloud.

Zwei Tools im selben Repo:

- **`chat.py`** — schlanker Frage-Antwort-Chatbot mit RAG. Eine Frage,
  qmd holt die Top-K-Mails, ollama antwortet. Sessions optional.
- **`recherche.py`** — interaktiver Recherche-Agent für längere
  Fall-Recherche (z. B. Aktenrekonstruktion für Anwälte). Orchestriert
  qmd-Suchen, bewertet jeden Mail-Thread per LLM-Hypothese, baut
  inkrementell eine Akte auf und exportiert am Ende Markdown + CSV +
  Mail-Ordner.

Geteilter qmd-Helper liegt in `qmd.py`.

## Voraussetzungen

- Python 3.10+
- [`qmd`](https://github.com/tobi/qmd) installiert mit indizierter
  Mail-Collection
- `ollama serve` läuft (CLI, **nicht** die GUI-App — die liest die
  ENV-Vars wie `OLLAMA_CONTEXT_LENGTH` nicht)
- Mindestens ein passendes Modell, z. B.
  `ollama pull qwen2.5:32b` oder `ollama pull gpt-oss:20b`

## `chat.py` — schneller RAG-Chat

```bash
# REPL starten
python3 chat.py

# Mit Session-Persistenz (lädt vorhandene, speichert auto)
python3 chat.py -s vermieter-streit

# Einmalige Frage
python3 chat.py -s vermieter-streit "Worum ging es im Streit mit der Hausverwaltung?"
```

**Optionen:**

| Flag | Env | Default | Bedeutung |
|------|-----|---------|-----------|
| `-s NAME` | — | (keine) | Session-Name. Auto-Save nach jedem Turn. |
| `-m MODEL` | `OLLAMA_MODEL` | `gpt-oss:20b` | ollama-Modell |
| `-n K` | `CHATBOT_TOP_K` | 6 | Top-K Mails pro Frage |
| `-l N` | `CHATBOT_MAX_LINES` | 200 | max. Zeilen pro Mail |
| `-c N` | `OLLAMA_NUM_CTX` | 16384 | Kontextfenster |
| — | `OLLAMA_URL` | `http://localhost:11434` | ollama-Endpunkt |
| — | `CHATBOT_SESSIONS_DIR` | `~/.local/share/mini-chatbot/sessions/` | Speicherort |

**Sessions:** mit `-s NAME` startet/lädt eine Session. Bereits in den
Verlauf geladene Mails werden bei Folge-Fragen *nicht* erneut in den
Prompt gepackt — Mail-Dedup hält den Kontext linear klein. REPL-Befehle
in der Session: `/reset` (Verlauf löschen), `/quit`.

## `recherche.py` — Akten-Recherche-Agent

Für strukturierte Recherche: gehe iterativ durch Suchergebnisse, lass
das LLM Relevanz und Kernaussage vorschlagen, bestätige/verwerfe pro
Thread, exportiere am Ende.

```bash
python3 recherche.py -s marmalade-fall
```

Beim ersten Start fragt das Tool nach einer kurzen Fallbeschreibung
(1–3 Sätze). Diese steuert alle späteren LLM-Aufrufe.

**Optionen:**

| Flag | Env | Default | Bedeutung |
|------|-----|---------|-----------|
| `-s NAME` | — | **Pflicht** | Session-Name |
| `-m MODEL` | `OLLAMA_MODEL` | `qwen2.5:32b` | ollama-Modell |
| `-n K` | — | 20 | qmd Top-N pro Suche |
| `-o DIR` | — | `~/marmalade-fall/output` | Output-Verzeichnis |
| `-c N` | `OLLAMA_NUM_CTX` | 32768 | Kontextfenster |

### REPL-Befehle

```
/search <query>           qmd-Suche, fügt Treffer als 'pending' hinzu
/add <uri> [<uri> ...]    URIs direkt aufnehmen (z. B. aus eigenen qmd-Läufen)
/add @<datei>             Zeilen aus Datei einlesen, qmd://…-URIs extrahieren
/suggest                  LLM schlägt 3-5 Suchanfragen vor
:1  :2  …                 Vorschlag Nr. N direkt ausführen
/review                   pending-Mails durchgehen (mit LLM-Hypothese)
/review fast              dito ohne LLM — nur Frontmatter+Body, schnell
/reject <pattern>         Bulk-Reject pending Mails mit Pattern in
                          Betreff/Teilnehmer (zeigt Treffer + bestätigen)
/list                     Status-Übersicht der aktuellen Session
/sessions                 alle gespeicherten Sessions auflisten
/gaps                     LLM analysiert zeitliche Lücken
/summary                  narrative Zusammenfassung erzeugen
/export                   schreibt Markdown + CSV + mails/-Ordner
/case [<text>]            Fallbeschreibung anzeigen / setzen
/edit <subject-fragment>  Kernaussage einer akzept. Mail ändern
/undo <subject-fragment>  Mail zurück auf 'pending'
/help                     Hilfe
/quit                     Beenden (Session ist auto-gespeichert)
```

**Natürlichsprachliche Eingabe:**

- `Suche nach <begriff>` / `Finde <begriff>` — wird als qmd-Suche ausgeführt
  (Äquivalent zu `/search <begriff>`). Erkannt werden auch „such mal nach …",
  „find …" usw.
- Alles andere ohne `/` geht als Frage an das LLM — mit Fallbeschreibung und
  allen bestätigten Mail-Kernaussagen als Kontext.

### Workflow

1. **Erste Suchen** mit `/search Eric Fischer` etc. — oder `/suggest`
   und dann `:1`, `:2`, … für die vorgeschlagenen Anfragen.
2. **`/review`** geht durch alle pending-Treffer. Pro Thread zeigt der
   Agent Frontmatter + Body-Auszug + LLM-Hypothese (relevant ja/nein,
   Kernaussage). Du wählst:
   - `a` akzeptieren (Kernaussage editierbar)
   - `r` aussortieren (mit Begründung)
   - `s` skip (bleibt pending)
   - `b` Body anzeigen
   - `q` Review verlassen, Session bleibt erhalten
3. **`/gaps`** für zeitliche Lücken in der bestätigten Timeline →
   konkrete Such-Vorschläge zum Schließen.
4. **`/export`** schreibt:
   - `zeitlicher_ablauf.md` — Markdown-Tabelle, chronologisch
   - `zeitlicher_ablauf.csv` — dieselbe Tabelle als CSV
   - `mails/` — alle akzeptierten Threads als Volltext-Markdown
5. **`/summary`** erzeugt zusätzlich `zusammenfassung.md` (narrative
   Phasen-Beschreibung, Lücken werden explizit benannt).

### Sessions

Liegen unter `~/.local/share/mini-chatbot/sessions/recherche_<NAME>.json`.
Auflisten ohne eine zu starten:

```bash
python3 recherche.py --list-sessions
```

Im REPL listet `/sessions` dasselbe (zum Wechseln aktuell `/quit` + neu
starten mit anderem `-s`).

Enthalten Fallbeschreibung, alle Kandidaten mit Status, Suchhistorie,
letzte Vorschläge. Wiederaufnahme einfach mit demselben `-s NAME`.

Aussortierte Mails werden nicht gelöscht, sondern als `rejected` mit
optionalem Grund markiert — Bewertung kann jederzeit per `/undo` zurück
auf `pending` gesetzt werden.

### Prinzipien

- **Joscha kennt den Ablauf nicht** — der Agent stellt Hypothesen aus
  Mailinhalten, fordert keine Bestätigung bekannter Fakten.
- **Datum/Teilnehmer aus YAML-Frontmatter**, nie aus dem Mail-Text
  geraten.
- **Threads** (eine `.md`-Datei = ein Thread mit ≥1 Mails) sind die
  Bewertungseinheit.
- **Keine Cloud**, kein externes Logging — alles bleibt lokal.

## Hinweise zu ollama

Wenn du auf dem Mac mit der ollama-GUI arbeitest, beende sie und
starte stattdessen `ollama serve` im Terminal — nur dann werden
ENV-Variablen wie `OLLAMA_CONTEXT_LENGTH` und `OLLAMA_KEEP_ALIVE`
respektiert:

```bash
pkill -9 ollama
ollama serve
```
