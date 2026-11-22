"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { engine, displayName, kindLabel, type Item, type Kind, type Shelf } from "lib/engine";
import { EmptyState, Loading } from "app/components/ui";
import { Sprig } from "app/components/ornaments";
import { Cover, Volume } from "app/components/volume";

const kindPath: Record<Kind, string> = {
  notebook: "/notebooks",
  book: "/books",
  screenshot: "/screenshots",
};

/**
 * Shelves you make yourself: create one, put notebooks and books on it,
 * rename or remove it. An item lives on one shelf at a time.
 */
export function Shelves() {
  const router = useRouter();
  const [shelves, setShelves] = useState<Shelf[] | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [lib, sh] = await Promise.all([engine.library(), engine.shelves()]);
      setItems(lib.items);
      setShelves(sh.shelves);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <EmptyState>The engine isn&apos;t answering ({error}).</EmptyState>;
  if (!shelves) return <Loading>Tending the shelves</Loading>;

  const unshelved = items.filter((i) => !i.shelf).length;

  return (
    <div>
      <NewShelf onCreated={load} />

      {shelves.length === 0 ? (
        <div className="mt-12">
          <EmptyState>
            No shelves yet. Make one above, then put notebooks and books on it.
          </EmptyState>
        </div>
      ) : (
        shelves.map((shelf) => (
          <ShelfBed
            key={shelf.id}
            shelf={shelf}
            items={items.filter((i) => i.shelf?.toLowerCase() === shelf.name.toLowerCase())}
            allItems={items}
            adding={adding === shelf.id}
            onToggleAdd={() => setAdding((a) => (a === shelf.id ? null : shelf.id))}
            onOpen={(item) => router.push(`${kindPath[item.kind]}?open=${item.id}`)}
            onChanged={load}
          />
        ))
      )}

      {shelves.length > 0 && (
        <p className="mt-14 text-center faint italic">
          {unshelved} item{unshelved === 1 ? "" : "s"} not on a shelf yet ·{" "}
          <a className="text-link" href="/">
            see the whole library
          </a>
        </p>
      )}
    </div>
  );
}

function NewShelf({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    const r = await engine.createShelf(name, description);
    setBusy(false);
    if (r.error) {
      setError(r.error);
      return;
    }
    setName("");
    setDescription("");
    setError(null);
    onCreated();
  }

  return (
    <form onSubmit={create} className="card !mx-auto max-w-[44rem] !p-5">
      <p className="eyebrow mb-3">A new shelf</p>
      <div className="flex flex-col gap-3 sm:flex-row">
        <input
          className="field sm:max-w-[16rem]"
          placeholder="Name, e.g. Journal"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Shelf name"
        />
        <input
          className="field"
          placeholder="What grows here (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          aria-label="Shelf description"
        />
        <button className="btn btn-solid shrink-0" type="submit" disabled={!name.trim() || busy}>
          Create
        </button>
      </div>
      {error && <p className="mt-2 muted italic text-[0.95rem]">{error}</p>}
    </form>
  );
}

