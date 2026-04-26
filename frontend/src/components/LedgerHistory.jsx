function inr(paise) {
  const sign = paise < 0 ? "-" : "+";
  const abs = Math.abs(paise) / 100;
  return `${sign}${abs.toLocaleString("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  })}`;
}

const TYPE_LABEL = {
  credit: "Credit",
  payout_hold: "Hold",
  payout_debit: "Debit",
  payout_release: "Release",
};

export default function LedgerHistory({ entries }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <h2 className="border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700">
        Ledger (latest 100 entries)
      </h2>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2">Time</th>
              <th className="px-4 py-2">Bucket</th>
              <th className="px-4 py-2">Type</th>
              <th className="px-4 py-2">Amount</th>
              <th className="px-4 py-2">Reference</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {entries.length === 0 && (
              <tr>
                <td colSpan="5" className="px-4 py-6 text-center text-slate-400">
                  No ledger entries yet.
                </td>
              </tr>
            )}
            {entries.map((e) => (
              <tr key={e.id}>
                <td className="px-4 py-2 font-mono text-xs text-slate-500">
                  {new Date(e.created_at).toLocaleString()}
                </td>
                <td className="px-4 py-2 capitalize">{e.bucket}</td>
                <td className="px-4 py-2 text-slate-600">
                  {TYPE_LABEL[e.entry_type] || e.entry_type}
                </td>
                <td
                  className={`px-4 py-2 font-mono ${
                    e.amount_paise < 0 ? "text-red-700" : "text-emerald-700"
                  }`}
                >
                  {inr(e.amount_paise)}
                </td>
                <td className="px-4 py-2 text-xs text-slate-500">
                  {e.reference_type
                    ? `${e.reference_type}:${e.reference_id?.slice(0, 8) || ""}`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
