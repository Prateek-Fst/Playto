import { useEffect, useState } from "react";
import { api } from "../api.js";

export default function PayoutForm({ merchantId, bankAccounts, onSuccess }) {
  const [amountRupees, setAmountRupees] = useState("");
  const [bankAccountId, setBankAccountId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    if (bankAccounts.length && !bankAccountId) setBankAccountId(bankAccounts[0].id);
  }, [bankAccounts, bankAccountId]);

  const onSubmit = async (e) => {
    e.preventDefault();
    if (!merchantId || !bankAccountId) return;
    setSubmitting(true);
    setMessage(null);

    // Money is sent in paise. We round to integer paise to make sure we
    // don't accidentally send a float across the wire.
    const amount_paise = Math.round(parseFloat(amountRupees) * 100);
    if (!Number.isFinite(amount_paise) || amount_paise <= 0) {
      setMessage({ kind: "error", text: "Enter a valid amount in rupees" });
      setSubmitting(false);
      return;
    }

    // Fresh idempotency key per submit attempt. If the network drops, the
    // user can re-click and we'll re-send the same key — safe replay.
    const idempotencyKey = api.uuid();
    try {
      const { payout, replayed } = await api.createPayout(merchantId, {
        amount_paise,
        bank_account_id: bankAccountId,
        idempotencyKey,
      });
      setMessage({
        kind: "success",
        text: `Payout ${payout.id.slice(0, 8)}… ${replayed ? "replayed" : "created"}`,
      });
      setAmountRupees("");
      onSuccess?.();
    } catch (err) {
      setMessage({ kind: "error", text: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <h2 className="mb-3 text-sm font-semibold text-slate-700">Request a payout</h2>
      <form onSubmit={onSubmit} className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr,1fr,auto] sm:items-end">
        <label className="text-sm">
          <span className="block text-slate-500">Amount (₹)</span>
          <input
            type="number"
            step="0.01"
            min="0.01"
            value={amountRupees}
            onChange={(e) => setAmountRupees(e.target.value)}
            placeholder="500.00"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 font-mono focus:border-slate-500 focus:outline-none"
            required
          />
        </label>
        <label className="text-sm">
          <span className="block text-slate-500">To bank account</span>
          <select
            value={bankAccountId}
            onChange={(e) => setBankAccountId(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 focus:border-slate-500 focus:outline-none"
          >
            {bankAccounts.map((b) => (
              <option key={b.id} value={b.id}>
                {b.holder_name} ({b.ifsc} ****{b.account_number_last4})
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          disabled={submitting || !bankAccountId}
          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {submitting ? "Submitting…" : "Withdraw"}
        </button>
      </form>
      {message && (
        <div
          className={`mt-3 rounded-md px-3 py-2 text-sm ${
            message.kind === "success"
              ? "bg-emerald-50 text-emerald-700"
              : "bg-red-50 text-red-700"
          }`}
        >
          {message.text}
        </div>
      )}
    </section>
  );
}
