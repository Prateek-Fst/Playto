import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import BalanceCard from "./components/BalanceCard.jsx";
import LedgerHistory from "./components/LedgerHistory.jsx";
import MerchantSwitcher from "./components/MerchantSwitcher.jsx";
import PayoutForm from "./components/PayoutForm.jsx";
import PayoutHistory from "./components/PayoutHistory.jsx";

const POLL_INTERVAL_MS = 3000;

export default function App() {
  const [merchants, setMerchants] = useState([]);
  const [merchantId, setMerchantId] = useState(null);
  const [balance, setBalance] = useState(null);
  const [bankAccounts, setBankAccounts] = useState([]);
  const [payouts, setPayouts] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [loadError, setLoadError] = useState(null);

  // Load merchants once on mount.
  useEffect(() => {
    api
      .listMerchants()
      .then((rows) => {
        setMerchants(rows);
        if (rows.length && !merchantId) setMerchantId(rows[0].id);
      })
      .catch((e) => setLoadError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = useCallback(async () => {
    if (!merchantId) return;
    try {
      const [bal, banks, pys, led] = await Promise.all([
        api.getBalance(merchantId),
        api.getBankAccounts(merchantId),
        api.listPayouts(merchantId),
        api.getLedger(merchantId),
      ]);
      setBalance(bal);
      setBankAccounts(banks);
      setPayouts(pys);
      setLedger(led);
      setLoadError(null);
    } catch (e) {
      setLoadError(e.message);
    }
  }, [merchantId]);

  // Initial + polled refresh while a merchant is selected.
  useEffect(() => {
    refresh();
    if (!merchantId) return undefined;
    const t = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(t);
  }, [merchantId, refresh]);

  return (
    <div className="min-h-screen text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold">Playto Pay — Payout Engine</h1>
            <p className="text-sm text-slate-500">
              Ledger-backed merchant payouts with idempotent requests and
              row-locked concurrency.
            </p>
          </div>
          <MerchantSwitcher
            merchants={merchants}
            selected={merchantId}
            onSelect={setMerchantId}
          />
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-6 px-6 py-6">
        {loadError && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {loadError}
          </div>
        )}

        <BalanceCard balance={balance} />

        <PayoutForm
          merchantId={merchantId}
          bankAccounts={bankAccounts}
          onSuccess={refresh}
        />

        <PayoutHistory payouts={payouts} />

        <LedgerHistory entries={ledger} />
      </main>
    </div>
  );
}
