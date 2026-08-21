"""Generate a synthetic catalog for demo mode.

Nobody can evaluate Catalog without an Azure app registration, an Anthropic
key, and a mailbox with years of history in it. This produces the mailbox:
a few hundred fabricated threads spanning a decade of ordinary life —
appointments, statements, repairs, school, travel, and the bulk mail that
surrounds them — tagged the way the real tagger tags, so search and
Detective behave the way they behave on a real archive.

Every person, company, address and reference number here is invented.
Personal addresses use the reserved .example TLD, so the secret scanner's
free-mail rule can never fire on this file or on anything it generates.

Deterministic: same seed, same catalog. Run directly to preview threads
without writing anything:

    python demo_seed.py            # print a sample, write nothing
    python demo_seed.py --count 5  # preview five random threads
"""
import hashlib
import json
import random
import sys
from datetime import datetime, timedelta, timezone

DEMO_USER_ID = "demo:local"
DEMO_EMAIL = "you@demo.example"
DEMO_NAME = "Demo User"
SEED = 11629  # thread count of the archive this imitates

# ── The cast ──────────────────────────────────────────────────────────────────
# The owner of this fictional mailbox is Maya Lindqvist. The people below are
# her dentist, her bank, her plumber, her sister, her son's school. Domains
# are fictional businesses or .example; never a real free-mail provider.

OWNER = "maya.lindqvist@postbox.example"

