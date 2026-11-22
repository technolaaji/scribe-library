"use client";

import { useEffect, useRef, useState } from "react";
import {
  engine,
  displayName,
  formatDate,
  kindLabel,
  timeAgo,
  type Change,
  type Item,
} from "lib/engine";
import { StatusPill } from "app/components/ui";
import { Reader, type Mark } from "app/components/reader";
import { Marginalia } from "app/components/marginalia";

/**
 * An opened item: the reader on the left, and on the right your name for it,
 * its shelf and note, what's changed, and (for books) its highlights and
 * handwritten notes.
 */
export function Plate({
  item,
  shelves,
  onClose,
  onChange,
  onRemoved,
}: {
  item: Item;
  shelves: string[];
  onClose: () => void;
  onChange: (next: Partial<Item> & { id: string }) => void;
  onRemoved: (id: string) => void;
}) {
  const ref = useRef<HTMLElement>(null);
  const [history, setHistory] = useState<Change[]>([]);
  // side panel ⇄ reader: which page to turn to, and which pages carry notes
  const [readerReady, setReaderReady] = useState(false);
  const [target, setTarget] = useState<{ page: number; n: number; index?: number } | null>(null);
  const [marks, setMarks] = useState<Mark[]>([]);
  const jump = (page: number, index?: number) =>
    setTarget((t) => ({ page, index, n: (t?.n ?? 0) + 1 }));

  useEffect(() => {
    ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    engine.opened(item.id).catch(() => {});
    engine.item(item.id).then((d) => setHistory(d.history ?? [])).catch(() => {});
  }, [item.id]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      if ((e.target as HTMLElement).closest("input, textarea, select")) return;
      onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <article className="plate" ref={ref} aria-label={displayName(item)}>
      <header className="flex items-start gap-4 px-5 pt-4 pb-3 border-b border-[var(--border)]">
        <div className="min-w-0 flex-1">
          <div className="eyebrow !text-[0.66rem] faint flex items-center gap-2">
            {kindLabel[item.kind]}
            {item.kind === "book" && item.format ? ` · ${item.format}` : ""}
            <StatusPill status={item.status} />
          </div>
          <h2 className="mt-1 text-[1.6rem] leading-tight title">{displayName(item)}</h2>
          {item.kind === "book" && (item.authors ?? []).length > 0 && (
            <p className="muted italic">{item.authors!.join(", ")}</p>
          )}
        </div>
        <button className="btn btn-icon" onClick={onClose} title="Close (esc)" aria-label="Close">
          ✕
        </button>
      </header>

      <div className="plate-grid">
        <Reader item={item} active target={target} marks={marks} onReady={() => setReaderReady(true)} />
        <aside className="plate-side">
          <Details item={item} shelves={shelves} onChange={onChange} />

          {item.kind === "book" && (
            <SideSection title="Highlights & notes">
              <Marginalia id={item.id} readerReady={readerReady} onJump={jump} onMarks={setMarks} />
            </SideSection>
          )}

          <SideSection title="Record">
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[0.95rem]">
              <Fact label="On device">{formatDate(item.modified, "long")}</Fact>
              {item.first_seen && <Fact label="First synced">{formatDate(item.first_seen, "long")}</Fact>}
              {item.last_changed && <Fact label="Last change">{timeAgo(item.last_changed)}</Fact>}
              <Fact label="Version">{item.version}</Fact>
              <Fact label="Size">{item.size_mb < 1 ? `${Math.round(item.size_mb * 1024)} KB` : `${item.size_mb} MB`}</Fact>
              {item.uuid && (
                <Fact label="Id">
                  <span className="font-mono text-[0.72rem] break-all faint">{item.uuid}</span>
                </Fact>
              )}
              {item.kind === "book" && item.source && <Fact label="Source">{item.source}</Fact>}
            </dl>
            {history.length > 0 && (
              <ul className="mt-4 space-y-1 text-[0.9rem]">
                {history.slice(0, 8).map((c) => (
                  <li key={c.id} className="flex gap-2">
                    <span className="faint italic w-[5.5rem] shrink-0">{formatDate(c.at)}</span>
                    <span className="muted">
                      {c.change}
                      {c.detail ? ` · ${c.detail}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </SideSection>

          {item.kind === "screenshot" && (
            <SideSection title="Remove">
              <DeleteScreenshot item={item} onRemoved={onRemoved} />
            </SideSection>
          )}
        </aside>
      </div>
    </article>
  );
}

/* Your name, shelf and note for the item — saved to the local registry. */
function Details({
  item,
  shelves,
  onChange,
}: {
  item: Item;
  shelves: string[];
  onChange: (next: Partial<Item> & { id: string }) => void;
}) {
  const [name, setName] = useState(item.display_name ?? "");
  const [shelf, setShelf] = useState(item.shelf ?? "");
  const [note, setNote] = useState(item.note ?? "");
  const [saved, setSaved] = useState<"idle" | "saving" | "saved">("idle");

  const dirty =
    name !== (item.display_name ?? "") || shelf !== (item.shelf ?? "") || note !== (item.note ?? "");

  async function save(e?: React.FormEvent) {
    e?.preventDefault();
    if (!dirty) return;
    setSaved("saving");
    const next = await engine.update(item.id, { display_name: name, shelf, note });
    onChange({
      id: item.id,
      display_name: next.display_name,
      shelf: next.shelf,
      note: next.note,
      label: next.label,
    });
    setSaved("saved");
    setTimeout(() => setSaved("idle"), 1600);
  }

  const original = item.kind === "notebook" ? item.uuid ?? item.name : item.title ?? item.name;

  return (
    <form onSubmit={save} className="space-y-3" onBlur={(e) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node)) save();
    }}>
      <div>
        <label className="field-label" htmlFor={`name-${item.id}`}>
          Name
        </label>
        <input
          id={`name-${item.id}`}
          className="field"
          value={name}
          placeholder={displayName({ ...item, display_name: null })}
          onChange={(e) => setName(e.target.value)}
        />
        {item.display_name && original && (
          <p className="faint italic text-[0.8rem] mt-1 break-all">On the device: {original}</p>
        )}
      </div>
      <div>
        <label className="field-label" htmlFor={`shelf-${item.id}`}>
          Shelf
        </label>
        <input
          id={`shelf-${item.id}`}
          className="field"
          value={shelf}
          list={`shelves-${item.id}`}
          placeholder="e.g. Journal, Work, Reading notes"
          onChange={(e) => setShelf(e.target.value)}
        />
        <datalist id={`shelves-${item.id}`}>
          {shelves.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
      </div>
      <div>
        <label className="field-label" htmlFor={`note-${item.id}`}>
          Note
        </label>
        <textarea
          id={`note-${item.id}`}
          className="field"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </div>
      <div className="flex items-center gap-3">
        <button className="btn btn-solid" type="submit" disabled={!dirty || saved === "saving"}>
          Save
        </button>
        <span className="faint italic text-[0.9rem]" aria-live="polite">
          {saved === "saving" ? "Saving…" : saved === "saved" ? "Saved." : dirty ? "Unsaved changes" : ""}
        </span>
      </div>
    </form>
  );
}

function DeleteScreenshot({ item, onRemoved }: { item: Item; onRemoved: (id: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  async function remove() {
    if (!window.confirm(`Delete ${item.filename} from this library AND from the Kindle? This can't be undone.`)) {
      return;
    }
    setBusy(true);
    const r = await engine.deleteScreenshot(item.id);
    setBusy(false);
    if (r.local_deleted) onRemoved(item.id);
    else setMsg(r.errors.join(" ") || "Couldn't delete it.");
  }
  return (
    <div>
      <p className="faint italic text-[0.9rem] mb-3">Deletes the local copy and the one on the Kindle.</p>
      <button className="btn" onClick={remove} disabled={busy}>
        {busy ? "Deleting…" : "Delete screenshot"}
      </button>
      {msg && <p className="mt-2 text-[0.9rem] muted">{msg}</p>}
    </div>
  );
}

function SideSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-7 pt-5 border-t border-[var(--border)]">
      <h3 className="eyebrow !text-[0.68rem] mb-3">{title}</h3>
      {children}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="faint italic">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </>
  );
}
