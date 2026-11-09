"""ISBN validation and lookup of book data from public catalogs.

Sources (order can be changed in the settings):
  * dnb         – Deutsche Nationalbibliothek (SRU, MARC21) – very good for German-language titles
  * openlibrary – Open Library (Internet Archive)
  * google      – Google Books
The results are merged: fields that one source does not provide
are filled in from the next one.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger(__name__)
TIMEOUT = 7
HEADERS = {"User-Agent": "Catalytix (Bibliotheksverwaltung)"}
FIELDS = ("title", "subtitle", "authors", "publisher", "published", "pages",
          "language", "description", "cover_url", "category")


def clean_isbn(raw: str) -> str:
    return re.sub(r"[^0-9Xx]", "", raw or "").upper()


def is_valid_isbn(isbn: str) -> bool:
    isbn = clean_isbn(isbn)
    if len(isbn) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", isbn):
            return False
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(isbn))
        return total % 11 == 0
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn[:12]))
        return (10 - total % 10) % 10 == int(isbn[12])
    return False


def to_isbn13(isbn: str) -> str:
    isbn = clean_isbn(isbn)
    if not re.fullmatch(r"\d{9}[\dX]", isbn):   # e.g. "X" in the middle of the code: leave unchanged
        return isbn
    core = "978" + isbn[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def to_isbn10(isbn13: str) -> str | None:
    isbn13 = clean_isbn(isbn13)
    if not re.fullmatch(r"978\d{10}", isbn13):
        return None
    core = isbn13[3:12]
    total = sum((10 - i) * int(c) for i, c in enumerate(core))
    check = (11 - total % 11) % 11
    return core + ("X" if check == 10 else str(check))


# ---------------------------------------------------------------- Sources

MARC = "{http://www.loc.gov/MARC21/slim}"


def _subfields(field, code):
    return [sf.text.strip() for sf in field.findall(f"{MARC}subfield") if sf.get("code") == code and sf.text]


def _strip_marc(s: str) -> str:
    s = s.replace("\u0098", "").replace("\u009c", "")  # DNB non-sorting characters
    return re.sub(r"\s*[/:;,.=]\s*$", "", s).strip()


def lookup_dnb(isbn: str) -> dict:
    url = "https://services.dnb.de/sru/dnb"
    params = {"version": "1.1", "operation": "searchRetrieve", "query": f"num={isbn}",
              "recordSchema": "MARC21-xml", "maximumRecords": "1"}
    r = requests.get(url, params=params, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    record = root.find(f".//{MARC}record")
    if record is None:
        return {}
    data: dict = {}
    authors = []
    for df in record.findall(f"{MARC}datafield"):
        tag = df.get("tag")
        if tag == "245":
            data["title"] = _strip_marc(" ".join(_subfields(df, "a")))
            sub = _subfields(df, "b")
            if sub:
                data["subtitle"] = _strip_marc(" ".join(sub))
        elif tag in ("100", "700"):
            relator = " ".join(_subfields(df, "4") + _subfields(df, "e")).lower()
            if tag == "700" and relator and not any(k in relator for k in ("aut", "verfasser")):
                continue
            for a in _subfields(df, "a"):
                name = _strip_marc(a)
                if "," in name:  # "Last, First" -> "First Last"
                    last, first = [p.strip() for p in name.split(",", 1)]
                    name = f"{first} {last}"
                if name not in authors:
                    authors.append(name)
        elif tag in ("264", "260"):
            pub = _subfields(df, "b")
            year = _subfields(df, "c")
            if pub and "publisher" not in data:
                data["publisher"] = _strip_marc(pub[0])
            if year and "published" not in data:
                m = re.search(r"\d{4}", year[0])
                data["published"] = m.group(0) if m else _strip_marc(year[0])
        elif tag == "300":
            ext = " ".join(_subfields(df, "a"))
            m = re.search(r"(\d+)\s*S", ext)
            if m:
                data["pages"] = int(m.group(1))
        elif tag == "041":
            lang = _subfields(df, "a")
            if lang:
                data["language"] = lang[0]
        elif tag == "250":
            ed = _subfields(df, "a")
            if ed:
                data["edition"] = _strip_marc(ed[0])
        elif tag == "082" and "category" not in data:
            ddc = _subfields(df, "a")
            if ddc:
                data["category"] = f"DDC {ddc[0]}"
    if authors:
        data["authors"] = ", ".join(authors)
    if data.get("title"):
        data["cover_url"] = f"https://portal.dnb.de/opac/mvb/cover?isbn={isbn}"
    return data


def lookup_openlibrary(isbn: str) -> dict:
    r = requests.get("https://openlibrary.org/api/books",
                     params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
                     timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    item = r.json().get(f"ISBN:{isbn}")
    if not item:
        return {}
    data = {
        "title": item.get("title", ""),
        "subtitle": item.get("subtitle", ""),
        "authors": ", ".join(a.get("name", "") for a in item.get("authors", [])),
        "publisher": ", ".join(p.get("name", "") for p in item.get("publishers", [])),
        "published": item.get("publish_date", ""),
        "pages": item.get("number_of_pages"),
        "cover_url": (item.get("cover") or {}).get("large") or (item.get("cover") or {}).get("medium", ""),
        "category": ", ".join(s.get("name", "") for s in item.get("subjects", [])[:3]),
    }
    return {k: v for k, v in data.items() if v}


GOOGLE_API = "https://www.googleapis.com/books/v1/volumes"


class GoogleBooksError(Exception):
    """Google Books answered with an error – the message never contains the request URL (it may carry the API
    key). quota=True: daily limit reached (HTTP 429; without an own key Google allows no queries at all)."""

    def __init__(self, status: int | None):
        self.status = status
        self.quota = status == 429
        super().__init__(f"HTTP {status}" if status else "no answer")


def google_api_key() -> str:
    """Own Google API key (Administration → Settings → "Leseproben"), stored encrypted; "" = none."""
    try:
        from .models import get_setting
        return (get_setting("google_api_key") or "").strip()
    except RuntimeError:          # outside the application (e.g. a script)
        return ""


def google_get(params: dict[str, str | int]) -> dict:
    """GET on the Books API, with the own key if there is one. Errors without URL (no key in logs)."""
    params = dict(params)
    key = google_api_key()
    if key:
        params["key"] = key
    try:
        r = requests.get(GOOGLE_API, params=params, timeout=TIMEOUT, headers=HEADERS)
    except requests.RequestException:
        raise GoogleBooksError(None) from None   # never chained: the message of exc contains the URL
    if r.status_code != 200:
        raise GoogleBooksError(r.status_code)
    data = r.json()
    return data if isinstance(data, dict) else {}


def lookup_google(isbn: str) -> dict:
    data = google_get({"q": f"isbn:{isbn}"})
    items = data.get("items") or []
    if not items:
        return {}
    v = items[0].get("volumeInfo", {})
    cover = (v.get("imageLinks") or {}).get("thumbnail", "")
    data = {
        "title": v.get("title", ""),
        "subtitle": v.get("subtitle", ""),
        "authors": ", ".join(v.get("authors", [])),
        "publisher": v.get("publisher", ""),
        "published": v.get("publishedDate", ""),
        "pages": v.get("pageCount"),
        "language": v.get("language", ""),
        "description": v.get("description", ""),
        "cover_url": cover.replace("http://", "https://").replace("&edge=curl", ""),
        "category": ", ".join(v.get("categories", [])),
    }
    return {k: v for k, v in data.items() if v}


SOURCES = {
    "dnb": ("Deutsche Nationalbibliothek", lookup_dnb),
    "openlibrary": ("Open Library", lookup_openlibrary),
    "google": ("Google Books", lookup_google),
}


def lookup(isbn: str, order: list[str] | None = None) -> dict:
    """Query all configured sources and merge the results."""
    isbn = clean_isbn(isbn)
    isbn13 = to_isbn13(isbn)
    order = order or list(SOURCES)
    result: dict = {"isbn": isbn13, "sources": [], "errors": []}
    for key in order:
        if key not in SOURCES:
            continue
        label, fn = SOURCES[key]
        try:
            data = fn(isbn13)
            if not data and len(isbn) == 10:
                data = fn(isbn)
        except Exception as exc:  # network errors etc. must not block the other sources
            log.warning("ISBN lookup %s at %s failed: %s", isbn13, key, exc)
            result["errors"].append(f"{label}: {exc.__class__.__name__}")
            continue
        if not data:
            continue
        result["sources"].append(label)
        for k, v in data.items():
            if v and not result.get(k):
                result[k] = v
        if all(result.get(f) for f in ("title", "authors", "publisher", "published", "cover_url", "description")):
            break
    result["found"] = bool(result.get("title"))
    return result