STORYLINES = [
    # (slug, [(subject, from, to_extra, body, attachments, tags)], year_range)
    {
        "slug": "dental",
        "org": "Bluepine Dental",
        "sender": "reception@bluepinedental.example",
        "years": (2015, 2025),
        "per_year": 2,
        "subjects": [
            "Appointment confirmation — {month} {day}",
            "Your cleaning is due",
            "Reminder: appointment tomorrow at {hour}:00",
            "Treatment plan and estimate",
        ],
        "body": ("Dear Maya, this confirms your appointment with Dr. Okafor at our "
                 "Riverton office. Please arrive ten minutes early and bring your "
                 "insurance card. Our records show your last cleaning was over six "
                 "months ago."),
        "attachments": [("treatment_estimate_{year}.pdf", 184_000)],
        "attach_odds": 0.3,
        "tags": ["dentist", "dental", "bluepine", "appointment", "teeth", "cleaning",
                 "dr okafor", "okafor", "riverton", "health", "checkup", "hygiene",
                 "reminder", "estimate", "insurance", "medical"],
    },
    {
        "slug": "bank",
        "org": "Cassia Savings Bank",
        "sender": "statements@cassiasavings.example",
        "years": (2014, 2025),
        "per_year": 4,
        "subjects": [
            "Your {month} statement is ready",
            "eStatement notification — account ending 4471",
        ],
        "body": ("Your statement for the period is now available. Account ending "
                 "4471. Please review your transactions and report any discrepancy "
                 "within 60 days. Do not reply to this automated message."),
        "attachments": [("Cassia_Statement_{month}_{year}.pdf", 412_000)],
        "attach_odds": 0.9,
        "tags": ["bank", "statement", "cassia", "savings", "account", "4471",
                 "finance", "money", "banking", "estatement", "transactions",
                 "monthly", "pdf", "financial", "checking"],
    },
    {
        "slug": "carloan",
        "org": "Meridian Auto Finance",
        "sender": "loans@meridianautofinance.example",
        "years": (2016, 2021),
        "per_year": 3,
        "subjects": [
            "Auto loan payment received",
            "Your loan statement — agreement 88-20411",
            "Payoff quote you requested",
            "Welcome to Meridian Auto Finance",
        ],
        "body": ("Thank you for your payment on agreement 88-20411 for your 2016 "
                 "Honda Civic. Your remaining balance and payment schedule are in "
                 "the attached statement. Questions? Call us weekdays 8-6."),
        "attachments": [("loan_statement_88-20411_{year}.pdf", 268_000)],
        "attach_odds": 0.5,
        "tags": ["car", "loan", "auto", "honda", "civic", "meridian", "vehicle",
                 "payment", "finance", "financing", "88-20411", "2016", "automobile",
                 "monthly payment", "balance", "payoff", "car loan"],
    },
    {
        "slug": "school",
        "org": "Fernwood Elementary",
        "sender": "office@fernwoodelementary.example",
        "years": (2017, 2024),
        "per_year": 5,
        "subjects": [
            "Parent-teacher conferences — sign up",
            "Field trip permission slip — {month}",
            "School photos are ready to order",
            "Winter concert: {month} {day}",
            "Report cards go home Friday",
        ],
        "body": ("Dear Fernwood families, please find details attached. Elias's "
                 "class (Ms. Duarte, room 12) is scheduled for the morning session. "
                 "Return the signed form by Friday. Volunteers welcome."),
        "attachments": [("permission_slip_{year}.pdf", 96_000),
                        ("Fernwood_newsletter_{month}_{year}.pdf", 340_000)],
        "attach_odds": 0.6,
        "tags": ["school", "fernwood", "elementary", "elias", "ms duarte", "duarte",
                 "parent teacher", "conference", "field trip", "permission slip",
                 "children", "kids", "education", "class", "teacher", "newsletter",
                 "photos", "concert", "report card"],
    },
    {
        "slug": "plumber",
        "org": "Harbor & Sons Plumbing",
        "sender": "jim@harborandsons.example",
        "years": (2018, 2023),
        "per_year": 1,
        "subjects": [
            "Invoice #{ref} — water heater repair",
            "Quote for bathroom repipe",
            "Follow-up: kitchen leak",
        ],
        "body": ("Hi Maya, invoice attached for the work completed on Tuesday. "
                 "The water heater needed a new thermocouple and pressure valve. "
                 "Parts are under warranty for two years. Thanks — Jim."),
        "attachments": [("HarborSons_invoice_{ref}.pdf", 152_000)],
        "attach_odds": 0.8,
        "reply_odds": 0.9,
        "reply_body": ("Thanks Jim, paid by transfer this morning. The heater's "
                       "working fine now. Could you also quote the repipe when you "
                       "get a chance? — Maya"),
        "tags": ["plumber", "plumbing", "harbor", "jim", "invoice", "water heater",
                 "repair", "leak", "kitchen", "bathroom", "house", "home",
                 "maintenance", "quote", "warranty", "repipe", "thermocouple"],
    },
    {
        "slug": "insurance",
        "org": "Northgate Mutual",
        "sender": "policyservices@northgatemutual.example",
        "years": (2015, 2025),
        "per_year": 2,
        "subjects": [
            "Policy renewal — home insurance {year}",
            "Your updated policy documents",
            "Claim {ref}: status update",
        ],
        "body": ("Your homeowner's policy HN-3392-K renews on the first of next "
                 "month. Your premium and coverage summary are attached. No action "
                 "is needed unless your details have changed."),
        "attachments": [("Northgate_policy_HN-3392-K_{year}.pdf", 1_240_000)],
        "attach_odds": 0.7,
        "tags": ["insurance", "northgate", "policy", "home insurance", "renewal",
                 "premium", "coverage", "hn-3392-k", "homeowners", "claim", "house",
                 "property", "documents", "mutual", "annual"],
    },
    {
        "slug": "sister",
        "org": None,
        "sender": "annika.lindqvist@fastpost.example",
        "years": (2014, 2025),
        "per_year": 4,
        "subjects": [
            "Flights for midsummer",
            "Mom's birthday — ideas?",
            "Photos from the lake",
            "Re: that recipe you wanted",
            "Cabin dates this summer",
        ],
        "body": ("Hej! I found flights arriving the 19th if that still works for "
                 "you and Elias. Mom doesn't know we're both coming, so keep it "
                 "quiet. I'll forward the booking once it's paid. Kram, Annika"),
        "attachments": [("lake_weekend_{year}.pdf", 2_100_000)],
        "attach_odds": 0.2,
        "reply_odds": 0.8,
        "reply_body": ("The 19th works! Elias is out of school by then. Booking the "
                       "car tomorrow. Don't let mom near your calendar. /M"),
        "tags": ["annika", "sister", "family", "midsummer", "flights", "travel",
                 "mom", "birthday", "lake", "cabin", "summer", "photos", "recipe",
                 "sweden", "personal", "elias", "holiday", "booking"],
    },
    {
        "slug": "flights",
        "org": "Skylark Airways",
        "sender": "bookings@skylarkairways.example",
        "years": (2015, 2025),
        "per_year": 1,
        "subjects": [
            "Booking confirmed — {ref}",
            "Your itinerary and receipt, ref {ref}",
            "Check-in is open for your flight",
        ],
        "body": ("Booking reference {ref}. Outbound SLK441 departing 09:35, "
                 "returning SLK448. Baggage: 1 checked bag per passenger. Your "
                 "e-ticket and receipt are attached. Have a good flight."),
        "attachments": [("Skylark_eticket_{ref}.pdf", 220_000)],
        "attach_odds": 0.85,
        "tags": ["flight", "skylark", "airline", "booking", "travel", "itinerary",
                 "ticket", "eticket", "receipt", "trip", "vacation", "airways",
                 "slk441", "check-in", "airport", "baggage"],
    },
    {
        "slug": "tax",
        "org": "Ostrom & Vale Accounting",
        "sender": "priya@ostromvale.example",
        "years": (2015, 2025),
        "per_year": 1,
        "subjects": [
            "Your {year} return — documents needed",
            "{year} tax return filed — copy attached",
        ],
        "body": ("Hi Maya, your return has been filed and accepted. A copy is "
                 "attached for your records along with the deduction worksheet. "
                 "Keep these for seven years. Best, Priya"),
        "attachments": [("tax_return_{year}_filed.pdf", 890_000),
                        ("deduction_worksheet_{year}.pdf", 145_000)],
        "attach_odds": 0.95,
        "reply_odds": 0.5,
        "reply_body": ("Thank you Priya! Refund arrived Thursday. Same time next "
                       "year. — Maya"),
        "tags": ["tax", "taxes", "return", "priya", "ostrom", "vale", "accounting",
                 "accountant", "filed", "deduction", "refund", "documents",
                 "worksheet", "annual", "finance", "records"],
    },
]

