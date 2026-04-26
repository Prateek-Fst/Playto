// Thin API client. Generates a fresh idempotency key per submit attempt
// (NOT per render) — that's the pattern a production client should use.

const BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // RFC4122 v4 fallback
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

async function request(path, { merchantId, method = "GET", body, headers } = {}) {
  const finalHeaders = {
    "Content-Type": "application/json",
    ...(merchantId ? { "X-Merchant-Id": merchantId } : {}),
    ...(headers || {}),
  };
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: finalHeaders,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const message =
      (data && data.error && data.error.message) ||
      (typeof data === "string" ? data : `HTTP ${res.status}`);
    const err = new Error(message);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return { data, headers: res.headers };
}

export const api = {
  uuid,
  listMerchants: () => request("/api/v1/merchants/").then((r) => r.data),
  getBalance: (merchantId) =>
    request("/api/v1/merchants/me/balance/", { merchantId }).then((r) => r.data),
  getBankAccounts: (merchantId) =>
    request("/api/v1/merchants/me/bank-accounts/", { merchantId }).then((r) => r.data),
  getLedger: (merchantId) =>
    request("/api/v1/merchants/me/ledger/", { merchantId }).then((r) => r.data),
  listPayouts: (merchantId) =>
    request("/api/v1/payouts/", { merchantId }).then((r) => r.data),
  createPayout: (merchantId, { amount_paise, bank_account_id, idempotencyKey }) =>
    request("/api/v1/payouts/", {
      merchantId,
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: { amount_paise, bank_account_id },
    }).then((r) => ({
      payout: r.data,
      replayed: r.headers.get("Idempotent-Replayed") === "true",
    })),
};
