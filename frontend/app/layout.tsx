import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Ariadne Runtime",
  description: "Terminal control surface for Ariadne agent runs",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
