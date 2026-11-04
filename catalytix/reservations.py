"""Reservations: queue per book, holding with a pickup deadline.

Process:
- If all copies are on loan (or held), a book can be reserved and the person joins the queue.
- When a copy becomes available (return, more copies, reservation cancelled or expired), it is held for the first
  person (status "ready", until hold_until) and they get an e-mail. For everyone else it counts as
  unavailable.
- If the person borrows the book, the reservation is done. If they don't pick it up, the reservation expires after
  the pickup deadline and it's the next person's turn.

All changes to a book's queue run under the same lock as lending (lock_book),
so that concurrent returns, loans and reservations don't get in each other's way.
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
    """Position in the queue (1 = next in line); held reservations don't count."""
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
    """Close the reservation if it is still open. Returns True if this request closed it."""
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
    """Part of lending (same transaction, book already locked): the person's open reservation is done."""
    db.session.execute(
        update(Reservation).where(Reservation.book_id == book_id, Reservation.user_id == user_id,
                                  Reservation.status.in_((RES_WAITING, RES_READY)))
        .values(status=RES_DONE, closed_at=datetime.now()).execution_options(synchronize_session=False))


def waiting_ahead(book_id: int, user_id: int | None) -> int:
    """How many waiting people are ahead of this person (all, if they are not in the queue)?"""
    if not enabled():
        return 0
    if db.session.query(Reservation.id).filter(Reservation.book_id == book_id, Reservation.user_id == user_id,
                                               Reservation.status == RES_READY).first():
        return 0                  # held for the person – it's their turn
    waiting = [uid for (uid,) in db.session.query(Reservation.user_id).filter(
        Reservation.book_id == book_id, Reservation.status == RES_WAITING)
        .order_by(Reservation.created_at, Reservation.id)]
    return waiting.index(user_id) if user_id in waiting else len(waiting)


def held_by_others(book_id: int, user_id: int | None) -> int:
    q = db.session.query(func.count(Reservation.id)).filter(Reservation.book_id == book_id,
                                                            Reservation.status == RES_READY)
    if user_id is not None:
        q = q.filter(Reservation.user_id != user_id)
    return q.scalar() or 0


def still_valid(res: Reservation) -> bool:
    """May the person (still) get the book? Locked, no longer in the book's group or book is
    reference-only stock ("Präsenzbestand"): then the reservation only blocks a copy they can never pick up."""
    return bool(res.user.active and res.book.lendable and res.user.can_borrow(res.book))


def _drop_invalid(book_id: int) -> int:
    """End a book's open reservations that no longer apply (book is already locked, no commit)."""
    n = 0
    for res in Reservation.query.filter(Reservation.book_id == book_id,
                                        Reservation.status.in_((RES_WAITING, RES_READY))).all():
        if not still_valid(res):
            res.status, res.closed_at = RES_CANCELLED, datetime.now()
            n += 1
    if n:
        db.session.flush()
        log.info("Reservations: %d for book %s no longer valid (account, group or reference-only) – closed",
                 n, book_id)
    return n


def promote(book_id: int) -> list[Reservation]:
    """Assign available copies to the queue (hold them and notify)."""
    if not enabled():
        return []
    _lock(book_id)
    _drop_invalid(book_id)
    free = _free_copies(book_id)
    readied = []
    if free > 0:
        waiting = (Reservation.query.filter(Reservation.book_id == book_id, Reservation.status == RES_WAITING)
                   .order_by(Reservation.created_at, Reservation.id).all())
        until = date.today() + timedelta(days=hold_days())
        for res in waiting:
            if len(readied) >= free:
                break
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
    """After the number of copies changed: put excess holds (fewer copies) back into the queue
    – most recently held first, they keep their position –, otherwise assign available copies."""
    _lock(book_id)
    _drop_invalid(book_id)
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
    """Expire uncollected reservations and move the next person up."""
    today = today or date.today()
    due = Reservation.query.filter(Reservation.status == RES_READY, Reservation.hold_until < today).all()
    books = set()
    for res in due:
        if _close(res, RES_EXPIRED):
            books.add(res.book_id)
    db.session.commit()
    books |= revalidate()
    for book_id in books:
        promote(book_id)
    if books:
        log.info("Reservations: %d not collected in time – next in line notified", len(due))
    return len(books)


def revalidate(user_ids=None) -> set[int]:
    """Check open reservations that no longer apply (account locked, group left – also through the
    AD sync –, book moved or reference-only stock) and move the next person up.
    Runs regularly in the background and immediately after changes by admins. Returns the affected books."""
    if not enabled():
        return set()
    q = Reservation.query.filter(Reservation.status.in_((RES_WAITING, RES_READY)))
    if user_ids is not None:
        ids = [i for i in user_ids if i]
        if not ids:
            return set()
        q = q.filter(Reservation.user_id.in_(ids))
    books = {res.book_id for res in q.all() if not still_valid(res)}
    for book_id in books:
        promote(book_id)          # ends the invalid ones under the lock and reassigns the copy
    return books


def promote_after_delete(book_ids) -> None:
    """After deleting an account: assign its held copies to the next people."""
    for book_id in set(book_ids):
        promote(book_id)


def cancel_for_group(group_id: int) -> int:
    """Group becomes reference-only stock: end open reservations of its books (no commit – the caller saves)."""
    ids = db.session.query(Book.id).filter(Book.group_id == group_id)
    n = db.session.execute(
        update(Reservation).where(Reservation.book_id.in_(ids), Reservation.status.in_((RES_WAITING, RES_READY)))
        .values(status=RES_CANCELLED, closed_at=datetime.now()).execution_options(synchronize_session=False))
    return n.rowcount or 0


def cancel_all() -> int:
    """When switching off: end all open reservations (otherwise copies would stay held)."""
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
