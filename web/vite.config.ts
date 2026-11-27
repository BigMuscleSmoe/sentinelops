import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The page's Content-Security-Policy. Deployed, a viewer-response CloudFront
// Function sends it as a header, with frame-ancestors 'none' added (a meta tag
// can't carry that directive), beside nosniff, Referrer-Policy, X-Frame-Options
// DENY, and HSTS (custom response headers policies need the Business plan).
// The built page carries it as a meta tag too, so a local preview runs under
// the same rules. Trusted Types make any string written to an HTML sink throw,
// so text can't become markup anywhere on the page.
export const CSP = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self'",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "object-src 'none'",
  "require-trusted-types-for 'script'",
  "trusted-types 'none'",
].join("; ");

function cspMeta(): Plugin {
  return {
    name: "csp-meta",
    apply: "build", // dev mode's React Refresh needs an inline script
    transformIndexHtml: () => [
      { tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: CSP }, injectTo: "head-prepend" },
    ],
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), cspMeta()],
  build: {
    assetsInlineLimit: 0, // no data: URIs, which img-src 'self' refuses
    modulePreload: { polyfill: false },
  },
});