function ShelfBed({
  shelf,
  items,
  allItems,
  adding,
  onToggleAdd,
  onOpen,
  onChanged,
}: {
  shelf: Shelf;
  items: Item[];
  allItems: Item[];
  adding: boolean;
  onToggleAdd: () => void;
  onOpen: (item: Item) => void;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(shelf.name);
  const [description, setDescription] = useState(shelf.description ?? "");
  const [error, setError] = useState<string | null>(null);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const r = await engine.updateShelf(shelf.id, { name, description });
    if (r.error) return setError(r.error);
    setError(null);
    setEditing(false);
    onChanged();
  }

  async function remove() {
    const what = items.length
      ? ` Its ${items.length} item${items.length === 1 ? "" : "s"} stay in the library, unshelved.`
      : "";
    if (!window.confirm(`Remove the shelf "${shelf.name}"?${what}`)) return;
    await engine.deleteShelf(shelf.id);
    onChanged();
  }

  async function takeOff(item: Item) {
    await engine.shelveItems(shelf.id, { remove: [item.id] });
    onChanged();
  }

  const counts = [
    shelf.notebooks && `${shelf.notebooks} notebook${shelf.notebooks === 1 ? "" : "s"}`,
    shelf.books && `${shelf.books} book${shelf.books === 1 ? "" : "s"}`,
    shelf.screenshots && `${shelf.screenshots} screenshot${shelf.screenshots === 1 ? "" : "s"}`,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section className="mt-16">
      {editing ? (
        <form onSubmit={save} className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center">
          <input className="field sm:max-w-[16rem]" value={name} onChange={(e) => setName(e.target.value)} aria-label="Shelf name" />
          <input
            className="field"
            value={description}
            placeholder="What grows here (optional)"
            onChange={(e) => setDescription(e.target.value)}
            aria-label="Shelf description"
          />
          <div className="flex gap-2 shrink-0">
            <button className="btn btn-solid" type="submit">
              Save
            </button>
            <button className="btn btn-quiet" type="button" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
          {error && <p className="muted italic text-[0.95rem]">{error}</p>}
        </form>
      ) : (
        <div className="mb-5 flex flex-wrap items-baseline gap-x-4 gap-y-2">
          <h2 className="eyebrow !text-[0.9rem]">
            <Sprig height="1.8em" className="mr-2 -mt-1.5 align-middle" style={{ color: "var(--gold)" }} />
            {shelf.name}
          </h2>
          <span className="faint italic text-[0.95rem]">{counts || "empty"}</span>
          <span className="sm:ml-auto flex gap-1">
            <button className="btn" onClick={onToggleAdd} aria-expanded={adding}>
              {adding ? "Done adding" : "Add items"}
            </button>
            <button className="btn btn-quiet" onClick={() => setEditing(true)}>
              Rename
            </button>
            <button className="btn btn-quiet" onClick={remove}>
              Remove
            </button>
          </span>
          {shelf.description && <p className="basis-full muted italic -mt-1">{shelf.description}</p>}
        </div>
      )}

      {adding && <Picker shelf={shelf} items={allItems} onAdded={onChanged} />}

      {items.length === 0 ? (
        !adding && (
          <p className="faint italic border border-dashed border-[var(--border)] py-8 text-center">
            Nothing planted here yet.{" "}
            <button className="text-link" onClick={onToggleAdd}>
              Add notebooks or books
            </button>
          </p>
        )
      ) : (
        <div className="shelf">
          {items.map((item) => (
            <div key={item.id} className="relative group">
              <Volume item={item} onOpen={() => onOpen(item)} />
              <button
                className="btn btn-icon absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 focus:opacity-100 !bg-[var(--bg)]"
                title={`Take off ${shelf.name}`}
                aria-label={`Take ${displayName(item)} off ${shelf.name}`}
                onClick={() => takeOff(item)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/* Choose items to put on a shelf. Items on another shelf move here. */
function Picker({ shelf, items, onAdded }: { shelf: Shelf; items: Item[]; onAdded: () => void }) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<Kind | "all">("all");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items
      .filter((i) => i.shelf?.toLowerCase() !== shelf.name.toLowerCase())
      .filter((i) => kind === "all" || i.kind === kind)
      .filter(
        (i) =>
          !q ||
          [displayName(i), i.name, ...(i.authors ?? [])].some((s) => s?.toLowerCase().includes(q)),
      )
      .sort((a, b) => a.kind.localeCompare(b.kind) || displayName(a).localeCompare(displayName(b)));
  }, [items, shelf.name, kind, query]);

  function toggle(id: string) {
    setChosen((c) => {
      const next = new Set(c);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function add() {
    setBusy(true);
    await engine.shelveItems(shelf.id, { add: [...chosen] });
    setBusy(false);
    setChosen(new Set());
    onAdded();
  }

  return (
    <div className="card !p-4 mb-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center mb-3">
        <input
          type="search"
          className="field sm:max-w-[20rem]"
          placeholder="Find a notebook or book…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Find items to add"
          autoFocus
        />
        <div className="flex gap-4">
          {(["all", "notebook", "book", "screenshot"] as const).map((k) => (
            <button key={k} className="chip" aria-pressed={kind === k} onClick={() => setKind(k)}>
              {k === "all" ? "All" : `${kindLabel[k]}s`}
            </button>
          ))}
        </div>
        <button className="btn btn-solid sm:ml-auto" onClick={add} disabled={!chosen.size || busy}>
          {busy ? "Adding…" : `Add ${chosen.size || ""} to ${shelf.name}`}
        </button>
      </div>
      <ul className="max-h-[22rem] overflow-y-auto divide-y divide-[var(--border)]" style={{ scrollbarWidth: "thin" }}>
        {candidates.length === 0 && <li className="py-4 faint italic text-center">Nothing else to add.</li>}
        {candidates.map((item) => (
          <li key={item.id}>
            <label className="flex items-center gap-3 py-2 cursor-pointer">
              <input
                type="checkbox"
                checked={chosen.has(item.id)}
                onChange={() => toggle(item.id)}
                className="accent-[var(--text)]"
              />
              <span className="relative w-7 h-9 shrink-0 overflow-hidden border border-[var(--border)] [&_img]:w-full [&_img]:h-full [&_img]:object-cover [&_.binding-title]:hidden [&_.binding-sub]:hidden">
                <Cover item={item} />
              </span>
              <span className="min-w-0 flex-1 truncate">{displayName(item)}</span>
              <span className="faint italic text-[0.85rem] shrink-0">
                {kindLabel[item.kind]}
                {item.shelf ? ` · on ${item.shelf}` : ""}
              </span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}
