function inr(paise) {
  if (paise === null || paise === undefined) return "—";
  return (paise / 100).toLocaleString("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  });
}

const STATE_BADGE = {
  pending: "bg-slate-100 text-slate-700",
  processing: "bg-amber-100 text-amber-800",
  queued_for_retry: "bg-orange-100 text-orange-800",
  completed: "bg-emerald-100 text-emerald-800",
  failed: "bg-red-100 text-red-800",
};

const STATE_LABEL = {
  queued_for_retry: "queued for retry",
};

export default function PayoutHistory({ payouts }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <h2 className="border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700">
        Payout history
      </h2>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2">Created</th>
              <th className="px-4 py-2">Amount</th>
              <th className="px-4 py-2">To</th>
              <th className="px-4 py-2">State</th>
              <th className="px-4 py-2">Retries</th>
              <th className="px-4 py-2">Reason</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {payouts.length === 0 && (
              <tr>
                <td colSpan="6" className="px-4 py-6 text-center text-slate-400">
                  No payouts yet.
                </td>
              </tr>
            )}
            {payouts.map((p) => (
              <tr key={p.id}>
                <td className="px-4 py-2 font-mono text-xs text-slate-500">
                  {new Date(p.created_at).toLocaleString()}
                </td>
                <td className="px-4 py-2 font-mono">{inr(p.amount_paise)}</td>
                <td className="px-4 py-2 text-slate-600">
                  {p.bank_account
                    ? `${p.bank_account.ifsc} ****${p.bank_account.account_number_last4}`
                    : "—"}
                </td>
                <td className="px-4 py-2">
                  <span
                    className={`inline-block rounded-full px-2 py-0.5 text-xs font-semibold ${STATE_BADGE[p.state] || "bg-slate-100"}`}
                  >
                    {STATE_LABEL[p.state] || p.state}
                  </span>
                </td>
                <td className="px-4 py-2 font-mono text-slate-600">{p.retry_count}</td>
                <td className="px-4 py-2 text-slate-500">{p.failure_reason || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
