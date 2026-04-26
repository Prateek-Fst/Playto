"""Seed three merchants, give each a bank account and a few credits.

Idempotent: matches existing merchants by email so re-running doesn't duplicate
rows. Credits are only created if the merchant has none, so re-running keeps
the balance stable.

    python manage.py seed
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from payouts.models import BankAccount, LedgerEntry, Merchant
from payouts.services import credit_merchant


SEED = [
    {
        "name": "Bright Fox Studio",
        "email": "founder@brightfox.in",
        "bank": {
            "holder_name": "Bright Fox Studio Pvt Ltd",
            "account_number_last4": "4321",
            "ifsc": "HDFC0000123",
        },
        # paise.  100_000_00 = ₹1,00,000
        "credits_paise": [25_000_00, 75_000_00, 12_500_00],
    },
    {
        "name": "Sahil Mehta (Freelancer)",
        "email": "sahil@example.com",
        "bank": {
            "holder_name": "Sahil Mehta",
            "account_number_last4": "8899",
            "ifsc": "ICIC0001234",
        },
        "credits_paise": [8_000_00, 4_500_00],
    },
    {
        "name": "Indigo Labs",
        "email": "ops@indigolabs.in",
        "bank": {
            "holder_name": "Indigo Labs LLP",
            "account_number_last4": "0012",
            "ifsc": "AXIS0009876",
        },
        "credits_paise": [50_000_00],
    },
]


class Command(BaseCommand):
    help = "Seed merchants, bank accounts, and a credit history."

    def handle(self, *args, **options):
        with transaction.atomic():
            for spec in SEED:
                merchant, created = Merchant.objects.get_or_create(
                    email=spec["email"], defaults={"name": spec["name"]}
                )
                self.stdout.write(
                    f"merchant {'created' if created else 'exists'}: "
                    f"{merchant.name} ({merchant.id})"
                )

                if not merchant.bank_accounts.exists():
                    BankAccount.objects.create(merchant=merchant, **spec["bank"])

                if not LedgerEntry.objects.filter(merchant=merchant).exists():
                    for i, amt in enumerate(spec["credits_paise"]):
                        credit_merchant(
                            merchant.id,
                            amt,
                            reference_id=f"seed-{merchant.id}-{i}",
                            note=f"Seed credit {i + 1}",
                        )
                    self.stdout.write(
                        f"  -> seeded {len(spec['credits_paise'])} credits "
                        f"({sum(spec['credits_paise'])} paise total)"
                    )

        self.stdout.write(self.style.SUCCESS("seed complete"))
