"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { site } from "site.config";
import { Fleuron, Garden } from "app/components/ornaments";

/* Title-page masthead: the name in Roman capitals, a fleuron, the contents. */
export function Navbar() {
  const pathname = usePathname();

  return (
    <header className="pt-10 pb-12 text-center sm:pt-12 sm:pb-14">
      <Garden width={76} className="mx-auto mb-3 text-[var(--muted)]" />
      <Link href="/" className="site-name inline-block">
        {site.name}
      </Link>
      <Fleuron className="mt-3" height={24} />
      <nav aria-label="Main" className="mt-4">
        <ul className="flex flex-wrap justify-center gap-x-4 gap-y-2 sm:gap-x-1">
          {site.nav.map(({ href, label }, i) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <li key={href} className="flex items-center gap-x-1">
                {i > 0 && (
                  <span className="faint text-xs px-1.5 hidden sm:inline" aria-hidden>
                    ·
                  </span>
                )}
                <Link href={href} className="nav-link" aria-current={active ? "page" : undefined}>
                  {label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
