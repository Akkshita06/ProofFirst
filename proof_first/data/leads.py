"""
Sample lead fixtures.

IMPORTANT / HONESTY NOTE:
In a production build, `primary_signals` would come from real tool calls
(Google Maps/Business Profile API, a site scraper, a reviews API) and
`secondary_signals` would come from a SECOND, independent tool call the
SkepticAgent makes on purpose (e.g. an Instagram/Facebook lookup, a direct
fetch of the business's own site) that ResearchAgent never looks at.

This project does not have live Maps/Instagram API credentials wired up,
so these fixtures stand in for "what those tools would have returned" -
they are hand-written, clearly-labelled synthetic examples (not scraped
real businesses), used so the pipeline's decision logic can be run,
demoed, and evaluated end-to-end honestly without requiring paid keys.
Swap `fetch_primary_signals` / `fetch_secondary_signals` below for real
API calls to go from demo mode to production.
"""
from __future__ import annotations

from typing import Dict, Any, List

LEADS: List[Dict[str, Any]] = [
    {
        "id": "lead_001",
        "name": "Acme Dental",
        "vertical": "dental clinic",
        "primary_signals": {
            # what ResearchAgent's (single) search pass turns up
            "has_website": True,
            "site_has_booking_link": False,
            "avg_review_score": 4.1,
            "review_count": 62,
            "listing_hours_text": "Mon-Fri 9am-5pm",
        },
        "secondary_signals": {
            # what the SkepticAgent's INDEPENDENT second pass turns up
            "instagram_bio": "Book your appointment: DM us or call (555) 010-2222!",
            "instagram_recent_posts_mention_dm_booking": True,
            "actual_hours_from_gbp": "Mon-Fri 9am-5pm, Sat 10am-2pm",  # contradicts listing_hours_text
        },
    },
    {
        "id": "lead_002",
        "name": "Acme Cafe",
        "vertical": "cafe",
        "primary_signals": {
            "has_website": True,
            "site_has_booking_link": True,
            "site_booking_link_url": "https://acmecafe.example/book",
            "avg_review_score": 3.4,
            "review_count": 140,
            "recent_review_snippets": [
                "Tried to book online three times, the page just spins forever.",
                "Booking link never works for me either, had to call instead.",
            ],
        },
        "secondary_signals": {
            "booking_link_http_status": 500,      # independently re-checked - link is actually broken
            "instagram_bio": "Walk-ins welcome!",
            "instagram_recent_posts_mention_dm_booking": False,
        },
    },
    {
        "id": "lead_003",
        "name": "Acme Hardware",
        "vertical": "hardware store",
        "primary_signals": {
            "has_website": False,
            "site_has_booking_link": False,
            "avg_review_score": 4.7,
            "review_count": 210,
        },
        "secondary_signals": {
            "instagram_bio": "Family owned since 1988. Call ahead for special orders.",
            "instagram_recent_posts_mention_dm_booking": False,
            "actual_hours_from_gbp": None,
        },
    },

    # ------------------------------------------------------------------
    # Extended fixture set (17 more leads, 20 total).
    #
    # Added to exercise every branch of evidence_synthesis_agent.py's
    # routing (not just the two the original 3-lead set covered) and to
    # give evaluation.py a larger, more realistic sample. Given the
    # deterministic verdict/confidence rules in evidence_synthesis_agent.py
    # and skeptic_agent.py, the three-way split is fully determined by
    # which verdicts a lead's claims land on:
    #   - CLOSE:         any claim comes back CONTRADICTED (contradicted
    #                     confidences are always < 0.4, so this branch is
    #                     always reached regardless of other claims - see
    #                     lead_006/lead_018 below for leads that are
    #                     "mixed" in the sense of having a corroborated
    #                     claim too, but still correctly CLOSE overall).
    #   - PROCEED_TO_PROOF_OF_WORK (ACT): no contradicted claim and at
    #                     least one CORROBORATED claim.
    #   - HUMAN_REVIEW:   no contradicted, no corroborated - every claim
    #                     came back UNVERIFIABLE (no independent source was
    #                     available to check it either way). This branch
    #                     had ZERO fixture coverage before lead_013-016 and
    #                     lead_020 were added below.
    # Each lead's expected decision is checked against the real pipeline
    # in tests/test_pipeline.py::test_expected_decision_for_every_fixture_lead.
    # ------------------------------------------------------------------

    {
        # CLOSE: single CONTRADICTED claim (independent Instagram check
        # finds an active DM-booking flow the primary pass missed).
        "id": "lead_004", "name": "Sunrise Yoga Studio", "vertical": "yoga studio",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": False,
            "avg_review_score": 4.5, "review_count": 88,
        },
        "secondary_signals": {
            "instagram_bio": "Book a class: link in bio or DM us!",
            "instagram_recent_posts_mention_dm_booking": True,
        },
    },
    {
        # CLOSE: single CONTRADICTED claim (booking link works when
        # independently re-checked - the review complaints were stale).
        "id": "lead_005", "name": "Blue Harbor Seafood", "vertical": "restaurant",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "recent_review_snippets": ["The booking page is unusable, keeps timing out."],
            "avg_review_score": 3.9, "review_count": 210,
        },
        "secondary_signals": {
            "booking_link_http_status": 200,
            "instagram_bio": "Reservations open nightly.",
        },
    },
    {
        # CLOSE, but MIXED evidence: the booking claim is CORROBORATED
        # (0.85) while the hours claim is CONTRADICTED (0.3). A single
        # contradicted claim still caps overall confidence and forces
        # CLOSE, even with a corroborated claim present - this is the
        # literal mechanism the README calls "the system catches itself."
        "id": "lead_006", "name": "Northgate Auto Repair", "vertical": "auto repair",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": False,
            "listing_hours_text": "Mon-Sat 8am-6pm",
            "avg_review_score": 4.2, "review_count": 55,
        },
        "secondary_signals": {
            "instagram_bio": "Family owned. Call ahead.",
            "instagram_recent_posts_mention_dm_booking": False,
            "actual_hours_from_gbp": "Mon-Fri 8am-6pm",
        },
    },
    {
        # ACT with MIXED evidence: broken-link claim CORROBORATED (0.94),
        # hours claim UNVERIFIABLE (0.5, excluded from the confidence
        # average) - not a "clean" single-claim ACT case.
        "id": "lead_007", "name": "Petal & Stem Florist", "vertical": "florist",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "recent_review_snippets": ["Booking a delivery slot online never works."],
            "listing_hours_text": "Tue-Sun 9am-5pm",
            "avg_review_score": 4.0, "review_count": 33,
        },
        "secondary_signals": {
            "booking_link_http_status": 503,
            "actual_hours_from_gbp": "Tue-Sun 9am-5pm",
        },
    },
    {
        # ACT: no-website claim, always CORROBORATED by design.
        "id": "lead_008", "name": "Golden Wok Takeout", "vertical": "restaurant",
        "primary_signals": {
            "has_website": False, "site_has_booking_link": False,
            "avg_review_score": 4.6, "review_count": 300,
        },
        "secondary_signals": {
            "instagram_bio": "Order by phone, cash only.",
            "instagram_recent_posts_mention_dm_booking": False,
        },
    },
    {
        # ACT: booking claim CORROBORATED (no independent DM/booking
        # mention found).
        "id": "lead_009", "name": "Ironclad Gym", "vertical": "gym",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": False,
            "avg_review_score": 4.3, "review_count": 71,
        },
        "secondary_signals": {
            "instagram_bio": "Drop by for a free trial.",
            "instagram_recent_posts_mention_dm_booking": False,
        },
    },
    {
        # ACT: broken-link claim CORROBORATED (fresh 500 on re-check).
        "id": "lead_010", "name": "Maple Street Diner", "vertical": "restaurant",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "recent_review_snippets": ["Can't get the online booking to load, ever."],
            "avg_review_score": 3.6, "review_count": 175,
        },
        "secondary_signals": {
            "booking_link_http_status": 500,
            "instagram_bio": "Walk-ins only.",
        },
    },
    {
        # ACT: no-website claim CORROBORATED.
        "id": "lead_011", "name": "Willow Creek Vet Clinic", "vertical": "veterinary clinic",
        "primary_signals": {
            "has_website": False, "site_has_booking_link": False,
            "avg_review_score": 4.8, "review_count": 402,
        },
        "secondary_signals": {
            "instagram_bio": "New patients call the front desk.",
            "instagram_recent_posts_mention_dm_booking": False,
        },
    },
    {
        # ACT: booking claim CORROBORATED.
        "id": "lead_012", "name": "Cedar Lane Bookshop", "vertical": "bookshop",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": False,
            "avg_review_score": 4.4, "review_count": 61,
        },
        "secondary_signals": {
            "instagram_bio": "Come browse anytime, no appointment needed.",
            "instagram_recent_posts_mention_dm_booking": False,
        },
    },
    {
        # HUMAN_REVIEW: single UNVERIFIABLE claim - no independent
        # technical re-check of the booking link was available.
        "id": "lead_013", "name": "Sterling Law Offices", "vertical": "law firm",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "recent_review_snippets": ["Booking a consultation online is a pain."],
            "avg_review_score": 4.0, "review_count": 19,
        },
        "secondary_signals": {
            "instagram_bio": "Consultations by appointment.",
        },
    },
    {
        # HUMAN_REVIEW: single UNVERIFIABLE claim - no independent hours
        # source (e.g. Google Business Profile) was available.
        "id": "lead_014", "name": "Harmony Dental Care", "vertical": "dental clinic",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "listing_hours_text": "Mon-Thu 8am-4pm",
            "avg_review_score": 4.1, "review_count": 47,
        },
        "secondary_signals": {
            "instagram_bio": "Now accepting new patients.",
        },
    },
    {
        # HUMAN_REVIEW: two UNVERIFIABLE claims (broken-link + hours) -
        # ambiguous/mixed evidence that still nets out to "ask a human,"
        # not a clean single-claim case.
        "id": "lead_015", "name": "Riverbend Pet Grooming", "vertical": "pet grooming",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "recent_review_snippets": ["I tried booking a slot but the calendar never loads."],
            "listing_hours_text": "Wed-Sun 10am-6pm",
            "avg_review_score": 3.8, "review_count": 24,
        },
        "secondary_signals": {
            "instagram_bio": "Message us for availability.",
        },
    },
    {
        # HUMAN_REVIEW: single UNVERIFIABLE hours claim.
        "id": "lead_016", "name": "Pinecrest Chiropractic", "vertical": "chiropractic clinic",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "listing_hours_text": "Mon,Wed,Fri 9am-1pm",
            "avg_review_score": 4.6, "review_count": 15,
        },
        "secondary_signals": {
            "instagram_bio": "By appointment.",
        },
    },
    {
        # ACT: no-website claims are always CORROBORATED regardless of
        # secondary signals (by design - see _check_no_website_claim) -
        # included to make that design choice visible/testable even
        # though the Instagram bio here superficially looks
        # booking-related; it is irrelevant to this claim type.
        "id": "lead_017", "name": "Old Town Tailors", "vertical": "tailor shop",
        "primary_signals": {
            "has_website": False, "site_has_booking_link": False,
            "avg_review_score": 4.9, "review_count": 58,
        },
        "secondary_signals": {
            "instagram_bio": "DM for a fitting appointment.",
            "instagram_recent_posts_mention_dm_booking": True,
        },
    },
    {
        # CLOSE, MIXED evidence: broken-link claim CORROBORATED (0.94)
        # but hours claim CONTRADICTED (0.3) - again shows a single
        # contradicted claim overriding an otherwise strong corroborated
        # signal.
        "id": "lead_018", "name": "Copper Kettle Brewpub", "vertical": "brewpub",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "recent_review_snippets": ["Great beer but the table booking form is broken."],
            "listing_hours_text": "Daily 12pm-11pm",
            "avg_review_score": 4.3, "review_count": 260,
        },
        "secondary_signals": {
            "booking_link_http_status": 502,
            "actual_hours_from_gbp": "Daily 12pm-10pm",
        },
    },
    {
        # ACT, mixed evidence: booking claim CORROBORATED (0.85), hours
        # claim UNVERIFIABLE (0.5, excluded from the average since it
        # matches on independent re-check but the rule never upgrades a
        # matching hours claim to CORROBORATED).
        "id": "lead_019", "name": "Evergreen Landscaping", "vertical": "landscaping",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": False,
            "listing_hours_text": "Mon-Fri 7am-4pm",
            "avg_review_score": 4.5, "review_count": 40,
        },
        "secondary_signals": {
            "instagram_bio": "Free quotes, call or email.",
            "instagram_recent_posts_mention_dm_booking": False,
            "actual_hours_from_gbp": "Mon-Fri 7am-4pm",
        },
    },
    {
        # HUMAN_REVIEW: single UNVERIFIABLE hours claim (reviews don't
        # mention booking, so no broken-link claim is even generated).
        "id": "lead_020", "name": "Lakeside Marina Tours", "vertical": "boat tours",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "recent_review_snippets": ["Loved the tour, staff was great."],
            "listing_hours_text": "Seasonal, call for hours",
            "avg_review_score": 4.9, "review_count": 130,
        },
        "secondary_signals": {
            "instagram_bio": "Book your tour today - link in bio!",
        },
    },
]


def get_lead(lead_id: str) -> Dict[str, Any]:
    for lead in LEADS:
        if lead["id"] == lead_id:
            return lead
    raise KeyError(lead_id)
