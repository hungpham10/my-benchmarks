"""Publish blog/post.md to a WordPress blog through the REST API.

I cannot reach the network from the sandbox where this was written, so this
script exists to be run by you, with your credentials, on your machine. Nothing
is hardcoded and nothing is committed: the token is read from the environment or
prompted for, never stored.

    # 1. Preview. Writes HTML next to the post and exits. No network.
    #    Uses the `markdown` package if installed, otherwise a built-in
    #    renderer that covers what this post uses (headings, tables, fenced
    #    code, lists, links, emphasis). No pip install required.
    python3 blog/publish_wordpress.py --dry-run

    # 3. Publish as a DRAFT and print the edit link. Recommended first move.
    python3 blog/publish_wordpress.py --draft

    # 4. Publish live.
    python3 blog/publish_wordpress.py

Credentials: create an Application Password at
https://wordpress.com/settings/application-passwords , then

    export WP_APP_PASSWORD='xxxx xxxx xxxx xxxx xxxx xxxx'
    export WP_USER='your-wordpress-com-username'

Note that WordPress.com strips most raw HTML on the free plan. If the tables
come back mangled or empty after publishing, open the draft, paste the content
by hand, and keep the draft URL. The tables are the point of this post, so
check them before it goes live.
"""

import argparse
import base64
import getpass
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

SITE = os.environ.get("WP_SITE", "hungpham10.wordpress.com")
ROOT = pathlib.Path(__file__).resolve().parent.parent
POST_PATH = ROOT / "blog" / "post.md"
HTML_PATH = ROOT / "blog" / "post.html"


def inline(text):
    """Inline Markdown -> HTML. Code spans first so their contents are inert."""
    stash = []

    def keep(match):
        stash.append(match.group(1))
        return "\x00%d\x00" % (len(stash) - 1)

    text = re.sub(r"`([^`]+)`", keep, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<em>\1</em>", text)
    for index, code in enumerate(stash):
        text = text.replace("\x00%d\x00" % index, "<code>%s</code>" % code)
    return text


def fallback_html(markdown_text):
    """Dependency-free renderer covering exactly what blog/post.md uses.

    Exists so publishing never depends on pip being reachable. Not a general
    Markdown implementation: nested lists, reference links and setext headings
    are not supported and are not used in the post.
    """
    import html as htmllib

    out, lines, index = [], markdown_text.splitlines(), 0
    paragraph, list_mode = [], None

    def flush_paragraph():
        if paragraph:
            out.append("<p>%s</p>" % inline(" ".join(paragraph).strip()))
            paragraph.clear()

    def close_list():
        nonlocal list_mode
        if list_mode:
            out.append("</%s>" % list_mode)
            list_mode = None

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            close_list()

        elif stripped.startswith("```"):
            flush_paragraph()
            close_list()
            index += 1
            code = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            body = htmllib.escape("\n".join(code))
            out.append('<pre style="background:#f6f8fa;padding:12px;overflow:auto;'
                       'font-size:90%%"><code>%s</code></pre>' % body)

        elif stripped.startswith("|"):
            flush_paragraph()
            close_list()
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip())
                index += 1
            index -= 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            cells = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
            if cells:
                out.append('<table style="border-collapse:collapse;width:100%%">')
                out.append("<thead><tr>" + "".join(
                    "<th style='border:1px solid #ddd;padding:6px;text-align:left'>%s</th>"
                    % inline(c) for c in cells[0]) + "</tr></thead><tbody>")
                for row in cells[1:]:
                    out.append("<tr>" + "".join(
                        "<td style='border:1px solid #ddd;padding:6px'>%s</td>" % inline(c)
                        for c in row) + "</tr>")
                out.append("</tbody></table>")

        elif stripped.startswith("#"):
            flush_paragraph()
            close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append("<h%d>%s</h%d>" % (level, inline(stripped[level:].strip()), level))

        elif re.match(r"^[-*] ", stripped):
            flush_paragraph()
            if list_mode != "ul":
                close_list()
                out.append("<ul>")
                list_mode = "ul"
            out.append("<li>%s</li>" % inline(stripped[2:]))

        elif re.match(r"^\d+\. ", stripped):
            flush_paragraph()
            if list_mode != "ol":
                close_list()
                out.append("<ol>")
                list_mode = "ol"
            out.append("<li>%s</li>" % inline(re.sub(r"^\d+\. ", "", stripped)))

        elif stripped.startswith(">"):
            flush_paragraph()
            close_list()
            quoted = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(re.sub(r"^>\s?", "", lines[index].strip()))
                index += 1
            index -= 1
            out.append("<blockquote><p>%s</p></blockquote>"
                       % inline(" ".join(q.strip() for q in quoted).strip()))

        elif stripped.startswith("<!--"):
            while index < len(lines) and "-->" not in lines[index]:
                index += 1

        elif stripped == "---":
            flush_paragraph()
            close_list()
            out.append("<hr />")

        else:
            close_list()
            paragraph.append(stripped)

        index += 1

    flush_paragraph()
    close_list()
    return "\n".join(out)


