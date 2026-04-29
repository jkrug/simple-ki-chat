#!/usr/bin/env python3
"""Lokaler Mail-Chatbot: qmd (semantische Suche) + ollama (Antwort)."""
import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import qmd

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

SESSIONS_DIR = Path(
    os.environ.get("CHATBOT_SESSIONS_DIR")
    or (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "mini-chatbot" / "sessions")
)

SYSTEM_PROMPT = (
    "Du hilfst Joscha, aus seinen privaten E-Mails Sachverhalte für seine "
    "Anwälte zusammenzufassen. Antworte präzise auf Deutsch und ausschließlich "
    "auf Basis der zitierten E-Mails (sowohl in dieser Nachricht als auch "
    "früher in der Unterhaltung gezeigte). Wenn etwas nicht in den Mails steht, "
    "sage das klar. Nenne Datum, Absender und Empfänger, wenn relevant. "
    "Liste am Ende die verwendeten Quellen (Dateinamen)."
)

def build_context(uris: list[str], max_lines: int) -> str:
    return "\n\n".join(
        f"=== {u} ===\n{qmd.fetch(u, max_lines=max_lines)}" for u in uris
    )


def chat(model: str, messages: list[dict], num_ctx: int) -> str:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    out = []
    with urllib.request.urlopen(req) as resp:
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


def session_path(name: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", name)
    return SESSIONS_DIR / f"{safe}.json"


def load_session(name: str) -> tuple[list[dict], set[str]]:
    path = session_path(name)
    if not path.exists():
        return [{"role": "system", "content": SYSTEM_PROMPT}], set()
    data = json.loads(path.read_text())
    return data.get("messages", []), set(data.get("loaded_uris", []))


def save_session(name: str, messages: list[dict], loaded: set[str]) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = session_path(name)
    path.write_text(json.dumps(
        {"messages": messages, "loaded_uris": sorted(loaded)},
        ensure_ascii=False, indent=2,
    ))


def main() -> None:
    p = argparse.ArgumentParser(description="Lokaler Mail-Chatbot (qmd + ollama).")
    p.add_argument("-n", "--top-k", type=int,
                   default=int(os.environ.get("CHATBOT_TOP_K", "6")))
    p.add_argument("-m", "--model",
                   default=os.environ.get("OLLAMA_MODEL", "gpt-oss:20b"))
    p.add_argument("-l", "--max-lines", type=int,
                   default=int(os.environ.get("CHATBOT_MAX_LINES", "200")),
                   help="Maximale Zeilen pro geladener Mail")
    p.add_argument("-c", "--num-ctx", type=int,
                   default=int(os.environ.get("OLLAMA_NUM_CTX", "16384")),
                   help="Kontextfenster in Tokens")
    p.add_argument("-s", "--session",
                   help="Session-Name. Wird auto. geladen/gespeichert in "
                        f"{SESSIONS_DIR}.")
    p.add_argument("question", nargs="*",
                   help="Optional einmalige Frage; ohne Argument startet REPL.")
    args = p.parse_args()

    if args.session:
        history, loaded_uris = load_session(args.session)
        if len(history) > 1:
            print(f"Session '{args.session}' geladen: {len(history)-1} "
                  f"Nachrichten, {len(loaded_uris)} Mails im Verlauf.",
                  file=sys.stderr)
    else:
        history = [{"role": "system", "content": SYSTEM_PROMPT}]
        loaded_uris: set[str] = set()

    def turn(question: str) -> None:
        print(f"\n→ Suche Mails: {question!r}", file=sys.stderr)
        hits = qmd.search(question, args.top_k)
        new = [u for u in hits if u not in loaded_uris]
        print(f"→ {len(hits)} Treffer, davon {len(new)} neu.", file=sys.stderr)
        for u in new:
            print(f"   + {u}", file=sys.stderr)
        for u in hits:
            if u not in new:
                print(f"   . {u} (bereits im Verlauf)", file=sys.stderr)

        if new:
            ctx = build_context(new, args.max_lines)
            user_msg = (f"### Neue relevante E-Mails:\n\n{ctx}\n\n"
                        f"### Frage:\n{question}")
        else:
            user_msg = (f"### Frage (keine neuen Mails — nutze den bisherigen "
                        f"Verlauf):\n{question}")

        history.append({"role": "user", "content": user_msg})
        reply = chat(args.model, history, args.num_ctx)
        history.append({"role": "assistant", "content": reply})
        loaded_uris.update(hits)

        if args.session:
            save_session(args.session, history, loaded_uris)

    if args.question:
        turn(" ".join(args.question))
        return

    print(f"Mail-Chatbot (Modell: {args.model}, Top-K: {args.top_k}, "
          f"num_ctx: {args.num_ctx}"
          + (f", Session: {args.session}" if args.session else "")
          + "). Befehle: /reset, /quit. Strg-D beendet.",
          file=sys.stderr)
    while True:
        try:
            q = input("\n? ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in {"/quit", "/exit"}:
            break
        if q == "/reset":
            del history[1:]
            loaded_uris.clear()
            if args.session:
                save_session(args.session, history, loaded_uris)
            print("Verlauf gelöscht.", file=sys.stderr)
            continue
        turn(q)


if __name__ == "__main__":
    main()
