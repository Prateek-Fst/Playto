export default function MerchantSwitcher({ merchants, selected, onSelect }) {
  if (!merchants.length) {
    return (
      <span className="text-sm text-slate-500">No merchants seeded</span>
    );
  }
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-slate-500">Merchant</span>
      <select
        value={selected || ""}
        onChange={(e) => onSelect(e.target.value)}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm focus:border-slate-500 focus:outline-none"
      >
        {merchants.map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </select>
    </label>
  );
}
