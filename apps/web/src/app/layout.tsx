import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Surge Research Platform",
  description: "End-of-day screening, materials and analysis. Research only.",
};

const TABS = [
  { href: "/", label: "Dashboard" },
  { href: "/universe", label: "Universe" },
  { href: "/materials", label: "Materials" },
  { href: "/watch", label: "Watch & setup" },
  { href: "/episodes", label: "Episodes" },
  { href: "/entries", label: "Entry decisions" },
  { href: "/results", label: "Results" },
  { href: "/coverage", label: "Coverage" },
  { href: "/diagnostics", label: "Pipeline" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="masthead">
            <h1>Surge Research Platform</h1>
            <nav className="tabs">
              {TABS.map((tab) => (
                <Link key={tab.href} href={tab.href}>
                  {tab.label}
                </Link>
              ))}
            </nav>
          </header>
          {children}
          <footer className="colophon">
            Research output. Nothing on these screens is an entry decision: end-of-day analysis stops at
            setup, watch or reject, and no state here authorises a trade. Prior highs appear as obstacles,
            never as targets.
          </footer>
        </div>
      </body>
    </html>
  );
}
