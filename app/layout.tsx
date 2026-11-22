import "./global.css";
import type { Metadata } from "next";
import { GeistMono } from "geist/font/mono";
import { Cinzel, EB_Garamond } from "next/font/google";
import { site } from "site.config";
import { Navbar } from "app/components/nav";
import Footer from "app/components/footer";

// Body text: Garamond, the type of the 16th-century printers.
const garamond = EB_Garamond({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-garamond",
  display: "swap",
});

// Display capitals: modelled on Roman inscriptions (Trajan's Column).
const cinzel = Cinzel({
  subsets: ["latin"],
  variable: "--font-cinzel",
  display: "swap",
});

export const viewport = {
  themeColor: "#000000",
  colorScheme: "dark",
};

export const metadata: Metadata = {
  title: {
    default: site.name,
    template: `%s · ${site.name}`,
  },
  description: site.description,
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={[GeistMono.variable, garamond.variable, cinzel.variable].join(" ")}
    >
      <body className="antialiased">
        <div className="mx-auto w-full max-w-[78rem] px-5 sm:px-8">
          <Navbar />
          <main className="min-w-0 pb-8">{children}</main>
          <Footer />
        </div>
      </body>
    </html>
  );
}
