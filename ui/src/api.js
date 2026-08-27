async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${options.method || "GET"} ${path} -> ${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  getSlaves: () => request("/slaves"),
  createSlave: (payload) => request("/slaves", { method: "POST", body: JSON.stringify(payload) }),
  patchSlave: (id, payload) => request(`/slaves/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteSlave: (id) => request(`/slaves/${id}`, { method: "DELETE" }),
  getTrades: (limit = 200) => request(`/trades?limit=${limit}`),
  getStatus: () => request("/status"),
  getConfig: () => request("/config"),
  patchConfig: (payload) => request("/config", { method: "PATCH", body: JSON.stringify(payload) }),
  getLogs: (lines = 200) => request(`/logs?lines=${lines}`),
  getRisk: () => request("/risk"),
  patchRisk: (payload) => request("/risk", { method: "PATCH", body: JSON.stringify(payload) }),
};
