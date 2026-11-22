"use client";

import { useEffect, useState } from "react";
import { engine, formatDate, type Change, type Summary } from "lib/engine";
import { EmptyState, Loading, StatusPill } from "app/components/ui";

const kindPath = { notebook: "/notebooks", book: "/books", screenshot: "/screenshots" } as const;

/* Every change the registry recorded, newest first, grouped by day. */
export function ChangeLog() {
  const [data, setData] = useState<{ changes: Change[]; summary: Summary } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setData(await engine.changes());
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    load();
  }, []);

  if (error) return <EmptyState>The engine isn&apos;t answering ({error}).</EmptyState>;
  if (!data) return <Loading>Reading the ledger</Loading>;

  const days = new Map<string, Change[]>();
  for (const c of data.changes) {
    const day = c.at.slice(0, 10);
    days.set(day, [...(days.get(day) ?? []), c]);
  }

  return (
    <div>
      <div className="mb-8 flex flex-wrap items-baseline justify-between gap-3">
        <p className="muted italic">
          {data.summary.unseen
            ? `${data.summary.unseen} item${data.summary.unseen === 1 ? "" : "s"} new or updated since you last opened them.`
            : "You're up to date."}
        </p>
        <div className="flex gap-2">
          {data.summary.unseen > 0 && (
            <>
              <a className="btn" href="/?fresh=1">
                Show them
              </a>
              <button
                className="btn"
                onClick={async () => {
                  await engine.markAllSeen();
                  load();
                }}
              >
                Mark all as seen
              </button>
            </>
          )}
        </div>
      </div>

      {data.changes.length === 0 ? (
        <EmptyState>
          No changes yet. The first sync sets the baseline; after that, every new notebook and every page
          you write shows up here.
        </EmptyState>
      ) : (
        [...days.entries()].map(([day, changes]) => (
          <section key={day} className="mb-10">
            <h2 className="eyebrow mb-3">{formatDate(day, "long")}</h2>
            {changes.map((c) => (
              <a
                key={c.id}
                href={c.change === "removed" ? undefined : `${kindPath[c.kind]}?open=${c.item_id}`}
                className="row"
              >
                <span className="w-[6.5rem] shrink-0">
                  <StatusPill status={c.change} />
                </span>
                <span className="row-title">
                  {c.name}
                  <span className="faint italic text-[0.9rem]"> · {c.kind}</span>
                </span>
                <span className="row-leader" aria-hidden />
                <span className="row-meta">
                  {c.detail ? `${c.detail} · ` : ""}
                  {new Date(c.at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}
                </span>
              </a>
            ))}
          </section>
        ))
      )}
    </div>
  );
}
