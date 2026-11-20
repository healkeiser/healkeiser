"""Render the profile README, slate and tracker cards from live GitHub data.

Reads ``README.template.md`` and ``slate/slate.template.svg``, fills their
``{{...}}`` placeholders, and writes ``README.md`` plus the SVGs in
``slate/dark/`` and ``slate/light/``: the slate, the table heads and feet, one
card per shot and one card per daily. Each card is its own image so each can
carry its own link on GitHub; each exists in both themes so a ``<picture>``
can follow the viewer's GitHub theme.

Every image also has a narrow layout: below ``NARROW`` pixels of rendered
width (a phone), its own CSS hides the wide fields and shows fewer, larger
ones. The image keeps its aspect ratio, so the narrow layout is designed for
the same box at about 0.43 scale.

Shots are the pinned repositories, in pin order: change them by pinning
repositories on the profile, not by editing this file.

Examples:
    Render with a token from the GitHub CLI::

        GITHUB_TOKEN=$(gh auth token) python scripts/build_readme.py

    Check the pure helpers without network access::

        python scripts/build_readme.py --self-test
"""

# Built-in
import datetime as dt
import json
import os
import re
import shutil
import sys
import textwrap
import urllib.request
from html import escape
from pathlib import Path


###### Constants

USER = "healkeiser"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "slate"
THEMES = ("dark", "light")
WIDTH = 840
NARROW = 560  # rendered width, in CSS pixels, below which the narrow layout shows
FIRST_FRAME = 1001
FPS = 24
HOLD_AFTER_DAYS = 180
WAVE_DAYS = 120
IMPACT = 1.07  # seconds: when the slate's arm lands (see slate.template.svg)
PLAY_SECONDS = 9  # one pass of the waveform playhead
ROW_STEP = 0.07  # seconds between rows sliding in
DAILIES_COUNT = 5
DAILIES_PER_REPO = 2
DAILIES_SCAN = 15  # recently pushed repos scanned for dailies
VERSIONS = 3  # versions listed per shot
# Commits that are housekeeping, not work worth showing; matches conventional,
# bracketed ([DOC]) and plain-sentence styles.
NOISE = re.compile(
    r"^(\[?(chore|docs?|build|ci|style|tests?|refactor|release|wip)\b|merge\b|bump\b)"
    r"|changelog|readme|funding|sponsor|settings\.local",
    re.IGNORECASE,
)
PREFIX = re.compile(r"^(\[(\w+)\]|(\w+)(\([^)]*\))?!?:)\s*")
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
CHAR = 0.6  # width of a monospace glyph, in em

# GitHub's own palettes (Primer), one per theme.
PALETTES = {
    "dark": {
        "bg": "#0d1117", "panel": "#161b22", "ink": "#e6edf3", "line": "#30363d", "mute": "#8b949e",
        "ok": "#3fb950", "okb": "#1f6f3a55", "wip": "#d29922", "wipb": "#9e6a0355",
        "hold": "#8b949e", "holdb": "#30363d88", "fix": "#f85149", "fixb": "#da363355",
    },
    "light": {
        "bg": "#ffffff", "panel": "#f6f8fa", "ink": "#1f2328", "line": "#d0d7de", "mute": "#59636e",
        "ok": "#1a7f37", "okb": "#dafbe1", "wip": "#9a6700", "wipb": "#fff8c5",
        "hold": "#59636e", "holdb": "#eaeef2", "fix": "#cf222e", "fixb": "#ffebe9",
    },
}

# Shared by the slate template and the cards: layout switch and motion. The
# reduced-motion rule needs !important: the timecode sets its animation inline.
COMMON_CSS = f"""
text{{font-family:{MONO};fill:var(--ink)}}
.n{{display:none}}
@media (max-width:{NARROW}px){{.w{{display:none}}.n{{display:inline}}}}
.pulse{{animation:pulse 1.6s ease-in-out infinite}}
@keyframes pulse{{50%{{opacity:.2}}}}
@media (prefers-reduced-motion: reduce){{.in,.pulse,.tc{{animation:none!important}}}}
"""

CARD_CSS = """
.k{fill:var(--mute);font-size:10px;letter-spacing:1.5px}
.m{fill:var(--mute)} .ln{stroke:var(--line);stroke-width:1.5;fill:none}
.bg{fill:var(--bg)} .panel{fill:var(--panel)} .ink{fill:var(--ink)}
.in{animation:rise .6s cubic-bezier(.2,.8,.2,1) both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
"""

