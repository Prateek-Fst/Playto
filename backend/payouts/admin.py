from django.contrib import admin

from .models import BankAccount, IdempotencyKey, LedgerEntry, Merchant, Payout


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "email", "created_at")
    search_fields = ("name", "email")


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "holder_name", "account_number_last4", "ifsc")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merchant",
        "bucket",
        "entry_type",
        "amount_paise",
        "reference_id",
        "created_at",
    )
    list_filter = ("bucket", "entry_type")
    search_fields = ("reference_id",)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merchant",
        "amount_paise",
        "state",
        "retry_count",
        "created_at",
    )
    list_filter = ("state",)


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("merchant", "key", "status", "created_at", "payout")
    list_filter = ("status",)
    search_fields = ("key",)
