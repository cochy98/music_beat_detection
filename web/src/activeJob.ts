const key = "beat-active-job";
export function loadActiveJob(): string {
  try {
    const id = sessionStorage.getItem(key) ?? "";
    return /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id) ? id : "";
  } catch { return ""; }
}
export function rememberActiveJob(id: string) {
  try { sessionStorage.setItem(key, id); } catch { /* Current page remains usable. */ }
}
export function forgetActiveJob(id: string) {
  try { if (sessionStorage.getItem(key) === id) sessionStorage.removeItem(key); } catch { /* Storage disabled. */ }
}
