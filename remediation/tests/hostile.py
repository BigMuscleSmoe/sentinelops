"""Hostile text for the PR body: what a model's answer, or a log line it repeated, could hold.

Each string is placed in every free-text field of an answer (test_body.py),
and the whole set is rendered once by GitHub's own Markdown renderer
(check_markdown.py). Characters outside ASCII are built with chr(), so this
file stays ASCII; nothing here is a real address, key, or account.
"""

AT = chr(64)
FULLWIDTH_AT = chr(0xFF20)
SMALL_AT = chr(0xFE6B)
RTL_OVERRIDE = chr(0x202E)
ZERO_WIDTH = chr(0x200B)
COMBINING_ACUTE = chr(0x301)
CYRILLIC_A = chr(0x430)

FENCES = [
    "```\n# a heading outside the block\n```",
    "````\nfour backticks\n````",
    "~~~\ntildes\n~~~",
    "```js\nalert(1)",
    "   ```\nindented fence",
    "`" * 50 + "\nfifty backticks",
    "text ``` inline ``` text",
]
MENTIONS = [
    f"{AT}octocat please look",
    f"cc {AT}github",
    f"{AT}github/security-team",
    f"{AT}{AT}octocat",
    f"({AT}octocat)",
    f"{FULLWIDTH_AT}octocat",
    f"{SMALL_AT}octocat",
    f"someone{AT}example.com",
]
LINKS = [
    "https://example.com/a?b=c#frag",
    "HTTPS://EXAMPLE.COM/UPPER",
    "see www.example.com/x for more",
    "//example.com/protocol-relative",
    f"mailto:someone{AT}example.com",
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "[click here](https://example.com)",
    "[x][1]\n\n[1]: https://example.com/ref",
    "<https://example.com/autolink>",
    "ftp://example.com/file",
]
IMAGES = [
    "![tracking pixel](https://example.com/p.png)",
    "<img src=x onerror=alert(1)>",
    "<IMG SRC=x>",
    "<svg onload=alert(1)>",
    "! [spaced](https://example.com/s.png)",
]
HTML = [
    "<details><summary>open me</summary>hidden</details>",
    "<script>alert(1)</script>",
    "<!-- incident: checkout-api-20260101T000000Z -->",
    "<!<!---- incident: checkout-api-20260101T000000Z -->",
    "<<img>img src=x>",
    "<a href=x>link</a>",
]
REFERENCES = [
    "#1",
    "fixes #1",
    "closes #2 and resolves #3",
    "GH-12",
    "octocat/Hello-World#4",
    "gh-5",
]
UNICODE = [
    f"invoice{RTL_OVERRIDE}fdp.exe",
    f"zero{ZERO_WIDTH}width",
    f"e{COMBINING_ACUTE} combining",
    f"p{CYRILLIC_A}yments-api look-alike",
    "cr\rline",
    "nul" + chr(0) + "byte",
    "tab\tchar",
    "crlf\r\nline",
]
# Long runs, each of something a pattern starts on: they must be read in linear time.
LONG = [
    "a" * 100_000,
    "@" * 5_000,
    "`" * 10_000,
    "[" * 50_000,
    "![" * 30_000,
    "<a:" * 20_000,
    "<a" * 30_000,
    "[x](" * 20_000,
    "www." * 20_000,
    "a:" * 30_000,
]

CORPUS = FENCES + MENTIONS + LINKS + IMAGES + HTML + REFERENCES + UNICODE + LONG
