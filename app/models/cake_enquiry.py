from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.db.base import Base


class CakeEnquiry(Base):
    """
    A Build-a-Cake request the shop cannot price automatically.

    Two things route here: a cake heavier than the 3.5kg the size ladder sells,
    and any customisation the builder has no rule for. Both have the same
    problem - there is no price the engine could calculate - and the same
    answer: a conversation, not a checkout.

    Deliberately NOT an order. It has no total, no payment and no baker, and
    nothing promotes it into one: turning an enquiry into an order means a
    human agreeing a price and placing that order themselves. That separation
    is the whole point, because the alternative is inventing a number for a
    cake nobody has quoted.

    What it does carry is everything the person answering needs: which sponge
    and flavour the customer had chosen, what they asked for, and a reference
    image if they attached one.
    """

    __tablename__ = "cake_enquiries"

    id = Column(Integer, primary_key=True, index=True)
    # Nullable: someone may enquire before creating an account.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    contact_name = Column(String, nullable=False)
    contact_phone = Column(String, nullable=False)

    # The selections carried over from the builder. Free text rather than
    # foreign keys: the point of an enquiry is that it may not correspond to
    # anything sellable, and a rule that has since been renamed should not
    # change what the customer asked for.
    sponge = Column(String, nullable=True)
    flavor = Column(String, nullable=True)
    requested_size = Column(String, nullable=True)
    details = Column(Text, nullable=True)

    # Uploaded through the same /media mechanism as review photos.
    reference_image_url = Column(String, nullable=True)

    # Whether someone has picked this up. No workflow beyond that - an enquiry
    # is answered by a person, on WhatsApp.
    is_handled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")
