#!/usr/bin/env python3
"""Combine curated highlights with remaining PR links grouped by contributor."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SECTIONS = ["## Major new features", "## Breaking changes", "## Bug fixes"]


def _pr_link(pr: int, repo: str) -> str:
    return f"[#{pr}](https://github.com/{repo}/pull/{pr})"


def _author_link(credit: dict) -> str:
    author = credit["author"]
    return f"[@{author}]({credit['author_url']})" if author else "Author unavailable"


def _highlights(raw: str, credits: list[dict], repo: str) -> tuple[str, set[int]]:
    match = re.search(
        r"<!--\s*RELEASE_NOTES\s*-->(.*?)<!--\s*/RELEASE_NOTES\s*-->", raw, re.DOTALL
    )
    highlights = match.group(1).strip() if match else ""
    headings = re.findall(r"(?m)^## .+$", highlights)
    if not headings or headings != [section for section in SECTIONS if section in headings]:
        return "", set()
    by_pr = {credit["pr"]: credit for credit in credits}
    cited: set[int] = set()
    lines = []
    for line in highlights.splitlines():
        if not line.strip() or line in SECTIONS:
            lines.append(line)
            continue
        bullet = re.fullmatch(r"(- .+?)\s+\(([^()]*)\)\s*", line)
        prs = (
            list(dict.fromkeys(int(pr) for pr in re.findall(r"#(\d+)\b", bullet[2])))
            if bullet
            else []
        )
        if not prs or not set(prs) <= by_pr.keys():
            return "", set()
        # Credits come from GitHub metadata, even if the model omits or guesses handles.
        refs = [_pr_link(pr, repo) for pr in prs]
        refs.extend(dict.fromkeys(_author_link(by_pr[pr]) for pr in prs if by_pr[pr]["author"]))
        lines.append(f"{bullet[1]} ({', '.join(refs)})")
        cited.update(prs)
    return ("\n".join(lines), cited) if cited else ("", set())


def compose_notes(raw: str, credits: list[dict], repo: str) -> str:
    highlights, cited = _highlights(raw, credits, repo)
    groups: dict[str, list[dict]] = {}
    for credit in sorted(credits, key=lambda credit: credit["pr"]):
        if credit["pr"] not in cited:
            groups.setdefault(credit["author"], []).append(credit)
    lines = [highlights, ""] if highlights else []
    if groups:
        lines.extend(["## Other contributions", ""])
        contributions = []
        for author in sorted(groups, key=str.casefold):
            group = groups[author]
            refs = ", ".join(_pr_link(credit["pr"], repo) for credit in group)
            contributions.append(f"{refs}, {_author_link(group[0])}")
        lines.extend(["; ".join(contributions), ""])
    lines.append(f"Full Changelog: https://github.com/{repo}/blob/main/CHANGELOG.md")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--highlights", required=True, type=Path)
    parser.add_argument("--credits", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    raw = args.highlights.read_text(encoding="utf-8") if args.highlights.is_file() else ""
    credits = json.loads(args.credits.read_text(encoding="utf-8"))
    args.out.write_text(compose_notes(raw, credits, args.repo), encoding="utf-8")


if __name__ == "__main__":
    main()
