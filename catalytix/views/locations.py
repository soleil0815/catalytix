"""Define locations – librarians and administrators.

If at least one location is defined, it is picked from a list when adding books instead of typed.
Books still store the location as text; renaming also updates it in all affected books.
"""
from __future__ import annotations

import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..i18n import _, _n
from ..models import Book, Location, db, manageable_books_query
from . import form_str, get_by_id, librarian_required, parse_id

bp = Blueprint("locations", __name__)
MAX_LEN = 200


def _natural(name: str):
    """"Regal 2" before "Regal 10"."""
    return [int(t) if t.isascii() and t.isdecimal() else t.lower() for t in re.split(r"([0-9]+)", name or "")]


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def names() -> list[str]:
    return sorted((l.name for l in Location.query.all()), key=_natural)


def active() -> bool:
    return db.session.query(Location.id).first() is not None


def canonical(value: str) -> str | None:
    """Return the matching defined location (case-insensitive), otherwise None."""
    value = (value or "").strip()
    if not value:
        return ""
    wanted = _norm(value)
    return next((l.name for l in Location.query.all() if _norm(l.name) == wanted), None)


def check(value: str, previous: str = "") -> tuple[str, str | None]:
    """Validate a book's location. Returns (value, error message or None).

    Without defined locations any text is allowed. With a list only its entries – an already
    stored value that is not (yet) listed may remain unchanged."""
    value = (value or "").strip()[:MAX_LEN]
    if not active():
        return value, None
    c = canonical(value)
    if c is not None:
        return c, None
    if value == (previous or "").strip():
        return value, None
    return previous or "", _("Standort „{value}“ ist nicht festgelegt. Bitte aus der Liste wählen.", value=value)


def _usage() -> dict[str, int]:
    """Books per location – for group librarians only their own collection."""
    ids = manageable_books_query(current_user).with_entities(Book.id)
    rows = (db.session.query(Book.location, func.count(Book.id))
            .filter(Book.location.isnot(None), Book.location != "", Book.id.in_(ids))
            .group_by(Book.location).all())
    return {loc: n for loc, n in rows}


def _can_change_all(location: str) -> bool:
    """May the user change all books with this location? (group librarians only their own)"""
    total = Book.query.filter(Book.location == location).count()
    mine = manageable_books_query(current_user).filter(Book.location == location).count()
    return total == mine


def _books(n: int) -> str:
    return _n("{n} Buch", "{n} Bücher", n)


def _move_books(old: str, new: str) -> int:
    n = Book.query.filter(Book.location == old).update({Book.location: new}, synchronize_session=False)
    return n


@bp.route("/standorte")
@librarian_required
def index():
    import difflib
    usage = _usage()
    listed = sorted(Location.query.all(), key=lambda l: _natural(l.name))
    exact = {l.name for l in listed}
    by_norm = {_norm(l.name): l for l in listed}
    unlisted = []
    for value, n in sorted(usage.items(), key=lambda x: _natural(x[0])):
        if value in exact:
            continue
        # Suggestion: same spelling except case/whitespace, otherwise the most similar name (typo)
        hit = by_norm.get(_norm(value))
        if hit is None:
            close = difflib.get_close_matches(_norm(value), list(by_norm), n=1, cutoff=0.75)
            hit = by_norm[close[0]] if close else None
        unlisted.append({"value": value, "count": n, "suggest": hit.id if hit else None,
                         "variant": hit is not None and _norm(value) == _norm(hit.name)})
    return render_template("locations.html", locations=listed, usage=usage, unlisted=unlisted)


@bp.route("/standorte/neu", methods=["POST"])
@librarian_required
def create():
    added, skipped = [], []
    existing = {n.lower() for n in names()}
    for line in (request.form.get("names") or "").splitlines():
        name = re.sub(r"\s+", " ", line).strip()[:MAX_LEN]
        if not name:
            continue
        if name.lower() in existing:
            skipped.append(name)
            continue
        db.session.add(Location(name=name, note=form_str("note", 300) if len(added) == 0 else ""))
        existing.add(name.lower())
        added.append(name)
    db.session.commit()
    if added:
        flash(_("{n} Standort(e) angelegt: {names}", n=len(added),
                names=", ".join(added[:8]) + (" …" if len(added) > 8 else "")), "success")
    if skipped:
        flash(_("Schon vorhanden: {names}", names=", ".join(skipped[:8])), "info")
    if not added and not skipped:
        flash(_("Bitte mindestens einen Namen eingeben."), "error")
    return redirect(url_for("locations.index"))


