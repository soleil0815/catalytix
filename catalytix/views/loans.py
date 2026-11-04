"""Checkout, return, renewal and circulation desk."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import contains_eager

from .. import isbn as isbnlib
from ..i18n import _, format_day
from ..models import (Book, Loan, User, count_active_loans, db, default_due_date, get_bool, get_int, get_setting,
                      lendable_books, manageable_books_query)
from ..mailer import notify_loan, notify_return, send_manual_reminder
from .. import people, reservations
from . import get_by_id, librarian_required, parse_id, safe_next

bp = Blueprint("loans", __name__)
PER_PAGE = 50


def _back(default: str):
    return redirect(safe_next(request.form.get("next")) or default)


def lock_book(book_id: int) -> None:
    """Acquire a write lock on this book until the end of the transaction – before any check.

    The no-op update (setting a value to itself) starts the write transaction in SQLite
    (RESERVED lock: concurrent checkouts wait for each other) and locks the book row in
    MySQL/PostgreSQL; SELECT … FOR UPDATE additionally secures this there (SQLite ignores it)."""
    db.session.execute(update(Book).where(Book.id == book_id).values(updated_at=Book.updated_at)
                       .execution_options(synchronize_session=False))
    if db.session.get_bind().dialect.name != "sqlite":
        db.session.query(Book.id).filter(Book.id == book_id).with_for_update().first()


def lend_book(book: Book, borrower: User, due: date | None = None, note: str = "") -> tuple[bool, str]:
    """Central checkout logic including all checks."""
    if not book.lendable:
        return False, _("„{title}“ gehört zum Präsenzbestand (Gruppe „{group}“) und wird nicht verliehen.",
                        title=book.title, group=book.group.name)
    if not borrower.active:
        return False, _("{name} ist gesperrt.", name=borrower.name)
    if not borrower.can_borrow(book):
        grp = book.group.name if book.group else "–"
        return False, _("{name} gehört nicht zur Gruppe „{group}“ und darf dieses Buch nicht ausleihen.", name=borrower.name, group=grp)
    limit = get_int("max_loans", 0)
    try:
        lock_book(book.id)
        if limit and db.session.get_bind().dialect.name != "sqlite":
            # Per-person limit: also serialize concurrent loans of different books for the same person
            db.session.query(User.id).filter(User.id == borrower.id).with_for_update().first()
        # From here on count fresh from the database – concurrent requests wait until the commit.
        copies = db.session.query(Book.copies).filter(Book.id == book.id).scalar() or 0
        active = dict(db.session.query(Loan.user_id, func.count(Loan.id))
                      .filter(Loan.book_id == book.id, Loan.returned_at.is_(None)).group_by(Loan.user_id).all())
        if borrower.id in active:
            db.session.rollback()
            return False, _("{name} hat dieses Buch bereits ausgeliehen.", name=borrower.name)
        held = reservations.held_by_others(book.id, borrower.id)   # held for other people's reservations
        if sum(active.values()) + held >= copies:
            db.session.rollback()
            if held and sum(active.values()) < copies:
                return False, _("Das freie Exemplar ist für eine Vormerkung zurückgelegt.")
            return False, _("Kein Exemplar verfügbar.")
        # Don't skip the queue: a copy that just became free and is not yet held
        # (e.g. account with a reservation deleted) goes first to those waiting ahead of the person
        ahead = reservations.waiting_ahead(book.id, borrower.id)
        if ahead and copies - sum(active.values()) - held <= ahead:
            db.session.rollback()
            reservations.promote(book.id)        # hold the copy for those waiting and notify them
            return False, _("Das freie Exemplar steht zuerst den Personen zu, die das Buch vorgemerkt haben.")
        if limit and count_active_loans(borrower.id) >= limit:
            db.session.rollback()
            return False, _("{name} hat bereits die maximale Anzahl von {limit} Ausleihen.", name=borrower.name, limit=limit)
        loan = Loan(book_id=book.id, user_id=borrower.id, issued_by_id=current_user.id,
                    due_at=due or default_due_date(), note=note[:500])
        db.session.add(loan)
        reservations.fulfil_on_loan(book.id, borrower.id)   # own reservation is thereby fulfilled
        db.session.commit()
    except IntegrityError:   # unique index: the same person has just received the book
        db.session.rollback()
        return False, _("{name} hat dieses Buch bereits ausgeliehen.", name=borrower.name)
    except OperationalError:  # e.g. database locked for too long
        db.session.rollback()
        return False, _("Die Datenbank ist gerade beschäftigt. Bitte versuch es gleich noch einmal.")
    notify_loan(loan)
    return True, _("„{title}“ an {name} verliehen – fällig am {date}.", title=book.title, name=borrower.name,
                   date=format_day(loan.due_at))


def return_loan(loan: Loan) -> str:
    # Only check in if the loan is still open – two simultaneous returns (double click, two desks)
    # must neither overwrite the timestamp nor send two confirmations.
    res = db.session.execute(
        update(Loan).where(Loan.id == loan.id, Loan.returned_at.is_(None))
        .values(returned_at=datetime.now(), returned_by_id=current_user.id)
        .execution_options(synchronize_session=False))
    db.session.commit()
    db.session.refresh(loan)
    msg = _("„{title}“ von {name} zurückgenommen.", title=loan.book.title, name=loan.user.name)
    if res.rowcount == 1:
        notify_return(loan)
        readied = reservations.promote(loan.book_id)
        if readied and current_user.can_manage(loan.book):
            msg += " " + _("Bitte zurücklegen – vorgemerkt für {names}.", names=", ".join(f"{r.user.name} ({r.user.username})" for r in readied))
    return msg


@bp.route("/buecher/<int:book_id>/ausleihen", methods=["POST"])
@login_required
def borrow(book_id):
    book = get_by_id(Book, book_id) or abort(404)
    if not current_user.can_see(book, get_setting("catalog_visibility") == "all"):
        abort(404)   # like the book page: invisible books reveal neither title nor group here either
    target_id = parse_id(request.form.get("user_id"))
    target_name = (request.form.get("benutzer") or "").strip()[:200]
    due = None
    if target_name and target_name.lower() != current_user.username.lower():
        if not current_user.can_manage(book):
            abort(403)
        borrower = people.find(target_name)
        if borrower is None:
            flash(_("Benutzer „{query}“ nicht gefunden.", query=target_name), "error")
            return _back(url_for("books.detail", book_id=book.id))
    elif target_id and target_id != current_user.id:
        if not current_user.can_manage(book):
            abort(403)
        borrower = get_by_id(User, target_id) or abort(404)
    else:
        borrower = current_user
        if not get_bool("self_checkout") and not current_user.can_manage(book):
            abort(403)
    if current_user.can_manage(book) and request.form.get("due_at"):
        try:
            due = date.fromisoformat(request.form["due_at"])
        except ValueError:
            due = None
        if due is not None and not (date.today() <= due <= date.today() + timedelta(days=3650)):
            flash(_("Das Fälligkeitsdatum muss zwischen heute und in zehn Jahren liegen."), "error")
            return _back(url_for("books.detail", book_id=book.id))
    ok, msg, _person = people.lend_to(book, borrower, due, request.form.get("note", ""))
    flash(msg, "success" if ok else "error")
    return _back(url_for("books.detail", book_id=book.id))


@bp.route("/ausleihen/<int:loan_id>/rueckgabe", methods=["POST"])
@login_required
def give_back(loan_id):
    loan = get_by_id(Loan, loan_id) or abort(404)
    own = loan.user_id == current_user.id and get_bool("self_return")
    if not (own or current_user.can_manage(loan.book)):
        abort(403)
    if loan.returned_at:
        flash(_("Diese Ausleihe ist bereits abgeschlossen."), "info")
    else:
        flash(return_loan(loan), "success")
    return _back(url_for("loans.index"))


@bp.route("/ausleihen/<int:loan_id>/verlaengern", methods=["POST"])
@login_required
def extend(loan_id):
    loan = get_by_id(Loan, loan_id) or abort(404)
    manager = current_user.can_manage(loan.book)
    own = loan.user_id == current_user.id and get_bool("self_extend")
    if not (manager or own):
        abort(403)
    max_ext, days = get_int("max_extensions", 2), get_int("extension_days", 14)
    if not manager and reservations.enabled() and any(r.status == "waiting" for r in loan.book.reservations):
        msg = _("Dieses Buch ist vorgemerkt und kann deshalb nicht verlängert werden. Bitte gib es bis zur Frist zurück.")
        flash(msg, "error")
        return _back(url_for("loans.index"))
    for _attempt in range(3):
        if loan.returned_at:
            flash(_("Diese Ausleihe ist bereits abgeschlossen."), "info")
            break
        if not manager and loan.extensions >= max_ext:
            flash(_("Die maximale Anzahl an Verlängerungen ist erreicht. Bitte wende dich an die Bibliothek."), "error")
            break
        old_ext = loan.extensions
        new_due = max(loan.due_at, date.today()) + timedelta(days=days)
        # Only apply if nobody else has renewed or returned it in the meantime
        res = db.session.execute(
            update(Loan).where(Loan.id == loan.id, Loan.returned_at.is_(None), Loan.extensions == old_ext)
            .values(extensions=Loan.extensions + 1, due_at=new_due, due_changed_at=datetime.now()).execution_options(synchronize_session=False))
        db.session.commit()
        if res.rowcount == 1:
            flash(_("Verlängert bis {date}.", date=format_day(new_due)), "success")
            break
        db.session.refresh(loan)   # concurrent change – check again against the new state
    return _back(url_for("loans.index"))


@bp.route("/buecher/<int:book_id>/vormerken", methods=["POST"])
@login_required
def reserve(book_id):
    book = get_by_id(Book, book_id) or abort(404)
    if not reservations.enabled():
        abort(404)
    if not current_user.can_see(book, get_setting("catalog_visibility") == "all"):
        abort(404)
    ok, msg = reservations.reserve(book, current_user)
    flash(msg, "success" if ok else "error")
    return _back(url_for("books.detail", book_id=book.id))


@bp.route("/vormerkungen/<int:res_id>/stornieren", methods=["POST"])
@login_required
def cancel_reservation(res_id):
    from ..models import Reservation
    res = get_by_id(Reservation, res_id) or abort(404)
    if res.user_id != current_user.id and not current_user.can_manage(res.book):
        abort(403)
    if reservations.cancel(res):
        if res.user_id == current_user.id:
            flash(_("Vormerkung für „{title}“ gelöscht.", title=res.book.title), "success")
        else:
            flash(_("Vormerkung von {name} für „{title}“ gelöscht.", name=res.user.name, title=res.book.title),
                  "success")
    return _back(url_for("books.detail", book_id=res.book_id))


@bp.route("/ausleihen/<int:loan_id>/erinnern", methods=["POST"])
@librarian_required
def remind(loan_id):
    loan = get_by_id(Loan, loan_id) or abort(404)
    if not current_user.can_manage(loan.book):
        abort(403)
    if loan.returned_at:
        flash(_("Diese Ausleihe ist bereits abgeschlossen."), "info")
        return _back(url_for("loans.index"))
    ok, msg = send_manual_reminder(loan)
    flash(_("Erinnerung an {email} gesendet.", email=loan.user.email) if ok
          else _("Keine Erinnerung gesendet: {reason}", reason=msg),
          "success" if ok else "error")
    return _back(url_for("loans.index"))


@bp.route("/ausleihen")
@login_required
def index():
    view = request.args.get("ansicht", "aktiv")
    q = (request.args.get("q") or "").strip()
    query = Loan.query.join(Book).join(User, Loan.user_id == User.id)
    if current_user.is_librarian and request.args.get("nur") != "meine":
        query = query.filter(Loan.book_id.in_(manageable_books_query(current_user).with_entities(Book.id)))
        scope = "alle"
    else:
        query = query.filter(Loan.user_id == current_user.id)
        scope = "meine"
    if view == "ueberfaellig":
        query = query.filter(Loan.returned_at.is_(None), Loan.due_at < date.today()).order_by(Loan.due_at)
    elif view == "verlauf":
        query = query.filter(Loan.returned_at.isnot(None)).order_by(Loan.returned_at.desc())
    else:
        view = "aktiv"
        query = query.filter(Loan.returned_at.is_(None)).order_by(Loan.due_at)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Book.title.ilike(like), Book.authors.ilike(like), Book.isbn.ilike(like),
                                 User.username.ilike(like), User.display_name.ilike(like)))
    total = query.order_by(None).count()
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(max(1, request.args.get("seite", 1, type=int)), pages)
    loans = (query.options(contains_eager(Loan.book), contains_eager(Loan.user))
             .order_by(Loan.id).offset((page - 1) * PER_PAGE).limit(PER_PAGE).all())
    from ..mailer import mail_active
    my_reservations = reservations.open_of(current_user) if scope == "meine" and reservations.enabled() else []
    return render_template("loans/index.html", loans=loans, view=view, q=q, scope=scope, mail_on=mail_active(),
                           my_reservations=my_reservations, positions={r.id: reservations.position(r)
                                                                       for r in my_reservations},
                           page=page, pages=pages, total=total,
                           self_return=get_bool("self_return"), self_extend=get_bool("self_extend"),
                           max_ext=get_int("max_extensions", 2))


# ---------------------------------------------------------------- Circulation desk

def _like_escape(text: str) -> str:
    """Treat % and _ in search terms literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _find_books(code: str, exact_only: bool = False):
    """Books for a code. exact_only: only call number, ISBN or "#number" – for the desk automation, which
    must never lend or check in based on an approximate match."""
    code = (code or "").strip()
    if not code:
        return []
    base = manageable_books_query(current_user)
    # Order: call number (exact, case-insensitive) → ISBN → "#12" as internal book number → title → author etc.
    # Pure digits are often call numbers or inventory numbers and are never interpreted as a book number.
    # SQLite's lower() only handles A–Z – hence the additional exact comparison (e.g. "Ü-12")
    found = base.filter(or_(Book.signature == code, func.lower(Book.signature) == code.lower())).limit(20).all()
    if found:
        return found
    clean = isbnlib.clean_isbn(code)
    if len(clean) in (10, 13):
        found = base.filter(Book.isbn == isbnlib.to_isbn13(clean)).all()
        if found:
            return found
    if code.startswith("#") and parse_id(code[1:]):
        found = base.filter(Book.id == parse_id(code[1:])).all()
        if found:
            return found
    if exact_only:
        return []
    like = f"%{_like_escape(code)}%"
    found = base.filter(Book.title.ilike(like, escape="\\")).order_by(Book.title).limit(20).all()
    if not found:   # also author, subtitle and tags
        found = base.filter(or_(Book.authors.ilike(like, escape="\\"), Book.subtitle.ilike(like, escape="\\"),
                                Book.tags.ilike(like, escape="\\"))).order_by(Book.title).limit(20).all()
    return found


