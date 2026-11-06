"""
Clear the test data out before the shop opens to the public.

THIS DELETES THINGS AND CANNOT BE UNDONE.

WHAT GOES
---------
Every order. All thirty of them trace back to the team or to a Rs 1 payment
test - Test Customer, @caratsense.in accounts, Shriya's own trial orders -
and none to a paying member of the public. Starting at #1 on launch day is
worth more than keeping them.

The seeded test staff (Baker One, Baker Two, Rider One), the test customers,
and the team's own customer accounts. The real staff - Shreyansh Kashyap and
Anshul - are NOT in the manifest and the script will not touch them.

WHAT STAYS
----------
Shriya's admin account. The real baker and rider. The catalogue, the delivery
zones, the settings, the WhatsApp message history.

Kumar's account stays too, demoted from admin to customer: the history is
worth keeping and it is one line to put back. His number stops being greeted
as an admin by the bot.

SAFETY
------
Nothing is deleted by name alone. Every account in the manifest is checked
against the id AND the name it was surveyed under, and the script refuses to
run if a single one disagrees - because an id that has come to mean somebody
else is exactly how a cleanup script deletes a real customer.

It prints every order and account as JSON before touching anything, so the
dry run IS the backup: capture it to a file.

USAGE
-----
    railway ssh --service copa-backend "python -m scripts.go_live_cleanup" > backup.json
    railway ssh --service copa-backend "python -m scripts.go_live_cleanup --apply"
"""

import json
import sys

from app.db import SessionLocal
from app.models.address import Address
from app.models.cake_enquiry import CakeEnquiry
from app.models.order import Order
from app.models.review import Review
from app.models.shift import Shift
from app.models.trusted_device import TrustedDevice
from app.models.user import User, UserRole

# (id, the name it was surveyed under). The name is a checksum on the id, not
# a lookup: if the two ever disagree the database is not the one this manifest
# was written against, and nothing should be deleted.
DOOMED = [
    (2, "Test Customer"),
    (3, "Baker One"),
    (4, "Baker Two"),
    (5, "Rider One"),
    (6, "sneha"),
    (9, "Akshat Tiwary"),
    (14, "kavya"),
    (15, "Kavya Kauntia"),
    (16, "Hi Cake O'Clock"),      # a WhatsApp message that became an account
    (17, "Test"),
    (18, "Dhruv Kaushik"),
    (19, "vk"),
    (20, "Vedant Kothari"),
]

# Kept, but no longer an admin.
DEMOTE = (8, "Kumar Gaurav")

# Named so a reader can see they were considered and spared.
KEEP_STAFF = {1: "Shriya Mahendru (admin)", 12: "Shreyansh Kashyap (baker)",
              13: "Anshul (rider)"}


def main() -> int:
    apply = "--apply" in sys.argv
    db = SessionLocal()

    try:
        users = {u.id: u for u in db.query(User).all()}

        # ── refuse to touch a database this manifest was not written for ──
        problems = []
        for uid, expected in DOOMED + [DEMOTE]:
            found = users.get(uid)
            if found is None:
                problems.append(f"  #{uid} is gone - expected {expected!r}")
            elif not (found.name or "").startswith(expected):
                problems.append(
                    f"  #{uid} is {found.name!r}, manifest says {expected!r}")
        if problems:
            print("REFUSING TO RUN. The database does not match the manifest:\n")
            print("\n".join(problems))
            print("\nNothing was deleted. Re-survey and update DOOMED.")
            return 1

        orders = db.query(Order).order_by(Order.id).all()

        # ── the backup, printed before anything is touched ──
        if not apply:
            print(json.dumps({
                "orders": [
                    {c.name: str(getattr(o, c.name)) for c in Order.__table__.columns}
                    for o in orders
                ],
                "users": [
                    {c.name: str(getattr(u, c.name)) for c in User.__table__.columns}
                    for u in users.values()
                ],
            }, indent=1, default=str))
            print()

        doomed_ids = [uid for uid, _ in DOOMED]
        print(f"Orders to delete .............. {len(orders)}")
        print(f"Accounts to delete ............ {len(doomed_ids)}")
        for uid, name in DOOMED:
            print(f"    #{uid:<3} {name}")
        print(f"Account to demote to customer . #{DEMOTE[0]} {DEMOTE[1]}"
              f"  (currently {users[DEMOTE[0]].role})")
        print("Accounts kept untouched .......")
        for uid, who in KEEP_STAFF.items():
            print(f"    #{uid:<3} {who}")

        if not apply:
            print("\nDRY RUN - nothing was written. The JSON above is your backup.")
            print("Re-run with --apply.")
            return 0

        # ── orders first: everything else references them ──
        # Deleted one at a time rather than with a bulk query, so the
        # cascades configured on the relationships (items, events,
        # attachments) actually run.
        for order in orders:
            db.delete(order)
        db.flush()

        # ── what hangs off a user ──
        for model in (Address, Review, Shift, TrustedDevice):
            gone = (db.query(model)
                      .filter(model.user_id.in_(doomed_ids))
                      .delete(synchronize_session=False))
            if gone:
                print(f"  removed {gone} {model.__tablename__} row(s)")

        # An enquiry survives its author: it is a conversation the shop had,
        # and the column is nullable for exactly this reason.
        orphaned = (db.query(CakeEnquiry)
                      .filter(CakeEnquiry.user_id.in_(doomed_ids))
                      .update({"user_id": None}, synchronize_session=False))
        if orphaned:
            print(f"  detached {orphaned} cake enquiry row(s) from deleted accounts")

        for uid in doomed_ids:
            db.delete(users[uid])

        users[DEMOTE[0]].role = UserRole.CUSTOMER

        db.commit()
        print("\nApplied.")

        print(f"  orders remaining: {db.query(Order).count()}")
        print(f"  users remaining:  {db.query(User).count()}")
        admins = db.query(User).filter(User.role == UserRole.ADMIN).all()
        print(f"  admins:           {[u.name for u in admins]}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
