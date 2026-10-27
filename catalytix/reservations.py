"""Vormerkungen (Reservierungen): Warteschlange je Buch, Bereitlegen mit Abholfrist.

Ablauf:
- Sind alle Exemplare verliehen (oder zurückgelegt), kann man ein Buch vormerken und steht in der Warteschlange.
- Wird ein Exemplar frei (Rückgabe, mehr Exemplare, Vormerkung storniert oder abgelaufen), wird es für die erste
  Person zurückgelegt (Status „ready“, bis hold_until) und sie bekommt eine E-Mail. Für alle anderen gilt es als
  nicht verfügbar.
- Leiht die Person das Buch aus, ist die Vormerkung erledigt. Holt sie es nicht ab, läuft die Vormerkung nach der
  Abholfrist ab und die nächste Person ist dran.

Alle Änderungen an der Warteschlange eines Buchs laufen unter derselben Sperre wie die Ausleihe (lock_book),
damit gleichzeitige Rückgaben, Ausleihen und Vormerkungen sich nicht in die Quere kommen.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, update

from .i18n import _
from .models import (RES_CANCELLED, RES_DONE, RES_EXPIRED, RES_READY, RES_WAITING, Book, Loan, Reservation, User,
                     db, get_bool, get_int)

log = logging.getLogger(__name__)


def enabled() -> bool:
    return get_bool("reservations_enabled")


def hold_days() -> int:
    return max(1, min(get_int("reservation_hold_days", 7), 60))


def open_for(user, book) -> Reservation | None:
    uid = getattr(user, "id", None)
    return next((r for r in book.reservations if r.user_id == uid and r.is_open), None)


def position(res: Reservation) -> int:
    """Platz in der Warteschlange (1 = als Nächstes dran); bereitgelegte zählen nicht mit."""
    waiting = [r.id for r in res.book.reservations if r.status == RES_WAITING]
    return waiting.index(res.id) + 1 if res.id in waiting else 0


def count_open(user_id: int) -> int:
    return db.session.query(func.count(Reservation.id)).filter(
        Reservation.user_id == user_id, Reservation.status.in_((RES_WAITING, RES_READY))).scalar() or 0


def _lock(book_id: int) -> None:
    from .views.loans import lock_book
    lock_book(book_id)


def _free_copies(book_id: int) -> int:
    copies = db.session.query(Book.copies).filter(Book.id == book_id).scalar() or 0
    lent = db.session.query(func.count(Loan.id)).filter(Loan.book_id == book_id, Loan.returned_at.is_(None)).scalar()
    held = db.session.query(func.count(Reservation.id)).filter(Reservation.book_id == book_id,
                                                               Reservation.status == RES_READY).scalar()
    return copies - (lent or 0) - (held or 0)


def reserve(book: Book, user: User) -> tuple[bool, str]:
    if not enabled():
        return False, _("Vormerkungen sind nicht eingeschaltet.")
    if not book.lendable:
        return False, _("„{title}“ gehört zum Präsenzbestand (Gruppe „{group}“) und wird nicht verliehen.",
                        title=book.title, group=book.group.name)
    if not user.active:
        return False, _("{name} ist gesperrt.", name=user.name)
    if not user.can_borrow(book):
        return False, _("Dieses Buch darfst du nicht ausleihen – deshalb auch nicht vormerken.")
    limit = get_int("reservation_max", 5)
    try:
        _lock(book.id)
        if db.session.query(Loan.id).filter(Loan.book_id == book.id, Loan.user_id == user.id,
                                            Loan.returned_at.is_(None)).first():
            db.session.rollback()
            return False, _("Du hast dieses Buch bereits ausgeliehen.")
        if db.session.query(Reservation.id).filter(Reservation.book_id == book.id, Reservation.user_id == user.id,
                                                   Reservation.status.in_((RES_WAITING, RES_READY))).first():
            db.session.rollback()
            return False, _("Du hast dieses Buch bereits vorgemerkt.")
        if _free_copies(book.id) > 0:
            db.session.rollback()
            return False, _("Es ist gerade ein Exemplar frei – du kannst das Buch direkt ausleihen.")
        if limit and count_open(user.id) >= limit:
            db.session.rollback()
            return False, _("Du hast bereits {n} Bücher vorgemerkt – mehr sind nicht möglich.", n=limit)
        db.session.add(Reservation(book_id=book.id, user_id=user.id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    db.session.refresh(book)
    res = open_for(user, book)
    return True, _("„{title}“ ist vorgemerkt – du bist auf Platz {n}. Wir melden uns, sobald ein Exemplar frei ist.",
                   title=book.title, n=position(res) if res else 1)


def _close(res: Reservation, status: str) -> bool:
    """Vormerkung abschließen, falls sie noch offen ist. Liefert True, wenn diese Anfrage sie abgeschlossen hat."""
    done = db.session.execute(
        update(Reservation).where(Reservation.id == res.id, Reservation.status.in_((RES_WAITING, RES_READY)))
        .values(status=status, closed_at=datetime.now()).execution_options(synchronize_session=False))
    return done.rowcount == 1


def cancel(res: Reservation) -> bool:
    book_id = res.book_id
    _lock(book_id)
    changed = _close(res, RES_CANCELLED)
    db.session.commit()
    if changed:
        promote(book_id)
    return changed


def fulfil_on_loan(book_id: int, user_id: int) -> None:
    """Teil der Ausleihe (gleiche Transaktion, Buch schon gesperrt): offene Vormerkung der Person ist erledigt."""
    db.session.execute(
        update(Reservation).where(Reservation.book_id == book_id, Reservation.user_id == user_id,
                                  Reservation.status.in_((RES_WAITING, RES_READY)))
        .values(status=RES_DONE, closed_at=datetime.now()).execution_options(synchronize_session=False))


def held_by_others(book_id: int, user_id: int | None) -> int:
    q = db.session.query(func.count(Reservation.id)).filter(Reservation.book_id == book_id,
                                                            Reservation.status == RES_READY)
    if user_id is not None:
        q = q.filter(Reservation.user_id != user_id)
    return q.scalar() or 0


def promote(book_id: int) -> list[Reservation]:
    """Freie Exemplare an die Warteschlange vergeben (zurücklegen und benachrichtigen)."""
    if not enabled():
        return []
    _lock(book_id)
    free = _free_copies(book_id)
    readied = []
    if free > 0:
        waiting = (Reservation.query.filter(Reservation.book_id == book_id, Reservation.status == RES_WAITING)
                   .order_by(Reservation.created_at, Reservation.id).all())
        until = date.today() + timedelta(days=hold_days())
        for res in waiting:
            if len(readied) >= free:
                break
            if not res.user.active:          # gesperrte Person: Vormerkung entfällt
                res.status, res.closed_at = RES_CANCELLED, datetime.now()
                continue
            res.status, res.ready_at, res.hold_until = RES_READY, datetime.now(), until
            readied.append(res)
    db.session.commit()
    for res in readied:
        from .mailer import notify_reservation_ready
        notify_reservation_ready(res)
        log.info("Reservation %s: copy of book %s held for user %s until %s", res.id, book_id, res.user_id,
                 res.hold_until)
    return readied


def rebalance(book_id: int) -> None:
    """Nach einer Änderung der Exemplarzahl: zu viele Zurücklegungen (weniger Exemplare) wieder in die Warteschlange
    stellen – die zuletzt bereitgelegten zuerst, sie behalten ihren Platz –, sonst freie Exemplare vergeben."""
    _lock(book_id)
    excess = -_free_copies(book_id)
    if excess > 0:
        ready = (Reservation.query.filter(Reservation.book_id == book_id, Reservation.status == RES_READY)
                 .order_by(Reservation.ready_at.desc(), Reservation.id.desc()).limit(excess).all())
        for res in ready:
            res.status, res.ready_at, res.hold_until = RES_WAITING, None, None
        db.session.commit()
        return
    db.session.commit()
    promote(book_id)


def expire_due(today: date | None = None) -> int:
    """Nicht abgeholte Vormerkungen ablaufen lassen und die nächste Person nachrücken lassen."""
    today = today or date.today()
    due = Reservation.query.filter(Reservation.status == RES_READY, Reservation.hold_until < today).all()
    books = set()
    for res in due:
        if _close(res, RES_EXPIRED):
            books.add(res.book_id)
    db.session.commit()
    for book_id in books:
        promote(book_id)
    if books:
        log.info("Reservations: %d not collected in time – next in line notified", len(due))
    return len(books)


def cancel_for_group(group_id: int) -> int:
    """Gruppe wird Präsenzbestand: offene Vormerkungen ihrer Bücher beenden (ohne Commit – der Aufrufer speichert)."""
    ids = db.session.query(Book.id).filter(Book.group_id == group_id)
    n = db.session.execute(
        update(Reservation).where(Reservation.book_id.in_(ids), Reservation.status.in_((RES_WAITING, RES_READY)))
        .values(status=RES_CANCELLED, closed_at=datetime.now()).execution_options(synchronize_session=False))
    return n.rowcount or 0


def cancel_all() -> int:
    """Beim Ausschalten: alle offenen Vormerkungen beenden (sonst blieben Exemplare zurückgelegt)."""
    n = db.session.execute(
        update(Reservation).where(Reservation.status.in_((RES_WAITING, RES_READY)))
        .values(status=RES_CANCELLED, closed_at=datetime.now()).execution_options(synchronize_session=False))
    db.session.commit()
    return n.rowcount or 0


def ready_for(user) -> list[Reservation]:
    return (Reservation.query.filter(Reservation.user_id == user.id, Reservation.status == RES_READY)
            .order_by(Reservation.hold_until).all())


def open_of(user) -> list[Reservation]:
    return (Reservation.query.filter(Reservation.user_id == user.id,
                                     Reservation.status.in_((RES_WAITING, RES_READY)))
            .order_by(Reservation.created_at).all())
