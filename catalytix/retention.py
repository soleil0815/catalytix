"""Aufbewahrungsfristen (DSGVO: Speicherbegrenzung, Art. 5 Abs. 1 e).

Abgeschlossene Ausleihen werden nach der eingestellten Frist (Standard 12 Monate nach der Rückgabe)
gelöscht. Pro Buch bleibt nur die Anzahl erhalten (Book.archived_loans), damit die Statistik stimmt.
Einträge im Mail-Protokoll werden nach derselben Frist gelöscht – außer solche zu laufenden Ausleihen,
denn daran erkennt Catalytix, wie oft schon gemahnt wurde.

Laufende und überfällige Ausleihen werden nie gelöscht.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .models import Book, Loan, MailLog, db, get_int, set_setting

log = logging.getLogger(__name__)
BATCH = 500


def retention_months() -> int:
    return max(0, get_int("retention_months", 12))


def cutoff_date(now: datetime | None = None, months: int | None = None) -> datetime | None:
    months = retention_months() if months is None else months
    if months <= 0:
        return None
    return (now or datetime.now()) - timedelta(days=round(months * 365.25 / 12))


def archive_loans(loan_ids: list[int]) -> int:
    """Ausleihen löschen und pro Buch mitzählen. Erwartet nur abgeschlossene Ausleihen."""
    total = 0
    for i in range(0, len(loan_ids), BATCH):
        ids = loan_ids[i:i + BATCH]
        per_book = (db.session.query(Loan.book_id, func.count(Loan.id))
                    .filter(Loan.id.in_(ids), Loan.returned_at.isnot(None)).group_by(Loan.book_id).all())
        for book_id, n in per_book:
            db.session.query(Book).filter(Book.id == book_id).update(
                {Book.archived_loans: func.coalesce(Book.archived_loans, 0) + n}, synchronize_session=False)
        db.session.query(MailLog).filter(MailLog.loan_id.in_(ids)).update(
            {MailLog.loan_id: None}, synchronize_session=False)
        total += db.session.query(Loan).filter(Loan.id.in_(ids), Loan.returned_at.isnot(None)).delete(
            synchronize_session=False)
    return total


def delete_user(user) -> None:
    """Benutzer samt abgeschlossenen Ausleihen löschen (pro Buch nur gezählt, Statistik bleibt).

    Der Aufrufer prüft vorher, dass keine Ausleihe mehr läuft; committet wird nicht."""
    ids = [i for (i,) in db.session.query(Loan.id).filter(Loan.user_id == user.id)]
    archive_loans(ids)
    db.session.query(Loan).filter(Loan.user_id == user.id).delete(synchronize_session=False)
    # schon geladene Ausleihen aus der Sitzung nehmen – sonst will das ORM sie beim Löschen noch ändern
    for obj in [o for o in db.session.identity_map.values() if isinstance(o, Loan) and o.id in set(ids)]:
        db.session.expunge(obj)
    db.session.expire(user, ["loans"])
    db.session.delete(user)


def run_retention(now: datetime | None = None) -> dict:
    """Fristen anwenden. Liefert {'loans': n, 'mails': m} bzw. {'off': True}."""
    cutoff = cutoff_date(now)
    if cutoff is None:
        return {"off": True, "loans": 0, "mails": 0}
    old_ids = [i for (i,) in db.session.query(Loan.id).filter(Loan.returned_at.isnot(None),
                                                             Loan.returned_at < cutoff)]
    loans = archive_loans(old_ids)
    active = select(Loan.id).where(Loan.returned_at.is_(None))
    mails = (db.session.query(MailLog)
             .filter(MailLog.created_at < cutoff,
                     (MailLog.loan_id.is_(None)) | (MailLog.loan_id.notin_(active)))
             .delete(synchronize_session=False))
    db.session.commit()
    if loans or mails:
        log.info("Retention: %d old loan(s) and %d mail log entries deleted", loans, mails)
    return {"loans": loans, "mails": mails}


def format_result(res: dict) -> str:
    if res.get("off"):
        return "Aufbewahrungsfrist ist ausgeschaltet."
    return f"Gelöscht: {res['loans']} alte Ausleihe(n), {res['mails']} Einträge im E-Mail-Protokoll"


def display_message(msg: str) -> str:
    """Gespeichertes Ergebnis (deutsch) in der aktuellen Sprache."""
    import re
    from .i18n import _
    m = re.match(r"^Gelöscht: (\d+) alte Ausleihe\(n\), (\d+) Einträge im E-Mail-Protokoll$", msg)
    if m:
        return _("Gelöscht: {loans} alte Ausleihe(n), {mails} Einträge im E-Mail-Protokoll",
                 loans=m.group(1), mails=m.group(2))
    return _(msg)


def run_daily(now: datetime | None = None) -> dict | None:
    """Einmal pro Tag (auch bei mehreren Server-Prozessen nur einmal)."""
    from .mailer import _claim_today
    now = now or datetime.now()
    if not _claim_today(now.date().isoformat(), key="retention_last_run"):
        return None
    res = run_retention(now)
    set_setting("retention_last_result", f"{now:%d.%m.%Y %H:%M}: {format_result(res)}")
    db.session.commit()
    return res
