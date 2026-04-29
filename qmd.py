"""qmd-Helfer: Suche, Volltext, Frontmatter-Parsing."""
import re
import subprocess

URI_RE = re.compile(r"qmd://[^\s:]+\.md")


def search(query: str, n: int = 10, *, all_results: bool = False,
           min_score: float | None = None) -> list[str]:
    """Top-N Mail-URIs via qmd hybrid search."""
    cmd = ["qmd", "query", "--files", "-n", str(n), query]
    if all_results:
        cmd.append("--all")
    if min_score is not None:
        cmd += ["--min-score", str(min_score)]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    uris, seen = [], set()
    for line in res.stdout.splitlines():
        m = URI_RE.search(line.strip())
        if m and m.group(0) not in seen:
            seen.add(m.group(0))
            uris.append(m.group(0))
    return uris


def fetch(uri: str, *, full: bool = False, max_lines: int | None = None) -> str:
    cmd = ["qmd", "get", uri]
    if full:
        cmd.append("--full")
    if max_lines:
        cmd += ["-l", str(max_lines)]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Trennt YAML-Frontmatter von Body. Robust gegen einfache Strings/Listen."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    yaml_block, body = m.group(1), m.group(2)
    meta: dict = {}
    for line in yaml_block.split("\n"):
        if ": " not in line:
            continue
        key, val = line.split(": ", 1)
        key, val = key.strip(), val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            meta[key] = [x.strip().strip('"') for x in inner.split(",") if x.strip()]
        else:
            meta[key] = val
    return meta, body


_MONTHS = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05",
           "Jun": "06", "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10",
           "Nov": "11", "Dec": "12"}


def normalize_date(date_str: str) -> str:
    """'Mon, 29 Apr 2024 14:01:00 +0000' → '2024-04-29'. Fallback: Original."""
    if not date_str:
        return ""
    m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", date_str)
    if m:
        d, mon, y = m.groups()
        return f"{y}-{_MONTHS.get(mon, '??')}-{int(d):02d}"
    return date_str
