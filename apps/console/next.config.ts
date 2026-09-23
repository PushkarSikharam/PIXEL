import type { NextConfig } from "next";

// The one place the console may reach out to: the Pixel API it was pointed at, and nothing
// else. Unset, the console talks to nobody and shows its own sample data.
const apiBaseUrl = process.env.NEXT_PUBLIC_PIXEL_API_BASE_URL?.trim();
const apiOrigin = apiBaseUrl?.startsWith("http") ? new URL(apiBaseUrl).origin : null;

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    const backend = process.env.PIXEL_AGENT_API_BASE_URL?.trim() ?? "http://127.0.0.1:8001/api";
    return [{ source: "/api/agent/:path*", destination: `${backend.replace(/\/$/, "")}/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data:",
              "font-src 'self'",
              "media-src 'self' blob:",
              ["connect-src 'self' ws: wss:", apiOrigin].filter(Boolean).join(" "),
              "frame-ancestors 'none'",
              "base-uri 'none'",
              "form-action 'self'"
            ].join("; ")
          }
        ]
      }
    ];
  }
};

export default nextConfig;