BULK = {
    "senders": [
        ("Verdant Grocers", "offers@verdantgrocers.example",
         "This week: 20% off produce. Members save more every Thursday.",
         ["grocery", "verdant", "offers", "deals", "newsletter", "food",
          "discount", "weekly", "shopping", "promotion"]),
        ("Streamora", "no-reply@streamora.example",
         "New this month on Streamora. Continue watching where you left off.",
         ["streamora", "streaming", "newsletter", "shows", "movies",
          "entertainment", "subscription", "monthly", "new releases"]),
        ("Riverton Library", "news@rivertonlibrary.example",
         "Your holds are ready for pickup. Author talk this Saturday at 2pm.",
         ["library", "riverton", "books", "holds", "pickup", "events",
          "author talk", "reading", "community", "newsletter"]),
    ],
    "subjects": [
        "This week's offers",
        "{month} newsletter",
        "Don't miss out — ends Sunday",
        "Your {month} update",
    ],
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _rng_for(slug, year, i):
    """A stable per-thread RNG so edits to one storyline don't reshuffle others."""
    h = hashlib.sha256(f"{SEED}:{slug}:{year}:{i}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_thread(slug, sender, subject, body, msgs_extra, attachments, tags, when):
    """Assemble one thread dict in exactly the shape upsert_thread expects."""
    tid = f"demo-{slug}-{hashlib.sha256((subject + _iso(when)).encode()).hexdigest()[:16]}"
    participants = sorted({OWNER, sender})
    messages = [{
        "id": f"{tid}-m0",
        "web_link": "",
        "date": when.strftime("%Y-%m-%d"),
        "has_attachments": bool(attachments),
    }]
    date_last = when
    for j, (reply_from, reply_body, gap_hours) in enumerate(msgs_extra, start=1):
        date_last = date_last + timedelta(hours=gap_hours)
        messages.append({
            "id": f"{tid}-m{j}",
            "web_link": "",
            "date": date_last.strftime("%Y-%m-%d"),
            "has_attachments": False,
        })
        participants = sorted(set(participants) | {reply_from})

    body_chars = len(body) + sum(len(b) for _, b, _ in msgs_extra)
    return {
        "thread_id": tid,
        "message_ids": messages,
        "subject": subject,
        "participants": participants,
        "date_first": _iso(when),
        "date_last": _iso(date_last),
        "has_attachments": 1 if attachments else 0,
        "attachments": [
            {"name": name, "content_type": "application/pdf", "size": size,
             "actual_char_count": max(400, size // 300), "scan_status": "ok"}
            for name, size in attachments
        ],
        "web_link": "",
        "ai_tags": tags,
        "user_tags": [],
        "manually_reviewed": 0,
        "last_synced": _iso(when),
        "body_char_count": body_chars,
        "body_scan_status": "ok",
        "tags_truncated": 0,
    }


def generate():
    """Build the full synthetic catalog. Pure function of SEED."""
    threads = []

    for line in STORYLINES:
        y0, y1 = line["years"]
        for year in range(y0, y1 + 1):
            for i in range(line["per_year"]):
                rng = _rng_for(line["slug"], year, i)
                month_i = rng.randrange(12)
                when = datetime(year, month_i + 1, rng.randrange(1, 28),
                                rng.randrange(8, 19), rng.randrange(60),
                                tzinfo=timezone.utc)
                ref = f"{rng.randrange(10000, 99999)}"
                fmt = {"month": MONTHS[month_i], "day": rng.randrange(1, 28),
                       "hour": rng.randrange(9, 17), "year": year, "ref": ref}
                subject = rng.choice(line["subjects"]).format(**fmt)
                body = line["body"].format(**fmt)

                attachments = []
                if rng.random() < line["attach_odds"]:
                    name_t, size = rng.choice(line["attachments"])
                    attachments = [(name_t.format(**fmt),
                                    int(size * rng.uniform(0.6, 1.4)))]

                replies = []
                if rng.random() < line.get("reply_odds", 0.0):
                    replies = [(OWNER, line["reply_body"], rng.randrange(2, 72))]

                # Tags the way the tagger produces them: the storyline's tag
                # pool, shuffled and sampled, plus the year and month — over-
                # generated on purpose, deduplicated the way clean_tags does.
                tags = list(dict.fromkeys(
                    rng.sample(line["tags"], k=min(len(line["tags"]),
                                                   rng.randrange(10, len(line["tags"]) + 1)))
                    + [str(year), MONTHS[month_i].lower()]
                ))

                threads.append(_make_thread(
                    line["slug"], line["sender"], subject, body,
                    replies, attachments, tags, when))

    # Bulk mail: high volume, no replies, rarely attachments — the 12% that
    # a real mailbox carries and the reason "skip bulk mail" was measured.
    for year in range(2016, 2026):
        for i in range(10):
            rng = _rng_for("bulk", year, i)
            org, sender, body, tags = rng.choice(BULK["senders"])
            month_i = rng.randrange(12)
            when = datetime(year, month_i + 1, rng.randrange(1, 28),
                            rng.randrange(6, 12), rng.randrange(60),
                            tzinfo=timezone.utc)
            subject = rng.choice(BULK["subjects"]).format(month=MONTHS[month_i])
            threads.append(_make_thread(
                "bulk", sender, subject, body, [], [],
                list(dict.fromkeys(tags + [str(year), MONTHS[month_i].lower()])),
                when))

    threads.sort(key=lambda t: t["date_first"])
    return threads


def seed(db_module):
    """Write the catalog for the demo user. Returns the thread count."""
    now = datetime.now(timezone.utc).isoformat()
    db_module.upsert_user(DEMO_USER_ID, DEMO_EMAIL, DEMO_NAME, now)
    threads = generate()
    for t in threads:
        db_module.upsert_thread(DEMO_USER_ID, t)
    return len(threads)


if __name__ == "__main__":
    count = 3
    if "--count" in sys.argv:
        count = int(sys.argv[sys.argv.index("--count") + 1])
    threads = generate()
    print(f"{len(threads)} threads generated "
          f"({sum(1 for t in threads if t['has_attachments'])} with attachments, "
          f"{sum(1 for t in threads if len(t['message_ids']) > 1)} with replies)\n")
    for t in random.Random().sample(threads, count):
        print(f"── {t['subject']}")
        print(f"   {', '.join(t['participants'])}")
        print(f"   {t['date_first'][:10]}"
              + (f" → {t['date_last'][:10]}" if len(t['message_ids']) > 1 else ""))
        for a in t["attachments"]:
            print(f"   📎 {a['name']} ({a['size'] // 1000}KB)")
        print(f"   tags: {', '.join(t['ai_tags'][:14])}"
              + (" …" if len(t["ai_tags"]) > 14 else ""))
        print()
