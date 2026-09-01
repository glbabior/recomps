"""Reducing a fetched page to the text worth reading.

A modern listing page is mostly not content. Scripts, inline styles, tracking
payloads and navigation chrome routinely account for nine tenths of the bytes,
and sending all of it to a model means paying to have the model ignore it.

Two things this does that a naive tag-strip does not:

**Keeps embedded JSON.** Listing sites put the actual structured data --
prices, dates, agent names -- in `application/ld+json` blocks and Next.js state
payloads. That is often cleaner and more complete than the rendered text, and
it is the part most worth preserving.

**Keeps the page's own boundaries.** Table cells and list items become
separated lines rather than one run-on paragraph, because a row of a sold-comps
table is a record and merging it into its neighbours destroys the thing being
extracted.

Implemented on the standard library's parser. A heavier HTML library would
parse more correctly, but this input is being read by a language model that
tolerates messy text, and a dependency that has to be installed before anyone
can try the tool is a real cost.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

#: Content of these elements is discarded entirely.
_DROP = {"script", "style", "noscript", "svg", "canvas", "template", "iframe"}
#: These end a line, so records do not merge into their neighbours.
_BREAK = {
    "p", "div", "br", "li", "tr", "td", "th", "section", "article", "header",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "dl",
    "dd", "dt", "figure", "figcaption", "main", "nav", "aside", "form",
}
#: Script types that carry structured data rather than code.
_DATA_SCRIPT_TYPES = {"application/ld+json", "application/json"}

_BLANK_RUN = re.compile(r"\n{3,}")
# Any run of horizontal whitespace, newlines excluded. Written as a
# negated class so the non-breaking spaces listing pages use for layout
# are collapsed too, without putting an invisible character in source.
_SPACE_RUN = re.compile(r"[^\S\r\n]{2,}")


class _Reducer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.data_blocks: list[str] = []
        self._drop_depth = 0
        self._capture_data: str | None = None
        self._title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            kind = dict(attrs).get("type", "").lower()
            if kind in _DATA_SCRIPT_TYPES:
                self._capture_data = ""
                return
        if tag == "title":
            self._in_title = True
        if tag in _DROP:
            self._drop_depth += 1
            return
        if tag in _BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capture_data is not None:
            block = self._capture_data.strip()
            if block:
                self.data_blocks.append(block)
            self._capture_data = None
            return
        if tag == "title":
            self._in_title = False
        if tag in _DROP and self._drop_depth:
            self._drop_depth -= 1
            return
        if tag in _BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._capture_data is not None:
            self._capture_data += data
            return
        if self._drop_depth:
            return
        if self._in_title:
            self._title += data
        text = data.strip()
        if text:
            self.parts.append(text + " ")


def reduce_html(
    html: str, max_chars: int = 120_000, keep_data_blocks: bool = True
) -> str:
    """Return the readable text of `html`, plus any embedded structured data.

    `max_chars` is a safety valve, not a target: a page that exceeds it is
    truncated with an explicit marker so the caller and the model both know the
    text is incomplete, rather than silently losing the tail.
    """
    reducer = _Reducer()
    try:
        reducer.feed(html)
        reducer.close()
    except Exception:
        # A page malformed enough to break the parser still has readable text.
        return re.sub(r"<[^>]+>", " ", html)[:max_chars]

    text = "".join(reducer.parts)
    text = _SPACE_RUN.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RUN.sub("\n\n", text).strip()

    sections = []
    if reducer._title.strip():
        sections.append(f"PAGE TITLE: {reducer._title.strip()}")
    sections.append(text)

    if keep_data_blocks and reducer.data_blocks:
        # Structured data last: it is the most useful part, and putting it at
        # the end keeps it inside the window when the body gets truncated.
        blocks = "\n\n".join(reducer.data_blocks)
        sections.append("EMBEDDED STRUCTURED DATA:\n" + blocks)

    combined = "\n\n".join(s for s in sections if s)
    if len(combined) > max_chars:
        return combined[:max_chars] + "\n\n[TRUNCATED - page exceeded the size limit]"
    return combined


#: Words that sit next to the facts a comp needs. Matching is loose on purpose:
#: sites phrase attribution as "Listed by", "Listing courtesy of", "Presented
#: by", and price history as a table, a timeline, or a paragraph.
DEFAULT_FOCUS_TERMS = (
    "listed by", "listing courtesy", "courtesy of", "presented by", "bought with",
    "sold by", "buyer's agent", "buyers agent", "listing agent", "listing provided",
    "dre #", "dre#", "lic #", "license #", "brokerage",
    "price history", "sold on", "sold for", "last sold", "closed on",
    "list price", "listed on", "price change", "price reduced", "relisted",
    "lot size", "lot area", "square foot", "square feet", "sq ft", "sqft", "acres",
    "mls #", "mls#", "listing id",
)


def focus_text(
    text: str,
    terms: tuple[str, ...] = DEFAULT_FOCUS_TERMS,
    window: int = 900,
    max_chars: int = 14_000,
    head: int = 1_500,
) -> str:
    """Keep the parts of a page near the facts, and drop the rest.

    A property page is mostly mortgage calculators, neighbourhood copy, similar
    listings and reviews. The attribution line and the price history are a few
    hundred characters in total. Sending the whole page means paying to have
    almost all of it ignored -- on a measured page, 97% of the cost was input
    tokens, and nearly all of those were irrelevant.

    This keeps a window either side of every term that tends to sit beside a
    fact, merges the windows that overlap, and always keeps the top of the page
    where the address and headline price live. Gaps are marked so the reader
    knows the text is not continuous and should not infer across a cut.

    Deliberately generous: the cost of keeping too much is a few cents, and the
    cost of cutting out the one line naming the agent is a missing attribution.
    """
    if len(text) <= max_chars:
        return text

    lowered = text.lower()
    spans: list[tuple[int, int]] = [(0, min(head, len(text)))]
    for term in terms:
        start = 0
        while True:
            found = lowered.find(term, start)
            if found == -1:
                break
            spans.append((max(0, found - window // 2), min(len(text), found + window)))
            start = found + len(term)

    spans.sort()
    merged: list[list[int]] = []
    for span_start, span_end in spans:
        if merged and span_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], span_end)
        else:
            merged.append([span_start, span_end])

    pieces: list[str] = []
    total = 0
    for span_start, span_end in merged:
        piece = text[span_start:span_end]
        if total + len(piece) > max_chars:
            piece = piece[: max_chars - total]
        pieces.append(piece)
        total += len(piece)
        if total >= max_chars:
            break
    return "\n[...]\n".join(pieces)


def looks_like_a_block_page(text: str) -> bool:
    """Whether a 200 response is actually a challenge or refusal page.

    Sites increasingly answer automated requests with a friendly 200 that
    contains a captcha or a "verify you are human" notice. Treating that as
    content produces a page of nothing, which an extractor will faithfully
    report as "no listings found" -- a silent zero rather than a visible
    failure.
    """
    lowered = text[:4000].lower()
    signals = (
        "verify you are a human",
        "verifying you are human",
        "are you a robot",
        "captcha",
        "unusual traffic",
        "access denied",
        "request blocked",
        "enable javascript and cookies to continue",
        "checking your browser",
        "cf-browser-verification",
        "pardon our interruption",
    )
    return any(signal in lowered for signal in signals)
