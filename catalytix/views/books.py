"""Catalog and book maintenance."""
from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import logging
import os
import socket
import threading
import time
import uuid

import requests
import requests.adapters
import urllib3.connection
import urllib3.connectionpool
from flask import (Blueprint, Response, abort, current_app, flash, jsonify, make_response, redirect,
                   render_template, request, send_from_directory, url_for)
from flask_login import current_user, login_required
from sqlalchemy import case, func, or_, update
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from .. import isbn as isbnlib
from ..i18n import _, _n
from ..i18n import get_locale
from ..models import (Book, Group, Loan, User, db, get_bool, get_int, get_setting, manageable_books_query,
                      visible_books_query)
from . import form_int, form_str, get_by_id, is_digits, librarian_required, parse_id

bp = Blueprint("books", __name__)
log = logging.getLogger(__name__)

PER_PAGE = 24
TEXT_FIELDS = {  # field name -> maximum length
    "title": 500, "subtitle": 500, "authors": 500, "publisher": 300, "published": 30,
    "edition": 100, "language": 30, "category": 200, "tags": 500, "location": 200,
    "signature": 100, "cover_url": 1000,
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


MAX_PAGES = 100_000
MAX_COPIES = 10_000


def _active_loan_counts():
    """Subquery: book ID → occupied copies (open loans plus copies held for reservations) – for filters,
    suggestions and export without a query per book."""
    from ..models import occupied_counts
    return occupied_counts()


def _with_active_loans():
    """Eager-load only the open loans (and held reservations) – enough for "available", without the whole
    history and without a query per book."""
    from ..models import RES_READY, Reservation
    return (selectinload(Book.loans.and_(Loan.returned_at.is_(None))),
            selectinload(Book.reservations.and_(Reservation.status == RES_READY)))


def _bounded_int(value, maximum: int) -> int | None:
    """Clamp a number from JSON (int or digit text) to 1 … maximum; anything else → None."""
    if isinstance(value, str):
        value = value.strip()
        value = (int(value) if len(value) <= 30 else maximum) if is_digits(value) else None
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        return None
    return min(maximum, max(1, value))


def _get_book_or_404(book_id: int) -> Book:
    book = get_by_id(Book, book_id)
    if book is None:
        abort(404)
    return book


def _get_managed_book(book_id: int) -> Book:
    """Book the current user may manage. 404 (not 403) for books they cannot even see, so a group-bound
    librarian cannot tell which ids exist in other groups."""
    book = _get_book_or_404(book_id)
    if not current_user.can_manage(book):
        abort(403 if current_user.can_see(book, get_setting("catalog_visibility") == "all") else 404)
    return book


def _check_group_allowed(group_id):
    if group_id is None:
        return None
    group = get_by_id(Group, group_id)
    if group is None or group not in current_user.manageable_groups():
        abort(403)
    return group.id


# ---------------------------------------------------------------- Cover

def _store_cover_bytes(data: bytes, ext: str) -> str:
    name = f"{hashlib.sha1(data).hexdigest()[:20]}{ext}"
    path = os.path.join(current_app.config["COVER_DIR"], name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return name


COVER_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
COVER_MAX = 5 * 1024 * 1024
_COVER_FAILED: dict[int, float] = {}  # book ID -> time of the last failure


COVER_DEADLINE = 10   # seconds for fetching a whole cover (all redirects combined)
_COVER_TIMEOUT = 6    # seconds per connection attempt or read
# Address ranges that can hide internal IPv4 addresses: NAT64, IPv4-mapped/-compatible, Teredo
_BLOCKED_V6 = [ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "::ffff:0:0/96", "::/96",
                                                 "2001::/32")]
_fetch_state = threading.local()


def _ip_allowed(addr: str) -> bool:
    """Public unicast addresses only."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        if any(ip in net for net in _BLOCKED_V6):
            return False
        if ip.sixtofour is not None:   # 6to4: the embedded IPv4 address counts
            return _ip_allowed(str(ip.sixtofour))
    return ip.is_global and not ip.is_multicast


def _resolve_public(host: str, port: int) -> list | None:
    """Resolve the name; return the list (getaddrinfo form) only if every single address is public."""
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return None
    if not infos or not all(_ip_allowed(info[4][0]) for info in infos):
        return None
    return infos


def _public_host(host: str) -> bool:
    """Fetch only public addresses (no access to internal servers via the cover feature)."""
    return _resolve_public(host, 443) is not None


class _PinnedConnectionMixin:
    """Resolves the name only when connecting, checks all addresses and connects to exactly one checked
    address. DNS rebinding between check and connect thus has no effect; Host header, SNI and
    certificate verification still use the name."""

    def _new_conn(self):
        from urllib3.exceptions import NewConnectionError
        name = self._dns_host
        infos = _resolve_public(name, self.port)
        if not infos:
            raise NewConnectionError(self, f"{self.host}: keine (nur öffentliche) Adresse")
        error = None
        try:
            for info in infos:
                self._dns_host = info[4][0]
                try:
                    sock = super()._new_conn()
                except Exception as exc:  # noqa: BLE001 – try the next checked address
                    error = exc
                    continue
                socks = getattr(_fetch_state, "socks", None)
                if socks is not None:
                    socks.append(sock)
                return sock
        finally:
            self._dns_host = name
        raise error


class _PinnedHTTPConnection(_PinnedConnectionMixin, urllib3.connection.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, urllib3.connection.HTTPSConnection):
    pass


class _PinnedHTTPPool(urllib3.connectionpool.HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSPool(urllib3.connectionpool.HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _PinnedHTTPPool, "https": _PinnedHTTPSPool}


def _cover_session() -> requests.Session:
    session = requests.Session()
    # No detour via HTTP(S)_PROXY: the proxy would resolve the name itself (unchecked).
    # If covers can only be fetched via a proxy, set CATALYTIX_COVER_VIA_PROXY=1 (the proxy must then
    # block internal targets itself).
    session.trust_env = os.environ.get("CATALYTIX_COVER_VIA_PROXY") == "1"
    adapter = _PinnedAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _http_get(session: requests.Session, url: str):
    return session.get(url, timeout=_COVER_TIMEOUT, stream=True, allow_redirects=False,
                       headers={**isbnlib.HEADERS, "Accept-Encoding": "identity"})


def _abort_sockets(socks: list):
    """Deadline expired: forcibly close open connections (a blocking read then returns too)."""
    for sock in list(socks):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _fetch_cover(url: str) -> tuple[bytes, str] | None:
    """Fetch the cover image server-side (users' browsers never connect to third-party servers)."""
    from urllib.parse import urljoin, urlsplit
    deadline = time.monotonic() + COVER_DEADLINE
    socks: list = []
    _fetch_state.socks = socks
    watchdog = threading.Timer(COVER_DEADLINE, _abort_sockets, [socks])
    watchdog.daemon = True
    watchdog.start()
    session = _cover_session()
    try:
        for _hop in range(4):  # at most 3 redirects, each one is checked
            parts = urlsplit(url)
            if parts.scheme not in ("https", "http") or not parts.hostname or not _public_host(parts.hostname):
                return None
            r = _http_get(session, url)
            try:
                if r.is_redirect and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if r.status_code != 200 or ctype not in COVER_TYPES:
                    return None
                data = bytearray()
                while len(data) <= COVER_MAX:
                    if time.monotonic() > deadline:
                        log.info("Cover download aborted (%s): takes longer than %s s", url, COVER_DEADLINE)
                        return None
                    chunk = r.raw.read1(64 * 1024, decode_content=True)
                    if not chunk:
                        break
                    data += chunk
                if len(data) < 1500 or len(data) > COVER_MAX:  # ignore placeholder images
                    return None
                return bytes(data), ctype
            finally:
                r.close()
        return None
    except Exception as exc:  # noqa: BLE001 – network, protocol and decoding errors: no cover, no 500
        log.info("Cover download failed (%s): %s", url, exc)
        return None
    finally:
        watchdog.cancel()
        _fetch_state.socks = None
        session.close()


def _clean_image(raw: bytes) -> bytes | None:
    """Accept only real images and re-encode them (removes injected content and metadata)."""
    from PIL import Image
    from ..branding import BrandingError, _open_image
    try:
        img = _open_image(raw)
        img = img.convert("RGB")
        img.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=86, optimize=True)
        return out.getvalue()
    except BrandingError:
        return None
    except Exception as exc:  # noqa: BLE001 – e.g. truncated JPEG (OSError) or decompression bomb
        log.info("Image not readable: %s", exc)
        return None


def _download_cover(url: str) -> str:
    """Downloads a cover and stores it locally; returns the file name or ''."""
    try:
        got = _fetch_cover(url)
        clean = _clean_image(got[0]) if got else None
        return _store_cover_bytes(clean, ".jpg") if clean else ""
    except Exception as exc:  # noqa: BLE001 – a broken cover must never prevent saving/displaying
        log.warning("Could not take over the cover (%s): %s", url, exc)
        return ""


def _apply_cover(book: Book, old_url: str = ""):
    upload = request.files.get("cover_upload")
    if upload and upload.filename:
        ext = os.path.splitext(secure_filename(upload.filename))[1].lower()
        clean = _clean_image(upload.read()) if ext in IMAGE_EXT else None
        if clean is None:
            flash(_("Cover: Die Datei ist kein gültiges Bild (erlaubt: JPG, PNG, WEBP oder GIF)."), "error")
        else:
            book.cover_file = _store_cover_bytes(clean, ".jpg")
            return
    if request.form.get("remove_cover"):
        book.cover_file, book.cover_url = "", ""
        return
    if book.cover_url and (book.cover_url != old_url or not book.cover_file):
        book.cover_file = _download_cover(book.cover_url) or book.cover_file


@bp.route("/covers/<path:filename>")
@login_required
def cover(filename):
    resp = send_from_directory(current_app.config["COVER_DIR"], secure_filename(filename),
                               max_age=60 * 60 * 24 * 30)
    # Only behind a login (covers of books in other groups): browsers may cache, shared proxies must not
    resp.cache_control.public = False
    resp.cache_control.private = True
    return resp


@bp.route("/covers/buch/<int:book_id>")
@login_required
def cover_remote(book_id):
    """Cover of a book not yet stored locally: fetch and store it once server-side."""
    book = _get_book_or_404(book_id)
    if not current_user.can_see(book, get_setting("catalog_visibility") == "all"):
        abort(404)
    if not book.cover_file:
        if not book.cover_url or time.time() - _COVER_FAILED.get(book.id, 0) < 3600:
            abort(404)
        name = ""
        try:
            name = _download_cover(book.cover_url)
        finally:
            if not name:   # also on unexpected errors: don't retry on every catalog request
                _COVER_FAILED[book.id] = time.time()
        if not name:
            abort(404)
        book.cover_file = name
        db.session.commit()
    return redirect(url_for("books.cover", filename=book.cover_file))


@bp.route("/covers/vorschau")
@librarian_required
def cover_preview():
    """Preview while adding a book (image from the ISBN lookup), loaded via our own server."""
    got = _fetch_cover((request.args.get("u") or "")[:1000])
    clean = _clean_image(got[0]) if got else None
    if not clean:
        abort(404)
    return Response(clean, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600",
                                                          "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- Catalog

@bp.route("/buecher")
@login_required
def index():
    q = (request.args.get("q") or "").strip()
    group = request.args.get("gruppe", "")
    location = request.args.get("standort", "")
    status = request.args.get("status", "")
    sort = request.args.get("sort", "title")
    page = max(1, request.args.get("seite", 1, type=int))

    query = visible_books_query(current_user)
    if q:
        like = f"%{q}%"
        clean = isbnlib.clean_isbn(q)
        conds = [Book.title.ilike(like), Book.subtitle.ilike(like), Book.authors.ilike(like),
                 Book.publisher.ilike(like), Book.tags.ilike(like), Book.category.ilike(like),
                 Book.signature.ilike(like), Book.location.ilike(like)]
        if len(clean) >= 4:
            conds.append(Book.isbn.ilike(f"%{clean}%"))
            if len(clean) == 10:
                conds.append(Book.isbn == isbnlib.to_isbn13(clean))
        query = query.filter(or_(*conds))
    if group == "allgemein":
        query = query.filter(Book.group_id.is_(None))
    elif parse_id(group):
        query = query.filter(Book.group_id == parse_id(group))
    if location:
        query = query.filter(Book.location == location)
    if status in ("verfuegbar", "ausgeliehen"):   # filter in SQL instead of loading each book individually
        active = _active_loan_counts()
        lent = func.coalesce(active.c.n, 0)
        query = query.outerjoin(active, active.c.book_id == Book.id)
        query = query.filter(Book.copies > lent if status == "verfuegbar" else lent > 0)

    order = {
        "title": Book.title.asc(), "author": Book.authors.asc(),
        "new": Book.created_at.desc(), "year": Book.published.desc(),
    }.get(sort, Book.title.asc())
    total = query.order_by(None).count()
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    books = (query.options(*_with_active_loans(), selectinload(Book.group))
             .order_by(order, Book.id).offset((page - 1) * PER_PAGE).limit(PER_PAGE).all())

    if current_user.is_global_manager or get_setting("catalog_visibility") == "all":
        groups = Group.query.order_by(Group.name).all()
    else:
        groups = sorted(current_user.groups, key=lambda g: g.name.lower())
    from .locations import names as location_names
    return render_template("books/index.html", books=books, q=q, group=group, status=status,
                           sort=sort, page=page, pages=pages, total=total, groups=groups,
                           location=location, location_names=location_names())


SUGGEST_LIMIT = 8


@bp.route("/api/katalog")
@login_required
def api_catalog():
    """Suggestions for the top search field: only books the user may see in the catalog."""
    from .loans import _like_escape
    q = (request.args.get("q") or "").strip()[:100]
    if len(q) < 2:
        return jsonify({"results": [], "more": False})
    like = f"%{_like_escape(q)}%"
    clean = isbnlib.clean_isbn(q)
    isbn13 = isbnlib.to_isbn13(clean) if len(clean) in (10, 13) else None
    conds = [Book.title.ilike(like, escape="\\"), Book.subtitle.ilike(like, escape="\\"),
             Book.authors.ilike(like, escape="\\"), Book.tags.ilike(like, escape="\\"),
             Book.signature.ilike(like, escape="\\")]
    if len(clean) >= 4:
        conds.append(Book.isbn.ilike(f"%{clean}%"))
    if isbn13:
        conds.append(Book.isbn == isbn13)
    active = _active_loan_counts()
    base = (visible_books_query(current_user)
            .outerjoin(active, active.c.book_id == Book.id)
            .add_columns(func.coalesce(active.c.n, 0)))
    # Exact matches (ISBN, call number) always first, then titles starting with the search text
    exact = [func.lower(Book.signature) == q.lower()]
    if isbn13:
        exact.append(Book.isbn == isbn13)
    rows = base.filter(or_(*exact)).order_by(Book.title).limit(SUGGEST_LIMIT).all()
    seen = {b.id for b, _n in rows}
    starts = f"{_like_escape(q)}%"
    rest = (base.filter(or_(*conds)).filter(~Book.id.in_(seen or {0}))
            .order_by(Book.title.ilike(starts, escape="\\").desc(), Book.title, Book.id)
            .limit(SUGGEST_LIMIT * 2).all())
    rows += rest
    results = [{"id": b.id, "url": url_for("books.detail", book_id=b.id), "title": b.title,
                "authors": b.authors or "", "signature": b.signature or "",
                "available": max(0, (b.copies or 0) - n), "copies": b.copies or 0}
               for b, n in rows[:SUGGEST_LIMIT]]
    return jsonify({"results": results, "more": len(rows) > SUGGEST_LIMIT})


@bp.route("/buecher/<int:book_id>")
@login_required
def detail(book_id):
    from ..reading_sample import enabled as reading_sample_enabled
    book = _get_book_or_404(book_id)
    if not current_user.can_see(book, get_setting("catalog_visibility") == "all"):
        abort(404)
    own_loan = next((l for l in book.active_loans if l.user_id == current_user.id), None)
    from ..people import directory_available
    from .. import reservations
    res_on = reservations.enabled()
    my_res = reservations.open_for(current_user, book) if res_on else None
    waiting = res_on and any(r.status == "waiting" for r in book.reservations)
    return render_template("books/detail.html", book=book, own_loan=own_loan,
                           directory_search=current_user.can_manage(book) and directory_available(),
                           self_checkout=get_bool("self_checkout"),
                           self_return=get_bool("self_return"),
                           can_extend=bool(own_loan) and (current_user.can_manage(book) or (
                               get_bool("self_extend") and own_loan.extensions < get_int("max_extensions", 2)
                               and not waiting)),
                           extend_blocked=bool(own_loan) and waiting and not current_user.can_manage(book),
                           res_on=res_on, my_res=my_res,
                           my_pos=reservations.position(my_res) if my_res else 0,
                           avail_me=book.available_for(current_user),
                           loan_days=get_setting("loan_days"),
                           reading_sample=reading_sample_enabled())


# ---------------------------------------------------------------- Reading sample (Google Books, optional)

def _sample_book_or_404(book_id: int) -> Book:
    from .. import reading_sample
    book = _get_book_or_404(book_id)
    if not reading_sample.enabled() or not current_user.can_see(book, get_setting("catalog_visibility") == "all"):
        abort(404)
    return book


@bp.route("/api/leseprobe/<int:book_id>")
@login_required
def reading_sample_status(book_id):
    """Is there a reading sample? Asked by the book page; only the server talks to Google (only the ISBN)."""
    from .. import reading_sample
    book = _sample_book_or_404(book_id)
    return jsonify({"available": reading_sample.lookup(book.isbn) is not None})


@bp.route("/buecher/<int:book_id>/leseprobe")
@login_required
def reading_sample(book_id):
    """Two clicks: first a notice that the sample comes from Google, Google's viewer only after "laden=1"."""
    from .. import CSP, reading_sample as sample
    book = _sample_book_or_404(book_id)
    if request.args.get("laden") != "1":
        return render_template("books/reading_sample.html", book=book, embed=None, missing=False)
    volume = sample.lookup(book.isbn)
    if not volume:
        return render_template("books/reading_sample.html", book=book, embed=None, missing=True), 404
    resp = make_response(render_template("books/reading_sample.html", book=book, embed=sample.embed_url(volume),
                                         missing=False))
    resp.headers["Content-Security-Policy"] = sample.csp(CSP)
    return resp


# ---------------------------------------------------------------- Maintenance

def _fill_book(book: Book) -> str | None:
    """Apply the form; returns an error message (e.g. unknown location) or None."""
    from .locations import check as check_location
    previous_location = book.location or ""
    for field, maxlen in TEXT_FIELDS.items():
        setattr(book, field, form_str(field, maxlen))
    book.location, location_error = check_location(form_str("location", 200), previous_location)
    book.isbn = isbnlib.to_isbn13(isbnlib.clean_isbn(form_str("isbn", 30)))
    book.description = (request.form.get("description") or "").strip()[:20000]
    book.pages = form_int("pages", None, 1, MAX_PAGES)
    book.copies = form_int("copies", 1, 0, MAX_COPIES)
    if book.copies < len(book.active_loans):
        book.copies = len(book.active_loans)
        flash(_("Anzahl der Exemplare kann nicht kleiner als die aktuell verliehenen sein."), "warning")
    gid = request.form.get("group_id")
    book.group_id = _check_group_allowed(parse_id(gid)) if parse_id(gid) else None
    return location_error


def _same_book(book: Book) -> Book | None:
    """Existing book with the same ISBN in the same group that the person may manage."""
    if not book.isbn:
        return None
    same_group = Book.group_id.is_(None) if book.group_id is None else Book.group_id == book.group_id
    for other in Book.query.filter(Book.isbn == book.isbn, same_group).order_by(Book.id):
        if current_user.can_manage(other):
            return other
    return None


def _add_copies(same: Book, add: int) -> int:
    """Add copies to an existing book (in SQL, so concurrent entries are all counted); returns how many were
    really added (at the upper limit possibly fewer)."""
    before = same.copies or 0
    if add:
        total = func.coalesce(Book.copies, 0) + add
        db.session.execute(update(Book).where(Book.id == same.id)
                           .values(copies=case((total > MAX_COPIES, MAX_COPIES), else_=total))
                           .execution_options(synchronize_session=False))
    db.session.commit()
    from .. import reservations
    reservations.promote(same.id)          # new copies go to the waiting queue first
    db.session.refresh(same)
    return max(0, (same.copies or 0) - before)


@bp.route("/buecher/neu", methods=["GET", "POST"])
@librarian_required
def create():
    book = Book(copies=1)
    if request.method == "POST":
        error = _fill_book(book)
        if error:
            flash(error, "error")
        elif not book.title:
            flash(_("Ein Titel ist erforderlich."), "error")
        elif (same := _same_book(book)) is not None:
            # Same book (ISBN) already exists in the same group: add copies there instead of creating a duplicate
            added = _add_copies(same, book.copies or 0)
            if added:
                flash(_n("„{title}“ ist bereits im Bestand – {n} Exemplar hinzugefügt, jetzt {copies}. Die übrigen "
                         "Angaben des Formulars wurden nicht übernommen.",
                         "„{title}“ ist bereits im Bestand – {n} Exemplare hinzugefügt, jetzt {copies}. Die übrigen "
                         "Angaben des Formulars wurden nicht übernommen.",
                         added, title=same.title, copies=same.copies), "success")
            else:
                flash(_("„{title}“ ist bereits im Bestand ({copies} Exemplare) – es wurde nichts hinzugefügt.",
                        title=same.title, copies=same.copies), "warning")
            if request.form.get("next_action") == "another":
                return redirect(url_for("books.create"))
            return redirect(url_for("books.detail", book_id=same.id))
        else:
            book.created_by_id = current_user.id
            _apply_cover(book)
            db.session.add(book)
            db.session.commit()
            flash(_("„{title}“ wurde angelegt.", title=book.title), "success")
            if request.form.get("next_action") == "another":
                return redirect(url_for("books.create"))
            return redirect(url_for("books.detail", book_id=book.id))
    else:
        book.isbn = isbnlib.clean_isbn(request.args.get("isbn", ""))
        gid = parse_id(request.args.get("gruppe"))
        if gid:
            book.group_id = gid
    from .locations import names as location_names
    return render_template("books/form.html", book=book, groups=current_user.manageable_groups(),
                           is_new=True, location_names=location_names())


@bp.route("/buecher/<int:book_id>/bearbeiten", methods=["GET", "POST"])
@librarian_required
def edit(book_id):
    book = _get_managed_book(book_id)
    if request.method == "POST":
        old_url = book.cover_url
        error = _fill_book(book)
        if error:
            flash(error, "error")
        elif not book.title:
            flash(_("Ein Titel ist erforderlich."), "error")
        else:
            _apply_cover(book, old_url)
            db.session.commit()
            from .. import reservations
            reservations.rebalance(book.id)    # number of copies changed: adjust reservations
            flash(_("Änderungen gespeichert."), "success")
            return redirect(url_for("books.detail", book_id=book.id))
    from .locations import names as location_names
    return render_template("books/form.html", book=book, groups=current_user.manageable_groups(),
                           is_new=False, location_names=location_names())


@bp.route("/buecher/<int:book_id>/loeschen", methods=["POST"])
@librarian_required
def delete(book_id):
    book = _get_managed_book(book_id)
    if book.active_loans:
        flash(_("Das Buch ist noch ausgeliehen und kann nicht gelöscht werden."), "error")
        return redirect(url_for("books.detail", book_id=book.id))
    title = book.title
    db.session.delete(book)
    db.session.commit()
    flash(_("„{title}“ wurde gelöscht.", title=title), "success")
    return redirect(url_for("books.index"))


# ---------------------------------------------------------------- ISBN / quick entry

@bp.route("/api/isbn/<raw>")
@librarian_required
def api_isbn(raw):
    clean = isbnlib.clean_isbn(raw)
    if not isbnlib.is_valid_isbn(clean):
        return jsonify({"ok": False, "error": _("Keine gültige ISBN (Prüfziffer stimmt nicht).")}), 400
    isbn13 = isbnlib.to_isbn13(clean)
    existing = [   # only books the user may see in the catalog – no titles from other groups
        {"id": b.id, "title": b.title, "copies": b.copies, "available": b.available,
         "group": b.group.name if b.group else _("Allgemein"), "url": url_for("books.detail", book_id=b.id),
         "manageable": current_user.can_manage(b)}
        for b in visible_books_query(current_user).filter(Book.isbn == isbn13).all()
    ]
    data = {"ok": True, "isbn": isbn13, "existing": existing}
    if request.args.get("lookup", "1") != "0":
        sources = [s.strip() for s in (get_setting("isbn_sources") or "").split(",") if s.strip()]
        data["data"] = isbnlib.lookup(isbn13, sources or None)
    return jsonify(data)


@bp.route("/buecher/schnellerfassung")
@librarian_required
def scan():
    from .locations import names as location_names
    return render_template("books/scan.html", groups=current_user.manageable_groups(),
                           location_names=location_names())


@bp.route("/api/buecher", methods=["POST"])
@librarian_required
def api_quick_add():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": _("Ungültige Anfrage.")}), 400
    for key in (*TEXT_FIELDS, "isbn", "description", "pages", "copies", "group_id"):
        if payload.get(key) is not None and (isinstance(payload[key], bool)
                                             or not isinstance(payload[key], (str, int, float))):
            return jsonify({"ok": False, "error": _("Ungültige Anfrage.")}), 400
    title = str(payload.get("title") or "").strip()[:500]
    if not title:
        return jsonify({"ok": False, "error": _("Titel fehlt.")}), 400
    book = Book(title=title, created_by_id=current_user.id)
    for field, maxlen in TEXT_FIELDS.items():
        if field != "title":
            setattr(book, field, str(payload.get(field) or "").strip()[:maxlen])
    from .locations import check as check_location
    book.location, location_error = check_location(str(payload.get("location") or ""))
    if location_error:
        return jsonify({"ok": False, "error": location_error}), 400
    book.isbn = isbnlib.to_isbn13(isbnlib.clean_isbn(str(payload.get("isbn") or "")))
    book.description = str(payload.get("description") or "")[:20000]
    book.pages = _bounded_int(payload.get("pages"), MAX_PAGES)
    book.copies = _bounded_int(payload.get("copies"), MAX_COPIES) or 1
    gid = payload.get("group_id")
    if gid not in (None, "", "0", 0):
        if not parse_id(gid):
            return jsonify({"ok": False, "error": _("Ungültige Anfrage.")}), 400
        book.group_id = _check_group_allowed(parse_id(gid))
    if (same := _same_book(book)) is not None:
        # Same ISBN already in this group (e.g. scanned twice in auto mode, or the page was stale): add the
        # copies there instead of creating a duplicate – like the normal form does
        _add_copies(same, book.copies or 0)
        return jsonify({"ok": True, "id": same.id, "url": url_for("books.detail", book_id=same.id),
                        "merged": True, "copies": same.copies})
    if book.cover_url:
        book.cover_file = _download_cover(book.cover_url)
    db.session.add(book)
    db.session.commit()
    return jsonify({"ok": True, "id": book.id, "url": url_for("books.detail", book_id=book.id)})


@bp.route("/api/buecher/<int:book_id>/exemplar", methods=["POST"])
@librarian_required
def api_add_copy(book_id):
    book = _get_managed_book(book_id)
    # Increment in SQL: two simultaneous clicks yield two copies, not one
    db.session.execute(update(Book).where(Book.id == book.id, Book.copies < MAX_COPIES)
                       .values(copies=func.coalesce(Book.copies, 0) + 1).execution_options(synchronize_session=False))
    db.session.commit()
    from .. import reservations
    reservations.promote(book.id)          # new copy goes to the waiting queue first
    db.session.refresh(book)
    return jsonify({"ok": True, "copies": book.copies, "available": book.available})


# ---------------------------------------------------------------- Export

@bp.route("/buecher/export.csv")
@librarian_required
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([_(h) for h in ["ID", "ISBN", "Titel", "Untertitel", "Autoren", "Verlag", "Erschienen", "Auflage",
                "Seiten", "Sprache", "Kategorie", "Schlagwörter", "Standort", "Signatur",
                "Gruppe", "Exemplare", "Verfügbar"]])
    def cell(v):
        # Protection against formula injection in Excel: mark content starting with = + - @ as text
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    active = _active_loan_counts()
    rows = (manageable_books_query(current_user).outerjoin(active, active.c.book_id == Book.id)
            .add_columns(func.coalesce(active.c.n, 0)).options(selectinload(Book.group))
            .order_by(Book.title, Book.id))
    for b, lent in rows:
        w.writerow([cell(v) for v in [b.id, b.isbn, b.title, b.subtitle, b.authors, b.publisher, b.published, b.edition,
                    b.pages or "", b.language, b.category, b.tags, b.location, b.signature,
                    b.group.name if b.group else _("Allgemein"), b.copies, max(0, (b.copies or 0) - lent)]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + (
                        "catalogue.csv" if get_locale() == "en" else "bestand.csv")})
