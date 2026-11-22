import Link from "next/link";
import type { Status } from "lib/engine";
import { Fleuron, Sprig } from "app/components/ornaments";

/* Page header used at the top of every section page. */
export function PageHeader({
  title,
  intro,
  children,
}: {
  title: string;
  intro?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <header className="mb-12 text-center">
      <h1 className="page-title title">{title}</h1>
      <Fleuron className="mt-4" />
      {intro && (
        <p className="mx-auto mt-4 max-w-[34rem] italic muted text-[1.15rem] leading-relaxed">
          {intro}
        </p>
      )}
      {children}
    </header>
  );
}

/* A labelled section, optionally with a "see all" link. */
export function Section({
  label,
  href,
  linkText = "See all",
  aside,
  children,
}: {
  label: string;
  href?: string;
  linkText?: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-14 first:mt-0">
      <div className="mb-5 flex items-baseline justify-between gap-4">
        <h2 className="eyebrow">
          <Sprig height="1.7em" className="mr-2 -mt-1.5 align-middle" style={{ color: "var(--gold)" }} />
          {label}
        </h2>
        {aside}
        {href && (
          <Link href={href} className="text-link text-[0.95rem]">
            {linkText} <span aria-hidden>→</span>
          </Link>
        )}
      </div>
      {children}
    </section>
  );
}

const statusText: Record<string, string> = {
  new: "New",
  updated: "Updated",
  added: "Added",
  removed: "Removed",
  restored: "Restored",
  renamed: "Renamed",
};

export function StatusPill({ status }: { status: Status | string }) {
  if (status === "seen") return null;
  return (
    <span className="pill" data-status={status}>
      <span className="pill-dot" aria-hidden />
      {statusText[status] ?? status}
    </span>
  );
}

export function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="card muted italic text-center py-8">{children}</div>;
}

export function Loading({ children = "Opening the shelves" }: { children?: React.ReactNode }) {
  return <p className="loading-line text-center py-16">{children}</p>;
}
