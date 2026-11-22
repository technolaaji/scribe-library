"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { engine, displayName, type Item, type Kind, type LibraryResponse } from "lib/engine";
import { EmptyState, Loading, Section } from "app/components/ui";
import { Volume } from "app/components/volume";
import { Plate } from "app/components/plate";

type Sort = "changed" | "name" | "added";

const kindPlural: Record<Kind, string> = {
  notebook: "notebooks",
  book: "books",
  screenshot: "screenshots",
};

/**
 * The library: every item as a volume on a shelf. Clicking a volume opens it
 * in place as a full-width plate holding the reader and its details.
 * `kind` limits the shelf to one kind (the Notebooks / Books / Screenshots
 * pages); without it, everything is shown with a kind filter.
 */
export function Library({ kind }: { kind?: Kind }) {
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<Kind | "all">(kind ?? "all");
  const [onlyFresh, setOnlyFresh] = useState(false);
  const [shelf, setShelf] = useState<string | null>(null);
  const [sort, setSort] = useState<Sort>("changed");
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await engine.library());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const params = new URLSearchParams(window.location.search);
    const open = params.get("open");
    if (open) setOpenId(open);
    if (params.get("fresh")) setOnlyFresh(true);
  }, [load]);

  // Keep ?open=<id> in the URL so a plate can be linked to and survives reload.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (openId) url.searchParams.set("open", openId);
    else url.searchParams.delete("open");
    window.history.replaceState(null, "", url);
  }, [openId]);

  const items = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    const list = data.items.filter((item) => {
      if (kindFilter !== "all" && item.kind !== kindFilter) return false;
      if (onlyFresh && item.status === "seen") return false;
      if (shelf && item.shelf !== shelf) return false;
      if (!q) return true;
      return [displayName(item), item.name, item.shelf, item.note, ...(item.authors ?? [])]
        .filter(Boolean)
        .some((s) => String(s).toLowerCase().includes(q));
    });
    list.sort((a, b) => {
      if (sort === "name") return displayName(a).localeCompare(displayName(b));
      if (sort === "added") return (b.first_seen ?? "").localeCompare(a.first_seen ?? "");
      // new & updated first, then most recently edited on the device
      const fresh = Number(b.status !== "seen") - Number(a.status !== "seen");
      return fresh || (b.modified ?? "").localeCompare(a.modified ?? "");
    });
    return list;
  }, [data, query, kindFilter, onlyFresh, shelf, sort]);

  const scoped = useMemo(
    () => (data ? data.items.filter((i) => kindFilter === "all" || i.kind === kindFilter) : []),
    [data, kindFilter],
  );
  const freshCount = scoped.filter((i) => i.status !== "seen").length;
  const shelves = useMemo(() => {
    const counts = new Map<string, number>();
    for (const i of scoped) if (i.shelf) counts.set(i.shelf, (counts.get(i.shelf) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [scoped]);

  // Group by shelf when you've made shelves and aren't already filtering to one.
  const groups = useMemo(() => {
    if (shelf || shelves.length === 0) return [{ name: null as string | null, items }];
    const byShelf = new Map<string | null, Item[]>();
    for (const i of items) {
      const key = i.shelf ?? null;
      byShelf.set(key, [...(byShelf.get(key) ?? []), i]);
    }
    return [...byShelf.entries()]
      .sort((a, b) => (a[0] === null ? 1 : b[0] === null ? -1 : a[0].localeCompare(b[0])))
      .map(([name, list]) => ({ name, items: list }));
  }, [items, shelf, shelves.length]);

  const updateItem = useCallback((next: Partial<Item> & { id: string }) => {
    setData((d) =>
      d ? { ...d, items: d.items.map((i) => (i.id === next.id ? { ...i, ...next } : i)) } : d,
    );
  }, []);

  const removeItem = useCallback((id: string) => {
    setData((d) => (d ? { ...d, items: d.items.filter((i) => i.id !== id) } : d));
    setOpenId(null);
  }, []);

  const close = useCallback(() => {
    // the badge stays while the plate is open, and clears once you close it
    if (openId) updateItem({ id: openId, status: "seen" });
    setOpenId(null);
  }, [openId, updateItem]);

  if (error) {
    return (
      <EmptyState>
        The engine isn&apos;t answering ({error}).
        <br />
        Start it with <span className="kbd">npm run dev</span> or{" "}
        <span className="kbd">python3 engine/server.py</span>.
      </EmptyState>
    );
  }
  if (!data) return <Loading />;

  const allShelfNames = data.shelves.map((s) => s.name);
  const noun = kindFilter === "all" ? "items" : kindPlural[kindFilter];

  return (
    <div>
      <div className="mb-10 flex flex-col gap-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <input
            type="search"
            className="field sm:max-w-[22rem]"
            placeholder={`Search ${noun}…`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label={`Search ${noun}`}
          />
          <div className="flex items-center gap-2 sm:ml-auto">
            <label className="field-label !mb-0" htmlFor="sort">
              Order
            </label>
            <select
              id="sort"
              className="field !w-auto !py-1.5"
              value={sort}
              onChange={(e) => setSort(e.target.value as Sort)}
            >
              <option value="changed">Recently changed</option>
              <option value="added">Recently added</option>
              <option value="name">By name</option>
            </select>
          </div>
        </div>

        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          {!kind &&
            (["all", "notebook", "book", "screenshot"] as const).map((k) => (
              <button
                key={k}
                className="chip"
                aria-pressed={kindFilter === k}
                onClick={() => setKindFilter(k)}
              >
                {k === "all" ? "Everything" : kindPlural[k][0].toUpperCase() + kindPlural[k].slice(1)}
                <span className="chip-count">
                  {k === "all" ? data.items.length : data.items.filter((i) => i.kind === k).length}
                </span>
              </button>
            ))}
          {!kind && <span className="faint" aria-hidden>·</span>}
          <button className="chip" aria-pressed={onlyFresh} onClick={() => setOnlyFresh((v) => !v)}>
            New &amp; updated<span className="chip-count">{freshCount}</span>
          </button>
          {shelves.length > 0 && <span className="faint" aria-hidden>·</span>}
          {shelves.map(([name, count]) => (
            <button
              key={name}
              className="chip"
              aria-pressed={shelf === name}
              onClick={() => setShelf((s) => (s === name ? null : name))}
            >
              {name}
              <span className="chip-count">{count}</span>
            </button>
          ))}
        </div>
      </div>

      {items.length === 0 ? (
        <EmptyState>
          {data.items.length === 0 ? (
            <>
              Nothing here yet. Plug in your Scribe, unlock it, and sync from the{" "}
              <a className="link" href="/device">
                Device
              </a>{" "}
              page.
            </>
          ) : (
            <>No {noun} match.</>
          )}
        </EmptyState>
      ) : (
        groups.map((group) => {
          const shelfEl = (
            <div className="shelf" data-kind={kindFilter === "screenshot" ? "screenshot" : undefined}>
              {group.items.map((item) =>
                item.id === openId ? (
                  <Plate
                    key={item.id}
                    item={item}
                    shelves={allShelfNames}
                    onClose={close}
                    onChange={updateItem}
                    onRemoved={removeItem}
                  />
                ) : (
                  <Volume key={item.id} item={item} onOpen={() => setOpenId(item.id)} />
                ),
              )}
            </div>
          );
          return groups.length === 1 && group.name === null ? (
            <div key="all">{shelfEl}</div>
          ) : (
            <Section
              key={group.name ?? "__none"}
              label={group.name ?? "Unshelved"}
              aside={<span className="faint italic text-[0.95rem]">{group.items.length}</span>}
            >
              {shelfEl}
            </Section>
          );
        })
      )}
    </div>
  );
}