@bp.route("/standorte/<int:loc_id>/umbenennen", methods=["POST"])
@librarian_required
def rename(loc_id):
    loc = get_by_id(Location, loc_id)
    if loc is None:
        return redirect(url_for("locations.index"))
    new = re.sub(r"\s+", " ", form_str("name", MAX_LEN)).strip()
    if not new:
        flash(_("Der Name darf nicht leer sein."), "error")
    elif new.lower() != loc.name.lower() and canonical(new) is not None:
        flash(_("„{new}“ gibt es schon. Zum Zusammenlegen „{old}“ löschen, nachdem die Bücher umgezogen sind.",
                new=new, old=loc.name), "error")
    elif not _can_change_all(loc.name):
        flash(_("„{name}“ wird auch von Büchern anderer Gruppen genutzt – Umbenennen nur durch Administratoren "
                "oder Verwalter ohne Gruppe.", name=loc.name), "error")
    else:
        old = loc.name
        n = _move_books(old, new)
        loc.name = new
        loc.note = form_str("note", 300)
        db.session.commit()
        if n and old != new:
            flash(_("„{old}“ heißt jetzt „{new}“ – {books} angepasst.", old=old, new=new, books=_books(n)), "success")
        else:
            flash(_("„{old}“ heißt jetzt „{new}“.", old=old, new=new), "success")
    return redirect(url_for("locations.index"))


@bp.route("/standorte/<int:loc_id>/loeschen", methods=["POST"])
@librarian_required
def delete(loc_id):
    loc = get_by_id(Location, loc_id)
    if loc is None:
        return redirect(url_for("locations.index"))
    n = Book.query.filter(Book.location == loc.name).count()
    if n:
        flash(_("„{name}“ ist noch bei {books} eingetragen. Erst umbenennen oder die Bücher umstellen.",
                name=loc.name, books=_books(n)), "error")
    else:
        db.session.delete(loc)
        db.session.commit()
        flash(_("Standort „{name}“ gelöscht.", name=loc.name), "success")
    return redirect(url_for("locations.index"))


@bp.route("/standorte/bestand", methods=["POST"])
@librarian_required
def adopt():
    """Adopt a value present in the collection but not defined, or replace it with a location."""
    old = (request.form.get("value") or "")[:MAX_LEN]   # deliberately not stripped: "Regal 1 " with a space is also a typo
    target = request.form.get("target", "")
    count = Book.query.filter(Book.location == old).count() if old else 0
    if not old or not count:
        flash(_("Dieser Eintrag kommt im Bestand nicht (mehr) vor."), "info")
    elif target == "__neu__" and re.sub(r"\s+", " ", old).strip() != old and not _can_change_all(old):
        flash(_("„{name}“ kommt auch bei Büchern anderer Gruppen vor – Ersetzen nur durch Administratoren "
                "oder Verwalter ohne Gruppe.", name=old), "error")
    elif target == "__neu__":
        clean = re.sub(r"\s+", " ", old).strip()
        if canonical(clean) is None:
            db.session.add(Location(name=clean))
        if clean != old:
            _move_books(old, canonical(clean) or clean)
        db.session.commit()
        flash(_("„{name}“ ist jetzt ein festgelegter Standort.", name=clean), "success")
    else:
        loc = get_by_id(Location, parse_id(target) or 0)
        if loc is None:
            flash(_("Bitte einen Standort auswählen."), "error")
        elif not _can_change_all(old):
            flash(_("„{name}“ kommt auch bei Büchern anderer Gruppen vor – Ersetzen nur durch Administratoren "
                    "oder Verwalter ohne Gruppe.", name=old), "error")
        else:
            n = _move_books(old, loc.name)
            db.session.commit()
            flash(_("„{old}“ durch „{new}“ ersetzt – {books} angepasst.", old=old, new=loc.name, books=_books(n)), "success")
    return redirect(url_for("locations.index"))