PILLS = {  # kind: (label, text colour, fill colour)
    "approved": ("APPROVED", "--ok", "--okb"),
    "wip": ("IN PROGRESS", "--wip", "--wipb"),
    "hold": ("ON HOLD", "--hold", "--holdb"),
    "feat": ("FEAT", "--ok", "--okb"),
    "fix": ("FIX", "--fix", "--fixb"),
    "note": ("NOTE", "--hold", "--holdb"),
}

QUERY = """
query($login: String!, $dailies: Int!, $versions: Int!) {
  user(login: $login) {
    pinnedItems(first: 6, types: REPOSITORY) {
      nodes { ... on Repository { ...repo } }
    }
    repositories(first: $dailies, privacy: PUBLIC, isFork: false,
                 ownerAffiliations: OWNER,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes { ...repo }
    }
    contributionsCollection {
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
  }
}
fragment repo on Repository {
  name url description isPrivate isArchived pushedAt
  primaryLanguage { name color }
  releases(first: $versions, orderBy: {field: CREATED_AT, direction: DESC}) {
    nodes { tagName isPrerelease }
  }
  refs(refPrefix: "refs/tags/", first: $versions,
       orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) { nodes { name } }
  defaultBranchRef { name target { ... on Commit {
    history(first: 100) {
      totalCount nodes { oid messageHeadline committedDate url }
    }
  } } }
}
"""


###### Pure helpers


def fit(text: str, width: float, size: float) -> str:
    """Shorten text with an ellipsis so it fits a pixel width.

    Args:
        text: The text to fit.
        width: Available width in pixels.
        size: Font size in pixels (monospace).

    Returns:
        The text, cut and ended with ``…`` when it is too long.
    """
    limit = int(width / (size * CHAR) + 1e-9)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def wrap(text: str, width: float, size: float, lines: int = 2) -> list[str]:
    """Wrap text on words into at most ``lines`` lines that fit a pixel width."""
    limit = int(width / (size * CHAR) + 1e-9)
    parts = textwrap.wrap(text, limit) or [""]
    if len(parts) > lines:
        parts = parts[: lines - 1] + [fit(" ".join(parts[lines - 1 :]), width, size)]
    return parts


def version_of(tag: str | None) -> str | None:
    """Extract a ``v``-prefixed version from a tag such as ``fxgui-15.4.3``."""
    match = re.search(r"\d+(?:\.\d+)*", tag or "")
    return f"v{match.group(0)}" if match else None


def versions_of(repo: dict) -> list[str]:
    """Return the repository's latest versions, newest first.

    Stable releases come first; tags stand in when there are no releases.
    """
    releases = [r["tagName"] for r in (repo.get("releases") or {}).get("nodes", []) if not r["isPrerelease"]]
    tags = [t["name"] for t in (repo.get("refs") or {}).get("nodes", [])]
    found = [v for v in map(version_of, releases or tags) if v]
    return list(dict.fromkeys(found))  # keeps order, drops repeats


def status_of(version: str | None, pushed: dt.date, archived: bool, today: dt.date) -> str:
    """Pick the shot status for a repository.

    Args:
        version: The repository version, as returned by ``version_of``.
        pushed: Date of the last push.
        archived: Whether the repository is archived.
        today: The reference date.

    Returns:
        ``hold`` when archived or idle, ``approved`` from version 1.0, else ``wip``.
    """
    if archived or (today - pushed).days > HOLD_AFTER_DAYS:
        return "hold"
    if version and int(version[1:].split(".")[0]) >= 1:
        return "approved"
    return "wip"


def note_of(headline: str) -> tuple[str, str]:
    """Split a commit headline into a note type and a plain sentence.

    Args:
        headline: A commit headline, such as ``fix(parms): a ramp is kept``.

    Returns:
        ``(kind, sentence)`` where kind is ``feat``, ``fix`` or ``note``.
    """
    match = PREFIX.match(headline)
    kind = (match.group(2) or match.group(3)).lower() if match else ""
    note = headline[match.end() :] if match else headline
    kind = {"feat": "feat", "fix": "fix", "perf": "feat"}.get(kind, "note")
    return kind, note[:1].upper() + note[1:]


def duration(seconds: float | None) -> str:
    """Format seconds as ``MM:SS``, or ``—`` when unknown."""
    if seconds is None or seconds < 0:
        return "—"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def compact(number: int) -> str:
    """Format a line count the way GitHub does past a thousand: ``1.2k``."""
    return str(number) if number < 1000 else f"{number / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def diffstat(additions: int, deletions: int) -> list[str]:
    """Return GitHub's five-block diffstat: ``ok`` and ``fix`` blocks, ``hold`` for the rest."""
    total = additions + deletions
    if not total:
        return ["hold"] * 5
    green = round(5 * additions / total)
    return ["ok"] * green + ["fix"] * (5 - green)


