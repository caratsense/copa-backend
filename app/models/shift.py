from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from app.db.base import Base


class Shift(Base):
    """
    One stretch of a baker's or rider's day: when they came in, when they left.

    Before this there was a single boolean, `users.on_duty`, and nothing else.
    It could say whether somebody was in the building right now and that was
    all — no arrival time, no record of yesterday, nothing to answer "what time
    did he get in?" the morning after a cake went out late. The duty toggle was
    also the only way to set it, so being present was something an admin ticked
    on a staff member's behalf rather than something that person did.

    A shift with clocked_out_at still NULL is the one in progress. There is at
    most one open per person, enforced in the service rather than by a partial
    unique index, because SQLite (which the test suite runs on) does not have
    them and a rule only the live database enforces is a rule that is never
    tested.
    """
    __tablename__ = "shifts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    clocked_in_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    clocked_out_at = Column(DateTime(timezone=True), nullable=True)
    # "self" when the person pressed the button, "admin" when somebody closed
    # it for them, "stale" when it was closed by the shop's own cutoff. Worth
    # keeping: a shift closed by the cutoff is a guess at when they left, and a
    # roster that cannot tell the difference presents that guess as a fact.
    closed_by = Column(String, nullable=True)
