"""Split the raw High Sierra Topix forum scrape (threads.md) into
individual posts with clean metadata. This is the mechanical, structural
part of extraction -- post boundaries and headers follow a fixed phpBB
layout, so regex is reliable here. Place/severity extraction from the
body text is NOT done here (see NOTE below): that's genuinely ambiguous
natural language (parenthetical notes, word-numbers like "None (0)",
ranges, multi-day breakdowns) and is done as a separate LLM-assisted pass
over this script's output, not with more regex.
"""

import json
import re
from pathlib import Path

POST_HEADER_RE = re.compile(
    r"^Post by (?P<author>.+?) » (?P<date>\w{3} \w{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [ap]m)$"
)

YEAR_SECTION_RE = re.compile(r"^(20\d{2}) Mosquito Reports$")


def parse_posts(path: str):
    lines = Path(path).read_text(encoding="utf-8").splitlines()

    posts = []
    current_year = None
    i = 0
    post_id = 0

    while i < len(lines):
        line = lines[i].strip()

        m_year = YEAR_SECTION_RE.match(line)
        if m_year:
            current_year = int(m_year.group(1))

        m_header = POST_HEADER_RE.match(line)
        if m_header:
            # Walk backward from the "Post by X » date" line to recover the
            # subject line (the line immediately above) and, further back,
            # the metadata block (username/rank/posts/joined/experience/
            # location), stopping at the previous "Top" or "User avatar".
            subject = lines[i - 1].strip() if i > 0 else ""

            # Walk forward to collect the body, until the next "Top" line
            # (phpBB's per-post footer) or end of file.
            body_lines = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != "Top":
                body_lines.append(lines[j])
                j += 1
            body = "\n".join(body_lines).strip()

            posts.append(
                {
                    "post_id": post_id,
                    "year_section": current_year,
                    "author": m_header.group("author").strip(),
                    "post_date": m_header.group("date"),
                    "subject": subject,
                    "body": body,
                }
            )
            post_id += 1
            i = j
        i += 1

    return posts


if __name__ == "__main__":
    posts = parse_posts("/Users/maxwelltitsworth/mosquitoseverity/threads.md")
    print(f"{len(posts)} posts parsed")
    out_path = "/Users/maxwelltitsworth/mosquitoseverity/cache/parsed_posts.json"
    Path(out_path).write_text(json.dumps(posts, indent=2))
    print(f"Wrote {out_path}")
    for p in posts[:3]:
        print(json.dumps(p, indent=2))
