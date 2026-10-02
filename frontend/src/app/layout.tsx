import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stock Grader",
  description: "Personal research tool for NSE-listed equities",
};

// Follow the OS colour scheme (shadcn's `.dark` class) before first paint.
const themeScript = `(() => {
  const m = window.matchMedia("(prefers-color-scheme: dark)");
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch (e) {}
  const apply = () => document.documentElement.classList.toggle("dark", saved ? saved === "dark" : m.matches);
  apply();
  m.addEventListener("change", apply);
})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="antialiased">{children}</body>
    </html>
  );
}