###### GitHub


def request(url: str, token: str, body: dict | None = None) -> dict:
    """Send a GitHub API request and decode its JSON."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"bearer {token}", "User-Agent": "profile-readme"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def fetch(token: str) -> dict:
    """Return the ``user`` object of the GraphQL query."""
    variables = {"login": USER, "dailies": DAILIES_SCAN, "versions": VERSIONS}
    payload = request("https://api.github.com/graphql", token, {"query": QUERY, "variables": variables})
    if "errors" in payload:
        raise RuntimeError(payload["errors"])
    return payload["data"]["user"]


def render_time(token: str, repo: dict) -> float | None:
    """Return the duration, in seconds, of the repo's last successful CI run on push.

    Returns:
        None when the repository has no such run.
    """
    branch = (repo.get("defaultBranchRef") or {}).get("name")
    if not branch:
        return None
    url = (
        f"https://api.github.com/repos/{USER}/{repo['name']}/actions/runs"
        f"?branch={branch}&event=push&status=success&per_page=1"
    )
    try:
        runs = request(url, token)["workflow_runs"]
    except OSError:  # HTTP and network errors: show "—", keep rendering
        return None
    if not runs:
        return None
    start = dt.datetime.fromisoformat(runs[0]["run_started_at"].replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(runs[0]["updated_at"].replace("Z", "+00:00"))
    return (end - start).total_seconds()


def change_size(token: str, repo: str, oid: str) -> tuple[int, int]:
    """Return a commit's ``(additions, deletions)``.

    Asked per daily, not in the GraphQL query: line counts on every scanned
    commit make that query time out.
    """
    try:
        stats = request(f"https://api.github.com/repos/{USER}/{repo}/commits/{oid}", token).get("stats") or {}
    except OSError:  # HTTP and network errors: show an empty diffstat, keep rendering
        return 0, 0
    return stats.get("additions", 0), stats.get("deletions", 0)


def history_of(repo: dict) -> dict:
    """Return the default-branch history block, empty when there are no commits."""
    target = (repo.get("defaultBranchRef") or {}).get("target") or {}
    return target.get("history") or {"totalCount": 0, "nodes": []}


###### SVG primitives


def palette(theme: str) -> str:
    """Return the CSS custom properties of a theme."""
    return ":root{" + ";".join(f"--{key}:{value}" for key, value in PALETTES[theme].items()) + "}"


def text(
    x: float, y: float, content: str, cls: str = "", size: float = 12, weight: int = 400, anchor: str = ""
) -> str:
    """Return an escaped SVG text element."""
    klass = f' class="{cls}"' if cls else ""
    end = f' text-anchor="{anchor}"' if anchor else ""
    return f'<text x="{x}" y="{y}"{klass}{end} font-size="{size}" font-weight="{weight}">{escape(content)}</text>'


def wide(body: str) -> str:
    """Show content only in the wide layout."""
    return f'<g class="w">{body}</g>'


def narrow(body: str) -> str:
    """Show content only in the narrow (phone) layout."""
    return f'<g class="n">{body}</g>'


def pill(x: float, y: float, kind: str, width: float, size: float = 10) -> str:
    """Return a bordered pill; ``y`` is its vertical centre, ``size`` its font size."""
    label, ink, fill = PILLS[kind]
    height = size * 1.9
    live = kind == "wip"  # work in progress gets a pulsing dot, like a recording light
    centre = x + (width + size * 1.4) / 2 if live else x + width / 2
    return (
        f'<rect x="{x + .5}" y="{y - height / 2:.1f}" width="{width - 1}" height="{height:.1f}" rx="{height / 2:.1f}" '
        f'style="fill:var({fill});stroke:var({ink});stroke-opacity:.45;stroke-width:{size / 10:.1f}"/>'
        + (f'<circle class="pulse" cx="{x + size * 1.1:.1f}" cy="{y}" r="{size / 4:.1f}" style="fill:var({ink})"/>' if live else "")
        + f'<text x="{centre:.1f}" y="{y + size * .35:.1f}" font-size="{size}" text-anchor="middle" '
        f'letter-spacing="{size * .06:.2f}" style="fill:var({ink})">{label}</text>'
    )


def timecode(x: float, y: float, size: float, uid: str) -> str:
    """Return a running ``HH:MM:SS:FF`` timecode at ``FPS``, from the clap's impact.

    Each digit is a column of glyphs in a clip window, scrolled one glyph at a
    time by a stepped animation, so the fields roll like a slate's counter.

    Args:
        x: Left edge.
        y: Baseline.
        size: Font size.
        uid: Unique suffix for the clip path id within the document.

    Returns:
        SVG markup, with the keyframes it needs.
    """
    base, line = 10, 12  # drawn at font size 10, then scaled
    glyph = base * CHAR
    digits = "0123456789"
    fields = [  # (glyphs, seconds per full turn); None marks a fixed glyph
        ("0", None), ("0", None), (":", None),
        ("012345", 3600), (digits, 600), (":", None),
        ("012345", 60), (digits, 10), (":", None),
        ([f"{f:02d}" for f in range(FPS)], 1),
    ]
    parts, cursor, frames = [], 0.0, set()
    for glyphs, period in fields:
        if period is None:
            parts.append(f'<text x="{cursor}" y="0" font-size="{base}">{glyphs}</text>')
            cursor += glyph
            continue
        steps = len(glyphs)
        frames.add(steps)
        column = "".join(f'<text x="{cursor}" y="{i * line}" font-size="{base}">{g}</text>' for i, g in enumerate(glyphs))
        parts.append(
            f'<g class="tc" style="animation:tc{steps} {period}s steps({steps}) {IMPACT}s infinite">{column}</g>'
        )
        cursor += glyph * len(glyphs[0])
    keyframes = "".join(f"@keyframes tc{n}{{to{{transform:translateY(-{n * line}px)}}}}" for n in sorted(frames))
    return (
        f"<style>{keyframes}</style>"
        f'<clipPath id="tc{uid}"><rect x="-1" y="{-line * .85}" width="{cursor + 2}" height="{line * 1.15}"/></clipPath>'
        f'<g transform="translate({x} {y}) scale({size / base})" clip-path="url(#tc{uid})" font-weight="600">'
        + "".join(parts)
        + "</g>"
    )


def svg(
    theme: str, height: int, body: str, label: str, part: str = "row", top: int = 0, bottom: int = 0, delay: float = 0
) -> str:
    """Wrap one piece of a framed table in a themed SVG document.

    A table is a ``head`` (rounded top corners), ``row`` pieces and a ``foot``
    (rounded bottom corners), each its own image so each row can carry its own
    link. The README floats them (``align="right"``), which stacks them with no
    gap, so the pieces join into one frame.

    Args:
        theme: ``dark`` or ``light``.
        height: Height of the piece, in pixels.
        body: SVG content, drawn from the piece's top-left corner.
        label: Accessible label of the image.
        part: ``head``, ``row`` or ``foot``.
        top: Transparent space above the piece.
        bottom: Transparent space below the piece.
        delay: Seconds after the clap's impact before the piece slides in, so
            a table's rows arrive in turn.

    Returns:
        The SVG document.
    """
    a, r, w = 0.75, 10, WIDTH
    if part == "head":
        outline = f"M{a},{height} V{r + a} A{r},{r} 0 0 1 {r + a},{a} H{w - r - a} A{r},{r} 0 0 1 {w - a},{r + a} V{height}"
    elif part == "foot":
        outline = f"M{a},0 V{height - r - a} A{r},{r} 0 0 0 {r + a},{height - a} H{w - r - a} A{r},{r} 0 0 0 {w - a},{height - r - a} V0"
    else:
        outline = f"M{a},0 V{height} M{w - a},0 V{height}"
    fill = outline if part != "row" else f"M0,0 H{w} V{height} H0"
    rule = f'<line x1="0" y1="{height - a}" x2="{w}" y2="{height - a}" class="ln"/>' if part != "foot" else ""
    total = top + height + bottom
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{total}" '
        f'viewBox="0 0 {w} {total}" role="img" aria-label="{escape(label)}">'
        f"<style>{palette(theme)}{COMMON_CSS}{CARD_CSS}</style>"
        f'<g transform="translate(0 {top})"><g class="in" style="animation-delay:{IMPACT + delay:.2f}s">'
        f'<path d="{fill} Z" class="{"panel" if part == "foot" else "bg"}"/>'
        f'{body}{rule}<path d="{outline}" class="ln"/></g></g></svg>'
    )


###### Tables


def header(
    theme: str, title: str, summary: str, columns: list[tuple[float, str]], narrow_columns: list[tuple[float, str]], delay: float = 0
) -> str:
    """Return a table head: a title bar and the column labels."""
    a, r, w = 0.75, 10, WIDTH
    bar = f"M{a},40 V{r + a} A{r},{r} 0 0 1 {r + a},{a} H{w - r - a} A{r},{r} 0 0 1 {w - a},{r + a} V40 Z"
    body = (
        f'<path d="{bar}" class="panel"/><line x1="0" y1="40" x2="{w}" y2="40" class="ln"/>'
        + wide(
            text(16, 25, title, size=13, weight=700)
            + text(16 + (len(title) + 2) * 13 * CHAR, 25, summary, "m", 11)
            + "".join(text(x, 61, label, "k") for x, label in columns)
        )
        + narrow(
            text(16, 30, title, size=24, weight=700)
            + "".join(text(x, 67, label, "k", 20) for x, label in narrow_columns)
        )
    )
    return svg(theme, 74, body, f"{title}: {summary}", "head", top=16, delay=delay)


def footer(theme: str, left: str, short: str, right: str = "", delay: float = 0) -> str:
    """Return a table foot: a closing bar with a summary on the left."""
    body = wide(text(16, 25, left, "k")) + narrow(text(16, 29, short, "k", 22)) + right
    return svg(theme, 40, body, left, "foot", bottom=16, delay=delay)


def status_bar(counts: dict[str, int]) -> str:
    """Return a segmented bar of shot statuses for the right of a footer."""
    x, end, total = 600, WIDTH - 16, sum(counts.values()) or 1
    parts = []
    for kind, count in counts.items():
        if count:
            width = (end - x) * count / total
            parts.append(
                f'<rect x="{x:.1f}" y="16" width="{max(width - 3, 2):.1f}" height="8" rx="4" '
                f'style="fill:var({PILLS[kind][1]})"/>'
            )
            x += width
    return "".join(parts)


SHOT_COLUMNS = [(100, "SHOT"), (290, "DESCRIPTION"), (548, "FRAMES"), (628, "VER"), (692, "RT"), (732, "STATUS")]
SHOT_NARROW = [(100, "SHOT"), (604, "STATUS")]


def shot_card(theme: str, code: str, repo: dict, info: dict, delay: float = 0) -> str:
    """Return the card of one shot."""
    language = repo.get("primaryLanguage") or {}
    color = language.get("color") or "#8b949e"
    white = 'fill="#fff" style="fill:#fff"'
    lines = wrap(repo["description"] or "", 240, 11.5)
    versions = info["versions"] or ["—"]
    body = (
        # Thumbnail: a plate graded from the language colour, with the shot number.
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{color}"/><stop offset="1" stop-color="{color}" stop-opacity=".25"/></linearGradient></defs>'
        f'<rect x="14" y="10" width="72" height="44" rx="4" fill="#1b1f24" style="fill:#1b1f24"/>'
        f'<rect x="14" y="10" width="72" height="44" rx="4" fill="url(#g)"/>'
        f'<rect x="14.5" y="10.5" width="71" height="43" rx="4" style="fill:none;stroke:var(--ink);stroke-opacity:.3;stroke-width:1"/>'
        f'<text x="50" y="33" font-size="14" font-weight="700" text-anchor="middle" {white}>{escape(code[2:])}</text>'
        f'<text x="50" y="47" font-size="8.5" text-anchor="middle" letter-spacing=".5" opacity=".85" {white}>'
        f'{escape(fit(language.get("name", "").upper(), 66, 8.5))}</text>'
        + wide(
            text(100, 27, code, "m", 10.5)
            + text(100, 45, fit(repo["name"], 180, 13), size=13, weight=700)
            + "".join(text(290, 28 + i * 16 - (8 if len(lines) == 1 else 0), line, size=11.5) for i, line in enumerate(lines))
            + text(548, 37, info["frames"], size=11.5)
            # Version stack: the current version, the earlier ones under it.
            + text(628, 31 if len(versions) > 1 else 37, fit(versions[0], 60, 11.5), size=11.5, weight=600)
            + "".join(text(628, 31 + i * 11.5, fit(v, 60, 9), "m", 9) for i, v in enumerate(versions[1:], start=1))
            + text(692, 37, info["rt"], size=11.5)
            + pill(732, 32, info["status"], 96)
        )
        + narrow(
            text(100, 43, fit(repo["name"], 480, 26), size=26, weight=700)
            + pill(604, 32, info["status"], 222, 20)
        )
    )
    return svg(theme, 64, body, f"{code} {repo['name']}: {repo['description'] or ''}", delay=delay)


DAILY_COLUMNS = [(16, "DATE"), (104, "SHOT"), (290, "NOTE"), (788, "DIFF")]
DAILY_NARROW = [(16, "TYPE"), (128, "NOTE")]


def daily_card(theme: str, daily: dict, delay: float = 0) -> str:
    """Return the card of one daily: date, shot, note and change size."""
    code, repo, kind, note = daily["code"], daily["repo"], daily["kind"], daily["note"]
    adds, dels = daily["additions"], daily["deletions"]
    blocks = "".join(
        f'<rect x="{786 + i * 8}" y="27" width="6" height="6" rx="1" style="fill:var(--{c})"/>'
        for i, c in enumerate(diffstat(adds, dels))
    )
    shot = (text(104, 27, code, "m", 10.5) if code else "") + text(
        104 + (len(code) + 1) * 10.5 * CHAR if code else 104, 27, fit(repo, 180 - (len(code) + 1) * 6.3 if code else 180, 12),
        size=12, weight=700,
    )
    body = (
        wide(
            text(16, 27, daily["date"], size=11.5)
            + shot
            + pill(290, 22, kind, 48)
            + text(346, 27, fit(note, 420, 12), size=12)
            + f'<text x="824" y="21" font-size="10" text-anchor="end">'
            f'<tspan style="fill:var(--ok)">+{compact(adds)}</tspan> <tspan style="fill:var(--fix)">−{compact(dels)}</tspan></text>'
            + blocks
        )
        + narrow(pill(16, 22, kind, 96, 17) + text(128, 31, fit(note, 696, 24), size=24))
    )
    return svg(theme, 44, body, f"{daily['date']} {repo}: {note} (+{adds} −{dels})", delay=delay)


###### Slate


def waveform(days: list[dict]) -> tuple[str, int]:
    """Return daily contributions drawn as an audio waveform, and their total.

    Bars are mirrored around a centre line, the way an editing timeline shows
    a sound track. From the clap's impact, as on a real slate, a playhead
    sweeps it on a loop, lighting the bars it has played, and rests on today.
    """
    x0, width, mid, half = 22, WIDTH - 44, 350, 18
    step = width / len(days)
    peak = max((d["contributionCount"] for d in days), default=0) or 1
    bars = []
    for i, day in enumerate(days):
        amp = max(half * (day["contributionCount"] / peak) ** 0.5, 0.75)
        bars.append(
            f'<rect x="{x0 + i * step + step * 0.2:.1f}" y="{mid - amp:.1f}" width="{step * 0.6:.1f}" '
            f'height="{amp * 2:.1f}" rx="{min(step * 0.3, 1.5):.1f}"/>'
        )
    bars = "".join(bars)
    play = f'begin="{IMPACT}s" dur="{PLAY_SECONDS}s" repeatCount="indefinite" keyTimes="0;.85;1" calcMode="spline" keySplines=".4 0 .6 1;0 0 1 1"'
    head = x0 + width
    parts = (
        f'<line x1="{x0}" y1="{mid}" x2="{head}" y2="{mid}" class="ln"/>'
        f'<clipPath id="played"><rect x="{x0}" y="{mid - half - 1}" width="0" height="{2 * half + 2}">'
        f'<animate attributeName="width" values="0;{width};{width}" {play}/></rect></clipPath>'
        f'<g class="ink" opacity=".25">{bars}</g>'
        f'<g class="ink" opacity=".8" clip-path="url(#played)">{bars}</g>'
        f'<g transform="translate({-width} 0)"><animateTransform attributeName="transform" type="translate" values="{-width} 0;0 0;0 0" {play}/>'
        f'<line x1="{head}" y1="{mid - half - 2}" x2="{head}" y2="{mid + half + 4}" stroke="#e8590c" stroke-width="2"/>'
        f'<path d="M{head - 5},{mid - half - 8} h10 l-5,6 z" fill="#e8590c" style="fill:#e8590c"/></g>'
    )
    return parts, sum(d["contributionCount"] for d in days)


###### Main


def write(theme: str, name: str, content: str) -> None:
    """Write one themed SVG."""
    (OUT / theme / name).write_text(content, encoding="utf-8")


def picture(name: str, alt: str, align: str = ' align="right"') -> str:
    """Return a ``<picture>`` that shows the dark or light image by GitHub theme."""
    return (
        f'<picture><source media="(prefers-color-scheme: dark)" srcset="slate/dark/{name}"/>'
        f'<img src="slate/light/{name}" width="100%"{align} alt="{escape(alt)}"/></picture>'
    )


def main() -> None:
    """Fetch the data and write the README, the slate and the cards."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set.")
    user = fetch(token)
    pinned = [r for r in user["pinnedItems"]["nodes"] if r and not r["isPrivate"]]
    recent = [r for r in user["repositories"]["nodes"] if r["name"] != USER]
    today = dt.datetime.now(dt.timezone.utc).date()
    codes = {repo["name"]: f"SH{i * 10:03d}" for i, repo in enumerate(pinned, start=1)}

    # Start clean: shots and dailies come and go with pins and commits.
    for theme in THEMES:
        shutil.rmtree(OUT / theme, ignore_errors=True)
        (OUT / theme).mkdir(parents=True)

    ###### Shots
    counts = {"approved": 0, "wip": 0, "hold": 0}
    shots = []
    for row, repo in enumerate(pinned, start=1):
        versions = versions_of(repo)
        pushed = dt.date.fromisoformat((repo["pushedAt"] or "1970-01-01")[:10])  # null: never pushed
        status = status_of(versions[0] if versions else None, pushed, repo["isArchived"], today)
        counts[status] += 1
        info = {
            "frames": f"{FIRST_FRAME}-{FIRST_FRAME + max(history_of(repo)['totalCount'], 1) - 1}",
            "versions": versions,
            "rt": duration(render_time(token, repo)),
            "status": status,
        }
        shots.append((row, repo, info))
    frames = sum(max(history_of(r)["totalCount"], 1) for r in pinned)
    closing = (
        f"{len(pinned)} SHOTS · {frames:,} FRAMES · {counts['approved']} APPROVED · "
        f"{counts['wip']} IN PROGRESS · {counts['hold']} ON HOLD"
    )
    short = f"{len(pinned)} SHOTS · {counts['approved']} OK · {counts['wip']} WIP · {counts['hold']} HOLD"
    shots_end = (len(pinned) + 1) * ROW_STEP

    ###### Dailies
    picks = [
        (commit, repo)
        for repo in recent
        for commit in [c for c in history_of(repo)["nodes"] if not NOISE.search(c["messageHeadline"])][:DAILIES_PER_REPO]
    ]
    picks.sort(key=lambda pick: pick[0]["committedDate"], reverse=True)
    dailies = []
    for commit, repo in picks[:DAILIES_COUNT]:
        kind, note = note_of(commit["messageHeadline"])
        additions, deletions = change_size(token, repo["name"], commit["oid"])
        dailies.append({
            "date": commit["committedDate"][:10], "code": codes.get(repo["name"], ""), "repo": repo["name"],
            "kind": kind, "note": note, "url": commit["url"],
            "additions": additions, "deletions": deletions,
        })
    shown = len(dailies)
    dailies_end = shots_end + (shown + 2) * ROW_STEP

    ###### Slate
    days = [d for w in user["contributionsCollection"]["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    wave, total = waveform(days[-WAVE_DAYS:])
    last_take = max((r["pushedAt"][:10] for r in recent if r["pushedAt"]), default=str(today))
    template = (OUT / "slate.template.svg").read_text(encoding="utf-8")
    fills = {
        "{{COMMON}}": COMMON_CSS,
        "{{DATE}}": last_take,
        "{{WAVE}}": wave,
        "{{WAVE_LABEL}}": f"SOUND · LAST {WAVE_DAYS} DAYS · {total:,} CONTRIBUTIONS",
        "{{WAVE_LABEL_NARROW}}": f"SOUND · {WAVE_DAYS} DAYS",
        "{{TC_WIDE}}": timecode(732, 318, 13, "w"),
        "{{TC_NARROW}}": timecode(673, 323, 22, "n"),
    }

    ###### Write both themes
    for theme in THEMES:
        slate = template.replace("{{PALETTE}}", palette(theme))
        for key, value in fills.items():
            slate = slate.replace(key, value)
        write(theme, "slate.svg", slate)
        write(theme, "shots-header.svg", header(theme, "SHOTS", "pinned repositories", SHOT_COLUMNS, SHOT_NARROW))
        for row, repo, info in shots:
            code = codes[repo["name"]]
            write(theme, f"shot-{code}.svg", shot_card(theme, code, repo, info, row * ROW_STEP))
        write(theme, "shots-footer.svg", footer(theme, closing, short, status_bar(counts), shots_end))
        write(
            theme,
            "dailies-header.svg",
            header(theme, "DAILIES", "latest work across public repositories", DAILY_COLUMNS, DAILY_NARROW, shots_end + ROW_STEP),
        )
        for index, daily in enumerate(dailies, start=1):
            write(theme, f"daily-{index:02d}.svg", daily_card(theme, daily, shots_end + (index + 1) * ROW_STEP))
        write(
            theme,
            "dailies-footer.svg",
            footer(theme, f"{shown} OF {len(picks)} RECENT CHANGES · RENDERED {today}", f"{shown} OF {len(picks)} CHANGES", delay=dailies_end),
        )

    ###### README
    # align="right" floats the images: floats stack with no gap (inline images
    # get one under each), and GitHub's 20px float padding lands off the left edge.
    def block(name: str, links: list[tuple[str, str, str]]) -> str:
        rows = [picture(f"{name}-header.svg", "")]
        rows += [f'<a href="{url}">{picture(file, alt)}</a>' for url, file, alt in links]
        rows += [picture(f"{name}-footer.svg", "")]
        return "<p>\n" + "\n".join(rows) + "\n</p>"

    shot_links = [(repo["url"], f"shot-{codes[repo['name']]}.svg", f"{codes[repo['name']]} {repo['name']}") for _, repo, _ in shots]
    daily_links = [(d["url"], f"daily-{i:02d}.svg", f"{d['repo']}: {d['note']}") for i, d in enumerate(dailies, start=1)]
    readme = (ROOT / "README.template.md").read_text(encoding="utf-8")
    readme = readme.replace(
        "{{SLATE}}", picture("slate.svg", "GitHub slate — Valentin Beaumont, Head of Pipeline at Lotchi, Paris", "")
    )
    readme = readme.replace("{{SHOTS}}", block("shots", shot_links))
    readme = readme.replace("{{DAILIES}}", block("dailies", daily_links))
    (ROOT / "README.md").write_text(readme, encoding="utf-8")


def self_test() -> None:
    """Check the pure helpers."""
    today = dt.date(2026, 11, 20)
    assert version_of("fxgui-15.4.3") == "v15.4.3" and version_of(None) is None
    repo = {
        "releases": {"nodes": [{"tagName": "v3.0.0-rc1", "isPrerelease": True}, {"tagName": "v2.31.0", "isPrerelease": False},
                               {"tagName": "v2.30.0", "isPrerelease": False}]},
        "refs": {"nodes": [{"name": "v9"}]},
    }
    assert versions_of(repo) == ["v2.31.0", "v2.30.0"]
    assert versions_of({"releases": {"nodes": []}, "refs": {"nodes": [{"name": "v0.5.0"}, {"name": "v0.5.0"}]}}) == ["v0.5.0"]
    assert status_of("v2.31.0", today, False, today) == "approved"
    assert status_of("v0.5.0", today, False, today) == "wip"
    assert status_of("v2.0.0", dt.date(2025, 1, 9), False, today) == "hold"
    assert status_of("v2.0.0", today, True, today) == "hold"
    for noise in ("chore: x", "[DOC] CHANGELOG", "docs(api): x", "Merge pull request #1", "Sponsor button: x"):
        assert NOISE.search(noise), noise
    for work in ("feat(ui)!: x", "[FIX] x", "Debug views are opaque", "fix(server): x"):
        assert not NOISE.search(work), work
    assert note_of("fix(parms): a ramp is kept") == ("fix", "A ramp is kept")
    assert note_of("[FEAT] FXViewFooter: cards") == ("feat", "FXViewFooter: cards")
    assert note_of("Debug views are opaque") == ("note", "Debug views are opaque")
    assert fit("abcdef", 4 * 12 * CHAR, 12) == "abc…" and fit("abc", 100, 12) == "abc"
    assert wrap("one two three four", 9 * 10 * CHAR, 10) == ["one two", "three fo…"]
    assert duration(134) == "02:14" and duration(None) == "—"
    assert compact(999) == "999" and compact(1200) == "1.2k" and compact(3000) == "3k"
    assert diffstat(120, 34) == ["ok"] * 4 + ["fix"] and diffstat(0, 0) == ["hold"] * 5
    assert diffstat(0, 10) == ["fix"] * 5
    assert "&lt;b&gt;" in text(0, 0, "<b>")
    tc = timecode(0, 0, 10, "t")
    assert tc.count('class="tc"') == 5 and "steps(24)" in tc and "@keyframes tc24" in tc
    for theme in THEMES:
        assert f"--bg:{PALETTES[theme]['bg']}" in palette(theme)
    print("self-test passed")


if __name__ == "__main__":
    self_test() if "--self-test" in sys.argv else main()
