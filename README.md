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

**Optional, nur für `/dossier`-Export in Word/Excel:**

```bash
pip3 install openpyxl     # für .xlsx (Tabelle für Anwälte)
brew install pandoc       # für .docx (Zusammenfassung für Anwälte)
```

Fehlt eines der beiden, läuft `/dossier` trotzdem — du bekommst dann
nur die Markdown- und CSV-Varianten und einen Hinweis, was nachzuziehen
wäre.

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
/devil                    Anwalt der Gegenseite: Schwachstellen + Such-Stichworte
/context                  Kontext-Datei anzeigen
/context edit             Kontext-Datei in $EDITOR öffnen
/context <pfad.md>        anderen Pfad für Kontext-Datei setzen
/validate-context         prüft kontext.md gegen die akzeptierten Mails:
                          gestützt / widersprochen / unbelegt + Such-Stichworte
/summary                  narrative Zusammenfassung erzeugen
/export                   roher Dump (alle accepted): MD + CSV + mails/
/dossier                  kuratierter Gerichts-Export, validiert gegen
                          kontext.md, interne Korrespondenz gefiltert
                          (Alias: /akte)
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

### Kontext-Datei (Hintergrund-Briefing)

Du kannst dem LLM ein **selbst geschriebenes Markdown** als Kontext
beilegen — wer ist wer, Hintergrund, dein subjektiver Ablauf, worauf
zu achten ist. Der Inhalt wird automatisch jedem LLM-Aufruf (Review,
Suggest, Gaps, Devil, Summary, freier Chat) als „Hintergrund vom
Mandanten" mitgegeben.

- Default-Pfad: `<out_dir>/kontext.md` (also z. B.
  `~/marmalade-fall/output/kontext.md`).
- `/context edit` öffnet die Datei in `$EDITOR` (Fallback: `nano`/`vim`)
  und legt bei Bedarf ein Skelett mit Sektionen an.
- `/context <pfad>` setzt einen anderen Pfad (in der Session gespeichert).
- `/context` allein zeigt Pfad + Vorschau.

### `/dossier` (Alias `/akte`) — kuratierter Gerichts-Export

Endpunkt für die Aktenarbeit. Macht aus den akzeptierten Mails plus deiner
`kontext.md` einen strukturierten, gerichtstauglichen Output:

1. **Filtert interne Korrespondenz** automatisch raus — anhand einer
   Sektion `## Interne Kontakte` in `kontext.md`. Beispiel:
   ```markdown
   ## Interne Kontakte (NICHT exportieren)
   - Wolf
   - Warnken
   - taylorwessing.com
   - meine-assistenz@…
   ```
   Mails werden als „intern" klassifiziert, wenn ein Pattern als Substring
   in den Teilnehmern oder im Betreff auftaucht (case-insensitive).

2. **Validiert jede externe Mail gegen die Erinnerung.** Pro Mail markiert
   das LLM:
   - ✓ **bestätigt** — eine Aussage der Erinnerung wird durch die Mail belegt
   - ➕ **erweitert** — die Mail präzisiert/ergänzt die Erinnerung
   - ✗ **widerspricht** — Mail steht im Widerspruch zur Erinnerung
   - — **neu** — Mail führt einen Punkt ein, der in der Erinnerung fehlt

3. **Räumt vorher auf** — alle `akte_*`-Dateien und der Inhalt von
   `akte_mails/` werden vor dem Schreiben gelöscht. Keine Altlasten.

4. **Schreibt diese Artefakte:**
   - `akte_zeitlicher_ablauf.md` / `.csv` — Tabelle für das Gericht (Datum,
     Beteiligte, Ereignis, Bezug zur Erinnerung, Beleg-Datei)
   - `akte_zeitlicher_ablauf.xlsx` — Excel-Variante mit Auto-Filter
     und Spaltenbreiten (nur wenn `openpyxl` installiert ist)
   - `akte_zusammenfassung.md` — narrative Phasenübersicht für das Gericht
   - `akte_zusammenfassung.docx` — Word-Variante der Zusammenfassung
     (nur wenn `pandoc` installiert ist)
   - `akte_mails/` — Volltext aller externen Belege
   - `akte_intern.md` — Übersicht aussortierter interner Mails (NUR für
     dich, nicht für die Akte)

### `/validate-context` — Erinnerung gegen Mails prüfen

Nach Jahren stimmt die Erinnerung selten mit der Aktenlage überein. Dieser
Befehl schickt deine `kontext.md` zusammen mit der Timeline der akzeptierten
Mails ans LLM und teilt jede prüfbare Aussage in drei Töpfe:

- **✓ gestützt** — durch eine konkrete Mail belegt (Datum + Betreff)
- **✗ widersprochen** — Mail sagt etwas anderes als die Erinnerung
- **? unbelegt** — keine Mail-Spur, mit Such-Vorschlag zum Nachhaken

Die Such-Vorschläge landen wieder als nummerierte Liste, ausführbar per
`:1`, `:2`, … Außerdem ist im System-Prompt verankert: bei Konflikt
zwischen Erinnerung und Mail folgt das LLM grundsätzlich der Mail.

### `/devil` — Anwalt der Gegenseite

Schickt die akzeptierte Timeline + Kontext-Datei mit der Aufgabe „spiele
gegnerische Anwältin" ans LLM. Output: konkrete Schwachstellen und pro
Punkt eine qmd-Stichwortsuche, die du gleich per `:1`, `:2`, … ausführen
kannst, um Belege nachzulegen oder gezielt nach Munition zu suchen.

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
