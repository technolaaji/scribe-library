"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { engine, type Item, type Pages } from "lib/engine";
import { MarginBranch } from "app/components/ornaments";

type Zoom = "fit" | "width" | "large";

/**
 * Page-by-page reader for notebooks and books (rendered by the engine to one
 * JPEG per page), a lightbox for screenshots, and an iframe/download fallback
 * for formats that can't be paged.
 */
/** An annotation placed in the reader; x/y/left are fractions of the page. */
export type Mark = {
  index: number;
  page: number;
  kind: string;
  x?: number | null;
  y?: number | null;
  left?: number | null;
  endPage?: number | null;
  endY?: number | null;
  image?: string | null;
  location?: number | null;
  approximate?: boolean;
  text?: string | null;
  /** highlights: one [page, x0, y0, x1, y1] per highlighted line */
  rects?: number[][];
  /** notes: index of the highlight the note was written inside */
  highlight?: number | null;
};

export function Reader({
  item,
  active,
  target,
  marks = [],
  onReady,
}: {
  item: Item;
  active: boolean;
  /** page to turn to (and annotation to show); `n` changes on every request */
  target?: { page: number; n: number; index?: number } | null;
  /** pages that carry highlights or notes */
  marks?: Mark[];
  onReady?: () => void;
}) {
  const [doc, setDoc] = useState<Pages | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [spread, setSpread] = useState(false);
  const [zoom, setZoom] = useState<Zoom>("fit");
  const [full, setFull] = useState(false);
  const stripRef = useRef<HTMLDivElement>(null);
  // the annotation whose marker is open on the page
  const [focus, setFocus] = useState<{ index: number; n: number } | null>(null);

  useEffect(() => {
    if (item.kind === "screenshot") return;
    let cancelled = false;
    setDoc(null);
    setError(null);
    setPage(0);
    engine
      .pages(item)
      .then((d) => {
        if (cancelled) return;
        if (d.error) setError(d.error);
        setDoc(d);
      })
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [item.id, item.kind, item.version]); // eslint-disable-line react-hooks/exhaustive-deps

  const count = doc?.pages?.length ?? 0;
  const step = spread ? 2 : 1;
  const go = useCallback(
    (to: number) => setPage(Math.max(0, Math.min(Math.max(0, count - 1), to))),
    [count],
  );

  useEffect(() => {
    if (count) onReady?.();
  }, [count]); // eslint-disable-line react-hooks/exhaustive-deps

  // turn to a page asked for from outside (a note in the side panel)
  useEffect(() => {
    if (!target || !count) return;
    go(spread ? target.page - (target.page % 2) : target.page);
    if (target.index != null) setFocus({ index: target.index, n: target.n });
  }, [target?.n, count]); // eslint-disable-line react-hooks/exhaustive-deps

  const notePages = [...new Set(marks.filter((m) => m.kind === "handwritten_note").map((m) => m.page))].sort((a, b) => a - b);
  const markedPages = new Set(marks.map((m) => m.page));
  const nextNotePage = notePages.find((p) => p > page + (spread ? 1 : 0)) ?? notePages[0];
  const goNextNote = useCallback(() => {
    if (nextNotePage == null) return;
    go(nextNotePage);
    const m = marks.find((x) => x.kind === "handwritten_note" && x.page === nextNotePage);
    if (m) setFocus((f) => ({ index: m.index, n: (f?.n ?? 0) + 1 }));
  }, [nextNotePage, marks, go]);

  // keyboard: arrows turn pages, f toggles full screen, esc leaves it
  useEffect(() => {
    if (!active) return;
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select")) return;
      if (e.key === "ArrowRight" || e.key === "PageDown") go(page + step);
      else if (e.key === "ArrowLeft" || e.key === "PageUp") go(page - step);
      else if (e.key === "f") setFull((v) => !v);
      else if (e.key === "n" && notePages.length) goNextNote();
      else if (e.key === "Escape" && full) {
        e.stopImmediatePropagation();
        setFull(false);
      } else return;
      e.preventDefault();
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [active, go, page, step, full, notePages.length, goNextNote]);

  useEffect(() => {
    document.body.style.overflow = full ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [full]);

  useEffect(() => {
    stripRef.current
      ?.querySelector<HTMLElement>('[aria-current="true"]')
      ?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
  }, [page]);

  if (item.kind === "screenshot") {
    return (
      <div className="reader" data-full={full}>
        <Bar>
          <span className="faint italic text-[0.95rem]">{item.filename}</span>
          <span className="ml-auto" />
          <button className="btn btn-icon" onClick={() => setFull((v) => !v)} title="Full screen (f)">
            {full ? "⤡" : "⤢"}
          </button>
        </Bar>
        <div className="reader-stage" data-zoom="fit">
          <img className="page-sheet" src={`/screenshot/${item.id}`} alt={item.filename ?? ""} />
        </div>
      </div>
    );
  }

  if (error && !count) {
    return (
      <div className="reader">
        <div className="p-6">
          <p className="eyebrow mb-3">Couldn&apos;t open this</p>
          <pre className="console">{error}</pre>
        </div>
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="reader">
        <div className="reader-stage items-center">
          <p className="loading-line self-center">
            {item.kind === "notebook" ? "Inking the pages" : "Binding the pages"}
          </p>
        </div>
      </div>
    );
  }

  if (doc.viewable === false) {
    return (
      <div className="reader">
        <div className="p-8 text-center">
          <p className="muted italic mb-5">{doc.message}</p>
          {doc.download_url && (
            <a className="btn" href={doc.download_url}>
              Download the file
            </a>
          )}
        </div>
      </div>
    );
  }

  if (doc.fallback_pdf && doc.pdf_url) {
    return (
      <div className="reader" data-full={full}>
        <Bar>
          <span className="faint italic text-[0.95rem]">Shown in the browser&apos;s PDF viewer</span>
          <span className="ml-auto" />
          <button className="btn btn-icon" onClick={() => setFull((v) => !v)} title="Full screen (f)">
            {full ? "⤡" : "⤢"}
          </button>
        </Bar>
        <iframe src={doc.pdf_url} className="reader-stage !p-0 w-full" title={item.label} />
      </div>
    );
  }

  const shown = spread ? [page, page + 1].filter((p) => p < count) : [page];
  const sheetStyle: React.CSSProperties =
    zoom === "fit"
      ? { maxHeight: "100%", maxWidth: spread ? "calc(50% - 0.5rem)" : "100%" }
      : zoom === "width"
        ? { width: spread ? "calc(50% - 0.5rem)" : "100%", maxHeight: "none" }
        : { width: spread ? "85%" : "150%", maxWidth: "none", maxHeight: "none" };

  return (
    <div className="reader" data-full={full}>
      <Bar>
        <button className="btn btn-icon" onClick={() => go(page - step)} disabled={page === 0} title="Previous (←)">
          ←
        </button>
        <span className="text-[0.95rem] muted tabular-nums min-w-[6.5rem] text-center">
          {count ? (
            <>
              {shown.map((p) => p + 1).join("–")} <span className="faint italic">of {count}</span>
            </>
          ) : (
            <span className="faint italic">no pages</span>
          )}
        </span>
        <button
          className="btn btn-icon"
          onClick={() => go(page + step)}
          disabled={page + step >= count}
          title="Next (→)"
        >
          →
        </button>
        <span className="mx-1 faint" aria-hidden>
          ·
        </span>
        {(["fit", "width", "large"] as const).map((z) => (
          <button key={z} className="chip !text-[0.9rem]" aria-pressed={zoom === z} onClick={() => setZoom(z)}>
            {z === "fit" ? "Page" : z === "width" ? "Width" : "Close"}
          </button>
        ))}
        <span className="mx-1 faint" aria-hidden>
          ·
        </span>
        <button className="chip !text-[0.9rem]" aria-pressed={spread} onClick={() => setSpread((v) => !v)}>
          Spread
        </button>
        <span className="ml-auto" />
        {notePages.length > 0 && (
          <button
            className="btn btn-quiet"
            onClick={goNextNote}
            title={`${notePages.length} page${notePages.length === 1 ? "" : "s"} with handwritten notes (n)`}
          >
            ✎ Next note
          </button>
        )}
        {doc.pdf_url && (
          <a className="btn btn-quiet" href={doc.pdf_url} target="_blank" rel="noreferrer">
            PDF
          </a>
        )}
        <button className="btn btn-icon" onClick={() => setFull((v) => !v)} title="Full screen (f)">
          {full ? "⤡" : "⤢"}
        </button>
      </Bar>

      <div className="reader-stage" data-zoom={zoom}>
        {shown.map((p) => (
          <PageSheet
            key={doc.pages[p]}
            src={doc.pages[p]}
            page={p}
            style={sheetStyle}
            marks={marks.filter(
              (m) =>
                m.y != null &&
                (m.page === p ||
                  m.rects?.some((r) => r[0] === p) ||
                  (m.endPage != null && m.page < p && m.endPage >= p)),
            )}
            focus={focus}
            onTurn={(forward) => go(forward ? page + step : page - step)}
          />
        ))}
      </div>

      {count > 1 && (
        <div className="page-strip" ref={stripRef}>
          {doc.pages.map((src, i) => (
            <span key={src} className="page-thumb-wrap" data-mark={markedPages.has(i) ? (notePages.includes(i) ? "note" : "highlight") : undefined}>
              <img
                src={src}
                alt=""
                loading="lazy"
                className="page-thumb"
                aria-current={shown.includes(i)}
                onClick={() => go(spread ? i - (i % 2) : i)}
              />
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * One page, with its annotations drawn on it: highlighted lines shaded the way
 * the Kindle shades them, and a flowering branch in the margin for each
 * handwritten note (click it to see the note). A note written inside a
 * highlight blooms beside the start of that highlighted passage. The overlay
 * is measured from the rendered image so it stays aligned at every zoom.
 */
function PageSheet({
  src,
  page,
  style,
  marks,
  focus,
  onTurn,
}: {
  src: string;
  page: number;
  style: React.CSSProperties;
  marks: Mark[];
  focus: { index: number; n: number } | null;
  onTurn: (forward: boolean) => void;
}) {
  const img = useRef<HTMLImageElement>(null);
  const [box, setBox] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const markerRefs = useRef(new Map<number, HTMLElement>());

  const measure = useCallback(() => {
    const el = img.current;
    if (!el || !el.complete || !el.naturalWidth) return;
    setBox({ left: el.offsetLeft, top: el.offsetTop, width: el.offsetWidth, height: el.offsetHeight });
  }, []);

  useLayoutEffect(() => {
    measure();
    const el = img.current;
    if (!el) return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (el.parentElement) ro.observe(el.parentElement);
    return () => ro.disconnect();
  }, [measure, style]);

  // a note picked in the side panel (or by Next note): open it and bring it into view
  useEffect(() => {
    if (!focus || !marks.some((m) => m.index === focus.index && m.page === page)) return;
    setOpen(focus.index);
    const t = setTimeout(() => {
      markerRefs.current.get(focus.index)?.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 120);
    return () => clearTimeout(t);
  }, [focus?.n, box !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  const textLeft = (m: Mark) => Math.max(0.06, Math.min(m.left ?? 0.15, m.x ?? 1));
  const byIndex = new Map(marks.map((m) => [m.index, m]));
  // highlights that carry a note, and which note is open/hovered over them
  const noted = new Map<number, number>();
  for (const m of marks) if (m.kind === "handwritten_note" && m.highlight != null) noted.set(m.highlight, m.index);
  const pad = 0.003;

  return (
    <>
      <img
        ref={img}
        className="page-sheet"
        src={src}
        alt={`Page ${page + 1}`}
        style={style}
        onLoad={measure}
        onClick={(e) => {
          const r = (e.target as HTMLElement).getBoundingClientRect();
          onTurn(e.clientX - r.left >= r.width / 3);
        }}
      />
      {box && marks.length > 0 && (
        <div className="page-marks" style={box}>
          {marks.map((m) => {
            const left = textLeft(m);
            if (m.kind === "highlight" && m.rects?.length) {
              const note = noted.get(m.index);
              const lit = note != null && (open === note || hover === note);
              return m.rects
                .filter((r) => r[0] === page)
                .map(([, x0, y0, x1, y1], k) => (
                  <span
                    key={`${m.index}-${k}`}
                    className="page-hl"
                    data-noted={note != null ? "true" : undefined}
                    data-lit={lit ? "true" : undefined}
                    title={m.text ? `Highlight${m.location ? ` · Loc ${m.location}` : ""}` : undefined}
                    style={{
                      left: `${(x0 - pad) * 100}%`,
                      top: `${(y0 - pad) * 100}%`,
                      width: `${(x1 - x0 + pad * 2) * 100}%`,
                      height: `${(y1 - y0 + pad * 2) * 100}%`,
                    }}
                  />
                ));
            }
            if (m.kind === "highlight") {
              // no line boxes (text not matched exactly): a vine in the margin instead
              const top = m.page === page ? m.y! : 0;
              const bottom = m.endPage === page && m.endY != null ? m.endY : m.endPage != null && m.endPage > page ? 0.97 : top + 0.03;
              return (
                <span
                  key={m.index}
                  className="page-vine"
                  title={m.text ? `Highlight · ${m.text.slice(0, 120)}…` : "Highlight"}
                  style={{
                    left: `${(left - 0.025) * 100}%`,
                    top: `${top * 100}%`,
                    height: `${Math.max(0.02, bottom - top) * 100}%`,
                  }}
                  data-start={m.page === page ? "true" : undefined}
                />
              );
            }
            if (m.kind !== "handwritten_note" || m.page !== page) return null;
            // written inside a highlight: bloom beside the start of the highlighted lines on this page
            const hl = m.highlight != null ? byIndex.get(m.highlight) : undefined;
            const firstLine = hl?.rects?.find((r) => r[0] === page);
            const y = firstLine ? firstLine[2] : m.y!;
            const width = Math.min(0.17, left - 0.004);
            return (
              <div key={m.index} style={{ position: "absolute", top: `${y * 100}%`, left: 0, right: 0 }}>
                <button
                  ref={(el) => {
                    if (el) markerRefs.current.set(m.index, el);
                  }}
                  className="page-branch"
                  data-open={open === m.index}
                  style={{ left: `${(left - width - 0.004) * 100}%`, width: `${width * 100}%` }}
                  onClick={() => setOpen((o) => (o === m.index ? null : m.index))}
                  onMouseEnter={() => setHover(m.index)}
                  onMouseLeave={() => setHover((h) => (h === m.index ? null : h))}
                  title={`Handwritten note${hl ? " on a highlight" : ""}${m.location ? ` · ${m.approximate ? "≈ " : ""}Loc ${m.location}` : ""}`}
                  aria-label="Show handwritten note"
                  aria-expanded={open === m.index}
                >
                  <MarginBranch />
                </button>
                {open === m.index && (
                  <div className="page-note" style={{ left: `${left * 100}%` }}>
                    <div className="page-note-head">
                      <span>
                        Handwritten note{hl ? " on a highlight" : ""}
                        {m.location ? ` · ${m.approximate ? "≈ " : ""}Loc ${m.location}` : ""}
                      </span>
                      <button onClick={() => setOpen(null)} aria-label="Close note">
                        ✕
                      </button>
                    </div>
                    {m.image && <img src={m.image} alt="Your handwritten note" />}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

function Bar({ children }: { children: React.ReactNode }) {
  return <div className="reader-bar">{children}</div>;
}
