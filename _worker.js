/**
 * Cloudflare Worker — CaseOS
 *
 * Funções:
 * 1. Serve arquivos estáticos (via env.ASSETS)
 * 2. Cron trigger: pinga o backend Render a cada 5 min para evitar hibernação
 */

const BACKEND_HEALTH = 'https://caseos-api-bhdx.onrender.com/health';

export default {
  // ── Requisições HTTP normais → serve assets estáticos ──────────────────
  async fetch(request, env) {
    return env.ASSETS.fetch(request);
  },

  // ── Cron trigger: acorda o Render antes de hibernar ────────────────────
  async scheduled(event, env, ctx) {
    try {
      const res = await fetch(BACKEND_HEALTH, {
        headers: { 'User-Agent': 'CaseOS-KeepAlive/1.0' },
        signal: AbortSignal.timeout(20000),
      });
      console.log(`[KeepAlive] ${new Date().toISOString()} → ${res.status}`);
    } catch (err) {
      console.error(`[KeepAlive] Falha: ${err.message}`);
    }
  },
};
