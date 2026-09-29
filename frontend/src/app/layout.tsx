import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stock Grader",
  description: "Personal research tool for NSE-listed equities",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
