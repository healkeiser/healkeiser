"""Render the profile README, slate and tracker cards from live GitHub data.

Reads ``README.template.md`` and ``slate/slate.template.svg``, fills their
``{{...}}`` placeholders, and writes ``README.md`` plus the SVGs in ``slate/``:
the slate, one card per shot and one card per daily. Each card is its own
image so each can carry its own link on GitHub.

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
import sys
import textwrap
import urllib.error
import urllib.request
from html import escape
from pathlib import Path


###### Constants

USER = "healkeiser"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "slate"
WIDTH = 840
FIRST_FRAME = 1001
HOLD_AFTER_DAYS = 180
WAVE_DAYS = 120
IMPACT = 1.07  # seconds: when the slate's arm lands (see slate.template.svg)
PLAY_SECONDS = 9  # one pass of the waveform playhead
ROW_STEP = 0.07  # seconds between rows fading in
DAILIES_COUNT = 5
DAILIES_PER_REPO = 2
DAILIES_SCAN = 15  # recently pushed repos scanned for dailies
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

# Dark values first, light ones in the media query: the images follow the
# viewer's system theme, as GitHub's <picture> sources do.
STYLE = f"""<style>
:root{{--bg:#0d1117;--panel:#161b22;--ink:#e6edf3;--line:#30363d;--mute:#8b949e;
--ok:#3fb950;--okb:#1f6f3a55;--wip:#d29922;--wipb:#9e6a0355;--hold:#8b949e;--holdb:#30363d88;--fix:#f85149;--fixb:#da363355}}
@media (prefers-color-scheme: light){{:root{{--bg:#ffffff;--panel:#f6f8fa;--ink:#1f2328;--line:#d0d7de;--mute:#59636e;
--ok:#1a7f37;--okb:#dafbe1;--wip:#9a6700;--wipb:#fff8c5;--hold:#59636e;--holdb:#eaeef2;--fix:#cf222e;--fixb:#ffebe9}}}}
text{{font-family:{MONO};fill:var(--ink)}}
.k{{fill:var(--mute);font-size:10px;letter-spacing:1.5px}}
.m{{fill:var(--mute)}} .ln{{stroke:var(--line);stroke-width:1.5;fill:none}}
.bg{{fill:var(--bg)}} .panel{{fill:var(--panel)}} .ink{{fill:var(--ink)}}
.in{{animation:rise .6s cubic-bezier(.2,.8,.2,1) both}}
@keyframes rise{{from{{opacity:0;transform:translateY(10px)}}}}
.pulse{{animation:pulse 1.6s ease-in-out infinite}}
@keyframes pulse{{50%{{opacity:.2}}}}
@media (prefers-reduced-motion: reduce){{.in,.pulse{{animation:none}}}}
</style>"""

PILLS = {  # status: (label, text colour, fill colour)
    "approved": ("APPROVED", "--ok", "--okb"),
    "wip": ("IN PROGRESS", "--wip", "--wipb"),
    "hold": ("ON HOLD", "--hold", "--holdb"),
    "feat": ("FEAT", "--ok", "--okb"),
    "fix": ("FIX", "--fix", "--fixb"),
    "note": ("NOTE", "--hold", "--holdb"),
}

QUERY = """
query($login: String!, $dailies: Int!) {
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
  latestRelease { tagName }
  refs(refPrefix: "refs/tags/", first: 1,
       orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) { nodes { name } }
  defaultBranchRef { name target { ... on Commit {
    history(first: 100) { totalCount nodes { messageHeadline committedDate url } }
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


###### GitHub


def request(url: str, token: str, body: dict | None = None) -> dict:
    """Send a GitHub API request and decode its JSON."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"bearer {token}", "User-Agent": "profile-readme"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def fetch(token: str) -> dict:
    """Return the ``user`` object of the GraphQL query."""
    payload = request(
        "https://api.github.com/graphql",
        token,
        {"query": QUERY, "variables": {"login": USER, "dailies": DAILIES_SCAN}},
    )
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
    except urllib.error.HTTPError:
        return None
    if not runs:
        return None
    start = dt.datetime.fromisoformat(runs[0]["run_started_at"].replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(runs[0]["updated_at"].replace("Z", "+00:00"))
    return (end - start).total_seconds()


def history_of(repo: dict) -> dict:
    """Return the default-branch history block, empty when there are no commits."""
    target = (repo.get("defaultBranchRef") or {}).get("target") or {}
    return target.get("history") or {"totalCount": 0, "nodes": []}


###### SVG


def svg(
    height: int, body: str, label: str, part: str = "row", top: int = 0, bottom: int = 0, delay: float = 0
) -> str:
    """Wrap one piece of a framed table in a themed SVG document.

    A table is a ``head`` (rounded top corners), ``row`` pieces and a ``foot``
    (rounded bottom corners), each its own image so each row can carry its own
    link. The README floats them (``align="right"``), which stacks them with no
    gap, so the pieces join into one frame.

    Args:
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
        f'viewBox="0 0 {w} {total}" role="img" aria-label="{escape(label)}">{STYLE}'
        f'<g transform="translate(0 {top})"><g class="in" style="animation-delay:{IMPACT + delay:.2f}s">'
        f'<path d="{fill} Z" class="{"panel" if part == "foot" else "bg"}"/>'
        f'{body}{rule}<path d="{outline}" class="ln"/></g></g></svg>'
    )


def text(x: float, y: float, content: str, cls: str = "", size: float = 12, weight: int = 400) -> str:
    """Return an escaped SVG text element."""
    klass = f' class="{cls}"' if cls else ""
    return f'<text x="{x}" y="{y}"{klass} font-size="{size}" font-weight="{weight}">{escape(content)}</text>'


def pill(x: float, y: float, kind: str, width: float | None = None) -> str:
    """Return a status pill; ``y`` is its vertical centre."""
    label, ink, fill = PILLS[kind]
    width = width or len(label) * 10 * CHAR + 18
    live = kind == "wip"  # work in progress gets a pulsing dot, like a recording light
    return (
        f'<rect x="{x}" y="{y - 10}" width="{width}" height="20" rx="10" style="fill:var({fill})"/>'
        + (f'<circle class="pulse" cx="{x + 9}" cy="{y}" r="2.5" style="fill:var({ink})"/>' if live else "")
        + f'<text x="{x + width / 2 + (6 if live else 0)}" y="{y + 3.5}" font-size="10" text-anchor="middle" '
        f'letter-spacing="1" style="fill:var({ink})">{label}</text>'
    )


def header(title: str, summary: str, columns: list[tuple[float, str]], delay: float = 0) -> str:
    """Return a table head: a title bar and the column labels."""
    a, r, w = 0.75, 10, WIDTH
    bar = f"M{a},40 V{r + a} A{r},{r} 0 0 1 {r + a},{a} H{w - r - a} A{r},{r} 0 0 1 {w - a},{r + a} V40 Z"
    body = (
        f'<path d="{bar}" class="panel"/><line x1="0" y1="40" x2="{w}" y2="40" class="ln"/>'
        + text(16, 25, title, size=13, weight=700)
        + text(16 + (len(title) + 2) * 13 * CHAR, 25, summary, "m", 11)
        + "".join(text(x, 61, label, "k") for x, label in columns)
    )
    return svg(74, body, f"{title}: {summary}", "head", top=16, delay=delay)


def footer(left: str, right: str = "", delay: float = 0) -> str:
    """Return a table foot: a closing bar with a summary on the left."""
    return svg(40, text(16, 25, left, "k") + right, left, "foot", bottom=16, delay=delay)


def status_bar(counts: dict[str, int]) -> str:
    """Return a segmented bar of shot statuses for the right of a footer."""
    x, end, total = 600, WIDTH - 16, sum(counts.values()) or 1
    parts = []
    for kind, count in counts.items():
        if count:
            width = (end - x) * count / total
            parts.append(
                f'<rect x="{x:.1f}" y="17" width="{max(width - 3, 2):.1f}" height="6" rx="3" '
                f'style="fill:var({PILLS[kind][1]})"/>'
            )
            x += width
    return "".join(parts)


SHOT_COLUMNS = [(100, "SHOT"), (290, "DESCRIPTION"), (548, "FRAMES"), (628, "VER"), (692, "RT"), (742, "STATUS")]


def shot_card(code: str, repo: dict, info: dict, delay: float = 0) -> str:
    """Return the card of one shot."""
    language = repo.get("primaryLanguage") or {}
    color = language.get("color") or "#8b949e"
    white = 'fill="#fff" style="fill:#fff"'
    lines = wrap(repo["description"] or "", 240, 11.5)
    body = (
        # Thumbnail: a plate graded from the language colour, with the shot number.
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{color}"/><stop offset="1" stop-color="{color}" stop-opacity=".25"/></linearGradient></defs>'
        f'<rect x="14" y="10" width="72" height="44" rx="4" fill="#1b1f24" style="fill:#1b1f24"/>'
        f'<rect x="14" y="10" width="72" height="44" rx="4" fill="url(#g)"/>'
        f'<text x="50" y="33" font-size="14" font-weight="700" text-anchor="middle" {white}>{escape(code[2:])}</text>'
        f'<text x="50" y="47" font-size="8.5" text-anchor="middle" letter-spacing=".5" opacity=".85" {white}>'
        f'{escape(fit(language.get("name", "").upper(), 66, 8.5))}</text>'
        + text(100, 27, code, "m", 10.5)
        + text(100, 45, fit(repo["name"], 180, 13), size=13, weight=700)
        + "".join(text(290, 28 + i * 16 - (8 if len(lines) == 1 else 0), line, size=11.5) for i, line in enumerate(lines))
        + text(548, 37, info["frames"], size=11.5)
        + text(628, 37, fit(info["version"], 60, 11.5), size=11.5)
        + text(692, 37, info["rt"], size=11.5)
        + pill(742, 32, info["status"], 86)
    )
    return svg(64, body, f"{code} {repo['name']}: {repo['description'] or ''}", delay=delay)


DAILY_COLUMNS = [(16, "DATE"), (120, "SHOT"), (360, "NOTE")]


def daily_card(date: str, code: str, repo: str, kind: str, note: str, delay: float = 0) -> str:
    """Return the card of one daily."""
    body = (
        text(16, 27, date, size=11.5)
        + text(120, 27, code, "m", 10.5)
        + text(120 + (len(code) + 1) * 10.5 * CHAR if code else 120, 27, fit(repo, 200 if not code else 160, 12), size=12, weight=700)
        + pill(360, 22, kind, 48)
        + text(420, 27, fit(note, WIDTH - 436, 12), size=12)
    )
    return svg(44, body, f"{date} {repo}: {note}", delay=delay)


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

    for old in list(OUT.glob("shot-*.svg")) + list(OUT.glob("daily-*.svg")):
        old.unlink()

    ###### Shots
    counts = {"approved": 0, "wip": 0, "hold": 0}
    shot_links = []
    for row, repo in enumerate(pinned, start=1):
        tag = (repo.get("latestRelease") or {}).get("tagName")
        if not tag and repo["refs"]["nodes"]:
            tag = repo["refs"]["nodes"][0]["name"]
        version = version_of(tag)
        pushed = dt.date.fromisoformat(repo["pushedAt"][:10])
        status = status_of(version, pushed, repo["isArchived"], today)
        counts[status] += 1
        info = {
            "frames": f"{FIRST_FRAME}-{FIRST_FRAME + max(history_of(repo)['totalCount'], 1) - 1}",
            "version": version or "—",
            "rt": duration(render_time(token, repo)),
            "status": status,
        }
        code = codes[repo["name"]]
        (OUT / f"shot-{code}.svg").write_text(shot_card(code, repo, info, row * ROW_STEP), encoding="utf-8")
        shot_links.append((repo["url"], f"shot-{code}.svg", f"{code} {repo['name']}"))
    (OUT / "shots-header.svg").write_text(header("SHOTS", "pinned repositories", SHOT_COLUMNS), encoding="utf-8")
    frames = sum(max(history_of(r)["totalCount"], 1) for r in pinned)
    closing = (
        f"{len(pinned)} SHOTS · {frames:,} FRAMES · {counts['approved']} APPROVED · "
        f"{counts['wip']} IN PROGRESS · {counts['hold']} ON HOLD"
    )
    shots_end = (len(pinned) + 1) * ROW_STEP
    (OUT / "shots-footer.svg").write_text(footer(closing, status_bar(counts), shots_end), encoding="utf-8")

    ###### Dailies
    picks = [
        (commit, repo)
        for repo in recent
        for commit in [c for c in history_of(repo)["nodes"] if not NOISE.search(c["messageHeadline"])][:DAILIES_PER_REPO]
    ]
    picks.sort(key=lambda pick: pick[0]["committedDate"], reverse=True)
    daily_links = []
    for index, (commit, repo) in enumerate(picks[:DAILIES_COUNT], start=1):
        kind, note = note_of(commit["messageHeadline"])
        card = daily_card(
            commit["committedDate"][:10], codes.get(repo["name"], ""), repo["name"], kind, note,
            shots_end + (index + 1) * ROW_STEP,
        )
        (OUT / f"daily-{index:02d}.svg").write_text(card, encoding="utf-8")
        daily_links.append((commit["url"], f"daily-{index:02d}.svg", f"{repo['name']}: {note}"))
    (OUT / "dailies-header.svg").write_text(
        header("DAILIES", "latest work across public repositories", DAILY_COLUMNS, shots_end + ROW_STEP),
        encoding="utf-8",
    )
    closing = f"{min(len(picks), DAILIES_COUNT)} OF {len(picks)} RECENT CHANGES · RENDERED {today}"
    dailies_end = shots_end + (min(len(picks), DAILIES_COUNT) + 2) * ROW_STEP
    (OUT / "dailies-footer.svg").write_text(footer(closing, delay=dailies_end), encoding="utf-8")

    ###### Slate
    days = [d for w in user["contributionsCollection"]["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    wave, total = waveform(days[-WAVE_DAYS:])
    last_take = max((r["pushedAt"][:10] for r in recent), default=str(today))
    slate = (OUT / "slate.template.svg").read_text(encoding="utf-8")
    slate = slate.replace("{{DATE}}", last_take).replace("{{WAVE}}", wave)
    slate = slate.replace("{{WAVE_LABEL}}", f"SOUND · LAST {WAVE_DAYS} DAYS · {total:,} CONTRIBUTIONS")
    (OUT / "slate.svg").write_text(slate, encoding="utf-8")

    ###### README
    # align="right" floats the images: floats stack with no gap (inline images
    # get one under each), and GitHub's 20px float padding lands off the left edge.
    def block(name: str, links: list[tuple[str, str, str]]) -> str:
        image = '<img src="slate/{}" width="100%" align="right" alt="{}"/>'
        rows = [image.format(f"{name}-header.svg", "")]
        rows += [f'<a href="{url}">{image.format(file, escape(alt))}</a>' for url, file, alt in links]
        rows += [image.format(f"{name}-footer.svg", "")]
        return "<p>\n" + "\n".join(rows) + "\n</p>"

    readme = (ROOT / "README.template.md").read_text(encoding="utf-8")
    readme = readme.replace("{{SHOTS}}", block("shots", shot_links))
    readme = readme.replace("{{DAILIES}}", block("dailies", daily_links))
    (ROOT / "README.md").write_text(readme, encoding="utf-8")


def self_test() -> None:
    """Check the pure helpers."""
    today = dt.date(2026, 11, 20)
    assert version_of("fxgui-15.4.3") == "v15.4.3" and version_of(None) is None
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
    assert "&lt;b&gt;" in text(0, 0, "<b>")
    print("self-test passed")


if __name__ == "__main__":
    self_test() if "--self-test" in sys.argv else main()
