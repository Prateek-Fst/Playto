function inr(paise) {
  if (paise === null || paise === undefined) return "—";
  const rupees = paise / 100;
  return rupees.toLocaleString("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  });
}

export default function BalanceCard({ balance }) {
  return (
    <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
      <Cell label="Available" paise={balance?.available_paise} primary />
      <Cell label="Held (in flight)" paise={balance?.held_paise} />
      <Cell label="Total" paise={balance?.total_paise} />
    </section>
  );
}

function Cell({ label, paise, primary }) {
  return (
    <div
      className={`rounded-lg border bg-white px-4 py-3 ${
        primary ? "border-emerald-300" : "border-slate-200"
      }`}
    >
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div
        className={`mt-1 font-mono text-2xl font-semibold ${
          primary ? "text-emerald-700" : "text-slate-800"
        }`}
      >
        {inr(paise)}
      </div>
      <div className="mt-1 font-mono text-xs text-slate-400">
        {paise ?? 0} paise
      </div>
    </div>
  );
}