def to_html(markdown_text):
    """Markdown -> HTML. Prefers the `markdown` package, falls back to built-in."""
    try:
        import markdown
    except ImportError:
        return fallback_html(markdown_text)

    md = markdown.Markdown(
        extensions=["extra", "tables", "fenced_code", "codehilite", "sane_lists"],
        extension_configs={"codehilite": {"guess_lang": False}},
    )
    return md.convert(markdown_text)


def title_from(markdown_text):
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled"


def slug_from(title):
    keep = [c.lower() if c.isalnum() else "-" for c in title]
    return "".join(keep).strip("-")[:70]


def post_json(args, html, title):
    payload = {
        "title": title,
        "content": html,
        "status": "draft" if args.draft else "publish",
        "slug": args.slug or slug_from(title),
    }
    if args.tags:
        payload["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.categories:
        payload["categories"] = [int(c.strip()) for c in args.categories.split(",") if c.strip()]
    return json.dumps(payload).encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="render HTML to blog/post.html and exit, no network")
    parser.add_argument("--draft", action="store_true",
                        help="publish as a draft instead of live")
    parser.add_argument("--slug", help="override the post slug")
    parser.add_argument("--tags", help="comma-separated tag names (needs the API to resolve them)")
    parser.add_argument("--categories", help="comma-separated category ids")
    parser.add_argument("--site", default=SITE, help="default: %s" % SITE)
    args = parser.parse_args()

    markdown_text = POST_PATH.read_text()
    html = to_html(markdown_text)
    title = title_from(markdown_text)

    HTML_PATH.write_text(html)
    print("rendered %s (%d bytes of HTML)" % (HTML_PATH.relative_to(ROOT), len(html)))
    print("title: %s" % title)

    if args.dry_run:
        print("\n--dry-run: not publishing. Open %s in a browser to check the tables."
              % HTML_PATH.relative_to(ROOT))
        return 0

    user = os.environ.get("WP_USER") or input("WordPress.com username: ").strip()
    token = os.environ.get("WP_APP_PASSWORD") or getpass.getpass(
        "Application password (from wordpress.com/settings/application-passwords): ")
    if not user or not token:
        sys.exit("username and application password are both required")

    url = "https://public-api.wordpress.com/wp/v2/sites/%s/posts" % args.site
    body = post_json(args, html, title)
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": "Basic " + base64.b64encode(
                ("%s:%s" % (user, token)).encode()).decode(),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "prom-vs-pg-bench/1.0",
        },
    )

    print("\nposting to %s ..." % url)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        sys.exit("HTTP %s\n%s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        sys.exit("network error: %s" % exc.reason)

    print("status: %s" % result.get("status"))
    print("edit:   %s" % result.get("link", "(not published yet)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
