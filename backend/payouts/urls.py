from django.urls import path

from . import views

urlpatterns = [
    path("merchants/", views.list_merchants),
    path("merchants/me/balance/", views.merchant_balance),
    path("merchants/me/bank-accounts/", views.merchant_bank_accounts),
    path("merchants/me/ledger/", views.merchant_ledger),
    path("payouts/", views.payouts),
    path("payouts/<uuid:payout_id>/", views.payout_detail),
]