@bp.route("/theke", methods=["GET", "POST"])
@librarian_required
def desk():
    mode = request.values.get("modus", "ausleihe")
    code = (request.values.get("code") or "").strip()
    user_q = (request.values.get("benutzer") or "").strip()[:200]
    borrower = None      # User or people.Candidate (AD person, not yet created)
    if user_q:
        borrower = people.find(user_q)
        if borrower is None:
            like = f"%{_like_escape(user_q)}%"
            hits = User.query.filter(User.active.is_(True),
                                     or_(User.display_name.ilike(like, escape="\\"),
                                         User.username.ilike(like, escape="\\"))).limit(2).all()
            borrower = hits[0] if len(hits) == 1 else None
        if borrower is None:
            flash(_("Benutzer „{query}“ nicht eindeutig gefunden.", query=user_q), "error")

    books, loans = [], []
    code_flashed = False
    if request.method == "POST":
        if mode == "rueckgabe" and request.form.get("loan_id"):
            loan = get_by_id(Loan, parse_id(request.form.get("loan_id")) or 0) or abort(404)
            if not current_user.can_manage(loan.book):
                abort(403)
            if loan.returned_at is None:
                flash(return_loan(loan), "success")
            return redirect(url_for("loans.desk", modus="rueckgabe"))
        if mode == "ausleihe" and request.form.get("book_id") and borrower:
            book = get_by_id(Book, parse_id(request.form.get("book_id")) or 0) or abort(404)
            if not current_user.can_manage(book):
                abort(403)
            ok, msg, _person = people.lend_to(book, borrower)
            flash(msg, "success" if ok else "error")
            return redirect(url_for("loans.desk", modus="ausleihe", benutzer=borrower.username))

    if code:
        books = _find_books(code)
        if mode == "ausleihe":   # Reference-only items are not even offered for lending at the desk
            present = [b for b in books if not b.lendable]
            books = [b for b in books if b.lendable]
            if present and not books:
                b = present[0]
                flash(_("„{title}“ gehört zum Präsenzbestand (Gruppe „{group}“) und wird nicht verliehen.",
                        title=b.title, group=b.group.name), "error")
                code_flashed = True
        auto = request.method == "POST" and request.values.get("auto") == "1"
        exact = _find_books(code, exact_only=True) if auto else []
        if mode == "rueckgabe":
            loans = [l for b in books for l in b.active_loans]
            exact_loans = [l for b in exact for l in b.active_loans]
            if auto and len(exact_loans) == 1:
                flash(return_loan(exact_loans[0]), "success")
                return redirect(url_for("loans.desk", modus="rueckgabe", auto="1"))
        elif mode == "ausleihe" and borrower and auto and len(exact) == 1:
            ok, msg, _person = people.lend_to(exact[0], borrower)
            flash(msg, "success" if ok else "error")
            return redirect(url_for("loans.desk", modus="ausleihe", benutzer=borrower.username, auto="1"))
        if not books and not code_flashed:
            flash(_("Kein Buch zu „{code}“ gefunden.", code=code), "error")

    # Group librarians only see loans and groups they manage themselves
    managed = {g.id for g in current_user.manageable_groups()}
    managed_names = {g.name for g in current_user.manageable_groups()}
    if isinstance(borrower, people.Candidate):
        borrower_loans = []
        borrower_groups = [n for n in borrower.group_names if n in managed_names]
    else:
        borrower_loans = [l for l in borrower.active_loans if current_user.can_manage(l.book)] if borrower else []
        borrower_groups = [g.name for g in borrower.groups if g.id in managed] if borrower else []
    borrower_ready = []    # reservations held for the person – lend directly here
    if borrower is not None and not isinstance(borrower, people.Candidate) and reservations.enabled():
        borrower_ready = [r for r in reservations.ready_for(borrower) if current_user.can_manage(r.book)]
    return render_template("loans/desk.html", mode=mode, code=code, books=books, loans=loans,
                           borrower=borrower, borrower_loans=borrower_loans, borrower_ready=borrower_ready,
                           is_candidate=isinstance(borrower, people.Candidate),
                           borrower_groups=borrower_groups,
                           user_q=user_q,
                           auto=request.form.get("auto", "") if request.method == "POST" else request.args.get("auto", "1"))


