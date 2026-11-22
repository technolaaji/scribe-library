"use client";

import { useEffect, useRef, useState } from "react";
import { engine, type Status_, type SyncResult } from "lib/engine";
import { Loading, Section } from "app/components/ui";

type What = "notebooks" | "books" | "screenshots";

/* Device status, sync, push-to-Kindle and housekeeping. */
export function Device() {
  const [status, setStatus] = useState<Status_ | null>(null);
  const [busy, setBusy] = useState<What | "all" | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [summary, setSummary] = useState<string | null>(null);

  async function refresh() {
    try {
      setStatus(await engine.status());
    } catch {
      setStatus(null);
    }
  }
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, []);

  async function sync(what: What | "all") {
    setBusy(what);
    setLog([]);
    setSummary(null);
    const order: What[] = what === "all" ? ["notebooks", "books", "screenshots"] : [what];
    const totals = { added: 0, updated: 0, removed: 0, restored: 0 };
    const parts: string[] = [];
    for (const w of order) {
      setLog((l) => [...l, `— ${w} —`]);
      let r: SyncResult;
      try {
        r = await engine.sync(w);
      } catch (e) {
        setLog((l) => [...l, `✗ ${e}`]);
        continue;
      }
      setLog((l) => [...l, ...(r.log ?? []), ...(r.error ? [`✗ ${r.error}`] : [])]);
      if (r.changes) for (const k of Object.keys(totals) as (keyof typeof totals)[]) totals[k] += r.changes[k] ?? 0;
      parts.push(`${r.copied ?? 0} of ${r.found ?? 0} ${w} copied`);
    }
    const changed = Object.entries(totals)
      .filter(([, n]) => n)
      .map(([k, n]) => `${n} ${k}`)
      .join(", ");
    setSummary(`${parts.join(" · ")}${changed ? ` — ${changed}` : " — nothing changed"}.`);
    setBusy(null);
    refresh();
  }

  if (!status) return <Loading>Listening for the engine</Loading>;

  return (
    <div>
      <div className="card text-center !py-6">
        <p className="eyebrow">{status.device_connected ? "Connected" : "Not connected"}</p>
        <p className="mt-1 text-[1.4rem]">{status.device_name ?? "No Kindle on USB"}</p>
        {!status.device_connected && (
          <p className="mt-1 muted italic text-[0.95rem]">Plug it in, unlock the screen, and use a data cable.</p>
        )}
        <p className="mt-3 faint italic text-[0.95rem]">
          {status.notebook_count} notebooks · {status.in_book_notebook_count} books with handwriting ·{" "}
          {status.book_count} books · {status.screenshot_count} screenshots
        </p>
      </div>

      <Section label="Sync">
        <p className="muted mb-4">
          Copies anything new or changed on the device. Changes are compared by size and the device&apos;s
          modification time, and recorded in the{" "}
          <a className="link" href="/changes">
            ledger
          </a>
          .
        </p>
        <div className="flex flex-wrap gap-2">
          <button className="btn btn-solid" onClick={() => sync("all")} disabled={!!busy || !status.device_connected}>
            {busy === "all" ? "Syncing…" : "Sync everything"}
          </button>
          {(["notebooks", "books", "screenshots"] as const).map((w) => (
            <button key={w} className="btn" onClick={() => sync(w)} disabled={!!busy || !status.device_connected}>
              {busy === w ? "Syncing…" : w}
            </button>
          ))}
        </div>
        {summary && <p className="mt-4 italic">{summary}</p>}
        {log.length > 0 && <pre className="console mt-4">{log.join("\n")}</pre>}
      </Section>

      <Section label="Send to Kindle">
        <Upload disabled={!status.device_connected} />
      </Section>

      <Section label="Housekeeping">
        <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-[0.95rem] mb-5">
          <dt className="faint italic">Library</dt>
          <dd className="font-mono text-[0.75rem] break-all self-center">{status.library_root}</dd>
          <dt className="faint italic">Registry</dt>
          <dd className="font-mono text-[0.75rem] break-all self-center">{status.registry_path}</dd>
          <dt className="faint italic">Render cache</dt>
          <dd>{status.cache_size_mb} MB</dd>
        </dl>
        {!status.converter_ready && <p className="muted italic mb-3">{status.converter_message}</p>}
        {!status.book_reader_ready && <p className="muted italic mb-3">{status.book_reader_message}</p>}
        <button
          className="btn"
          onClick={async () => {
            await engine.clearCache();
            refresh();
          }}
        >
          Clear render cache
        </button>
      </Section>
    </div>
  );
}

function Upload({ disabled }: { disabled: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<string[]>([]);

  async function push() {
    if (!file) return;
    setBusy(true);
    setOut(["Sending…"]);
    try {
      const r = await engine.upload(file);
      setOut([
        ...(r.log ?? []),
        ...(r.ok ? [`✓ On the Kindle at ${r.target}. It may take a minute to appear on the home screen.`] : []),
        ...(r.errors ?? []).map((e) => `✗ ${e}`),
        ...(r.error ? [`✗ ${r.error}`] : []),
      ]);
    } catch (e) {
      setOut([`✗ ${e}`]);
    }
    setBusy(false);
  }

  return (
    <div>
      <div
        className="dropzone"
        data-over={over}
        onClick={() => input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          setFile(e.dataTransfer.files[0] ?? null);
        }}
      >
        {file ? file.name : "Drop a PDF, EPUB, MOBI, TXT, DOCX or RTF here, or click to choose"}
        <input
          ref={input}
          type="file"
          hidden
          accept=".pdf,.epub,.mobi,.txt,.docx,.rtf"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </div>
      <div className="mt-3 flex gap-2">
        <button className="btn btn-solid" onClick={push} disabled={!file || busy || disabled}>
          {busy ? "Sending…" : "Send to Kindle"}
        </button>
        {file && (
          <button className="btn btn-quiet" onClick={() => setFile(null)} disabled={busy}>
            Clear
          </button>
        )}
      </div>
      {out.length > 0 && <pre className="console mt-3">{out.join("\n")}</pre>}
    </div>
  );
}
