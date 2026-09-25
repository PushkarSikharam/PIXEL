import type { Metadata } from "next";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import type { ReactNode } from "react";
import { ToastProvider } from "@pixel-console/components/toast";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Pixel", template: "%s | Pixel" },
  description: "Create product workspaces, manage records, and work with Edith inside each product.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