@bp.route("/api/personen")
@librarian_required
def api_people():
    """Person search for the desk and "Lend to user" – local and in Active Directory."""
    from flask import jsonify
    return jsonify(people.search(request.args.get("q", ""), directory=request.args.get("verzeichnis") != "0"))


@bp.route("/api/bestand")
@librarian_required
def api_stock():
    """Collection search at the desk: title, author, ISBN, call number, tag – own collection only."""
    from flask import jsonify
    q = (request.args.get("q") or "").strip()[:100]
    mode = request.args.get("modus", "ausleihe")
    if len(q) < 2:
        return jsonify({"results": [], "more": False})
    like = f"%{_like_escape(q)}%"
    clean = isbnlib.clean_isbn(q)
    isbn13 = isbnlib.to_isbn13(clean) if len(clean) in (10, 13) else None
    from ..models import occupied_counts
    on_loan = occupied_counts()          # loans plus copies held for reservations
    conds = [Book.title.ilike(like, escape="\\"), Book.subtitle.ilike(like, escape="\\"),
             Book.authors.ilike(like, escape="\\"), Book.tags.ilike(like, escape="\\"),
             Book.signature.ilike(like, escape="\\"), Book.isbn.ilike(like, escape="\\")]
    if isbn13:
        conds.append(Book.isbn == isbn13)
    base = (manageable_books_query(current_user)
            .outerjoin(on_loan, on_loan.c.book_id == Book.id)
            .add_columns(func.coalesce(on_loan.c.n, 0)))
    if mode != "rueckgabe":   # Reference-only items are not even offered for lending
        base = base.filter(lendable_books())
    # Fetch exact matches (call number, ISBN) first and regardless of the limit – otherwise e.g. call number
    # "77" would drop off the list with many partial matches
    exact_conds = [Book.signature == q, func.lower(Book.signature) == q.lower()]
    if isbn13:
        exact_conds.append(Book.isbn == isbn13)
    exact_rows = base.filter(or_(*exact_conds)).order_by(Book.title).limit(12).all()
    exact_ids = {b.id for b, _n in exact_rows}
    rows = base.filter(or_(*conds)).filter(~Book.id.in_(exact_ids or {0})).order_by(Book.title).limit(60).all()
    # At the desk with a selected person: books they may not borrow or already have go to the end
    name = (request.args.get("benutzer") or "").strip()[:200]
    borrower = people.find(name, cached=True) if name and mode != "rueckgabe" else None
    has = set()
    if borrower is not None and not isinstance(borrower, people.Candidate):
        has = {l.book_id for l in borrower.active_loans}

    def allowed(book):
        return borrower is None or borrower.can_borrow(book)

    def rank(row):
        book, n = row
        free = max(0, (book.copies or 0) - n)
        useful = (n > 0) if mode == "rueckgabe" else (free > 0 and allowed(book) and book.id not in has)
        return (not useful, (book.title or "").lower())
    rows.sort(key=rank)
    shown = [(b, n, True) for b, n in exact_rows] + [(b, n, False) for b, n in rows]
    results = []
    for book, n, exact in shown[:12]:
        results.append({"id": book.id, "title": book.title, "authors": book.authors or "", "isbn": book.isbn or "",
                        "signature": book.signature or "", "location": book.location or "",
                        "group": book.group.name if book.group else "", "copies": book.copies or 0,
                        "available": max(0, (book.copies or 0) - n), "on_loan": n, "exact": exact,
                        "has": book.id in has,
                        "allowed": allowed(book) if mode != "rueckgabe" else True})
    return jsonify({"results": results, "more": len(shown) > 12})
