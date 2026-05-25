/**
 * Cloudflare Worker — CaseOS
 *
 * Funções:
 * 1. Serve arquivos estáticos (via env.ASSETS) com security headers
 * 2. Cron trigger: pinga o backend Render a cada 5 min para evitar hibernação
 *
 * Security headers adicionados em todas as respostas de assets:
 *   - Content-Security-Policy  (XSS / injeção de recursos)
 *   - X-Frame-Options          (clickjacking)
 *   - X-Content-Type-Options   (MIME sniffing)
 *   - Referrer-Policy          (vazamento de URL)
 *   - Strict-Transport-Security (HSTS — força HTTPS)
 *   - Permissions-Policy       (APIs sensíveis do browser)
 */

const BACKEND_HEALTH = 'https://caseos-api-bhdx.onrender.com/health';
const BACKEND_ORIGIN = 'https://caseos-api-bhdx.onrender.com';
const ASSETS_ORIGIN  = 'https://caseos.voandonaia.com';

/**
 * Content-Security-Policy cuidadosamente construída para o CaseOS:
 *
 *  script-src  'unsafe-inline' — necessário pois os HTMLs usam <script> inline extensivamente.
 *              Quando migrar para JS modular (bundle), remover 'unsafe-inline' e usar 'strict-dynamic'.
 *
 *  connect-src — inclui wss:// para o WebSocket de acompanhamento de geração.
 *
 *  img-src     — lh3.googleusercontent.com: fotos de perfil do Google OAuth.
 *                caseos.voandonaia.com: assets de e-mail / pixel chars.
 *
 *  form-action — permite redirect para Stripe Checkout.
 */
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com",
  `img-src 'self' data: https://lh3.googleusercontent.com ${ASSETS_ORIGIN}`,
  `connect-src 'self' ${BACKEND_ORIGIN} ${BACKEND_ORIGIN.replace('https://', 'wss://')}`,
  "frame-src 'none'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self' https://checkout.stripe.com",
  "upgrade-insecure-requests",
].join('; ');

/**
 * Copia a resposta original e injeta os security headers.
 * Não modifica respostas de redirecionamento (3xx).
 */
function _addSecurityHeaders(response) {
  // Não toca em redirects — headers de segurança são para conteúdo, não location
  if (response.status >= 300 && response.status < 400) return response;

  const r = new Response(response.body, response);
  r.headers.set('Content-Security-Policy',   CSP);
  r.headers.set('X-Frame-Options',           'DENY');
  r.headers.set('X-Content-Type-Options',    'nosniff');
  r.headers.set('Referrer-Policy',           'strict-origin-when-cross-origin');
  r.headers.set('Permissions-Policy',
    'camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()');
  // HSTS: 2 anos, inclui subdomains, apto para preload list
  r.headers.set('Strict-Transport-Security', 'max-age=63072000; includeSubDomains; preload');
  // Remove header que expõe stack do servidor (Cloudflare costuma incluir)
  r.headers.delete('Server');
  return r;
}

export default {
  // ── Requisições HTTP normais → serve assets estáticos + security headers ──
  async fetch(request, env) {
    const url = new URL(request.url);

    // Redireciona raiz e /landing → dashboard (sem security headers no redirect)
    if (url.pathname === '/' || url.pathname === '/landing') {
      url.pathname = '/dashboard.html';
      return Response.redirect(url.toString(), 302);
    }

    if (url.pathname === '/review-studio') {
      url.pathname = '/review-studio.html';
      const response = await env.ASSETS.fetch(new Request(url.toString(), request));
      return _addSecurityHeaders(response);
    }

    const response = await env.ASSETS.fetch(request);
    return _addSecurityHeaders(response);
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
