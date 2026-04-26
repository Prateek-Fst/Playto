from __future__ import annotations

from rest_framework import serializers

from .models import BankAccount, LedgerEntry, Merchant, Payout


class MerchantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = ["id", "name", "email", "created_at"]


class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        fields = [
            "id",
            "holder_name",
            "account_number_last4",
            "ifsc",
            "created_at",
        ]


class LedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerEntry
        fields = [
            "id",
            "bucket",
            "entry_type",
            "amount_paise",
            "reference_type",
            "reference_id",
            "note",
            "created_at",
        ]


class PayoutSerializer(serializers.ModelSerializer):
    bank_account = BankAccountSerializer(read_only=True)

    class Meta:
        model = Payout
        fields = [
            "id",
            "amount_paise",
            "state",
            "bank_account",
            "processing_started_at",
            "retry_count",
            "failure_reason",
            "created_at",
            "updated_at",
        ]


class PayoutCreateSerializer(serializers.Serializer):
    """Validation only — actual creation is in services.request_payout."""

    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.UUIDField()
