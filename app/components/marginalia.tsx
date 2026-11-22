"use client";

import { useEffect, useState } from "react";
import { engine, formatDate, type Annotations, type AnnotationPlace } from "lib/engine";
import type { Mark } from "app/components/reader";

const label: Record<string, string> = {
  highlight: "Highlight",
  note: "Note",
  handwritten_note: "Handwritten note",
  bookmark: "Bookmark",
};

/**
 * Highlights, typed notes and handwritten notes made inside a book, in
 * reading order, each with its Kindle location.
 */
export function Marginalia({
  id,
  readerReady = false,
  onJump,
  onMarks,
}: {
  id: string;
  /** the reader has rendered the book, so pages can be worked out */
  readerReady?: boolean;
  onJump?: (page: number, index: number) => void;
  onMarks?: (marks: Mark[]) => void;
}) {
  const [data, setData] = useState<Annotations | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pages, setPages] = useState<(AnnotationPlace | undefined)[]>([]);

  useEffect(() => {
    if (!readerReady || !data?.annotations?.length) return;
    let cancelled = false;
    engine
      .annotationPages(id)
      .then((r) => {
        if (cancelled || !r.ready) return;
        setPages(r.pages);
        onMarks?.(
          r.pages.flatMap((p, i) => {
            const a = data.annotations[i];
            if (p?.page == null || a.type === "bookmark") return [];
            return [
              {
                index: i,
                page: p.page,
                kind: a.type,
                x: p.x,
                y: p.y,
                left: p.left,
                endPage: p.end_page,
                endY: p.end_y,
                image: a.image_url ?? null,
                location: a.location,
                approximate: a.approximate,
                text: a.text,
                rects: p.rects,
                highlight: p.highlight,
              },
            ];
          }),
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [id, readerReady, data]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancelled = false;
    engine
      .annotations(id)
      .then((d) => {
        if (cancelled) return;
        if (d.error) setError(d.error);
        setData(d);
      })
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (error && !data?.annotations?.length) {
    return <p className="faint italic text-[0.95rem]">Couldn&apos;t read annotations: {error}</p>;
  }
  if (!data) return <p className="loading-line text-[0.95rem]">Reading the margins</p>;

  const list = data.annotations
    .map((a, i) => ({ a, i, at: pages[i] }))
    .filter(({ a }) => a.type !== "bookmark");
  const counts = Object.entries(data.counts ?? {})
    .filter(([k]) => k !== "bookmark")
    .map(([k, n]) => `${n} ${(label[k] ?? k).toLowerCase()}${n === 1 ? "" : "s"}`)
    .join(" · ");

  return (
    <div>
      {list.length === 0 ? (
        <p className="faint italic text-[0.95rem]">
          {data.yjr ? "No highlights or notes in this book." : data.warnings[0] ?? "No annotations found."}
        </p>
      ) : (
        <>
          <div className="mb-4 flex items-baseline justify-between gap-3">
            <span className="faint italic text-[0.9rem]">{counts}</span>
            <a className="text-link text-[0.9rem]" href={`/api/book/${id}/annotations.md`}>
              Export .md
            </a>
          </div>
          {list.map(({ a, i, at }) => (
            <div
              key={`${a.start.raw}-${i}`}
              className="marginalia"
              data-jump={at?.page != null ? "true" : undefined}
              onClick={() => at?.page != null && onJump?.(at.page, i)}
              title={at?.page != null ? `Turn to page ${at.page + 1}` : undefined}
            >
              <div className="field-label !mb-1 !flex flex-wrap gap-x-3">
                <span className="flex-1">
                {label[a.type] ?? a.type}
                {a.type === "handwritten_note" && at?.highlight != null ? " on a highlight" : ""}
                {a.type === "highlight" && pages.some((q, k) => q?.highlight === i && data.annotations[k].type === "handwritten_note") ? " · ✎ note" : ""}
                {a.location ? ` · ${a.approximate ? "≈ " : ""}Loc ${a.location}` : " · position not stored"}
                {a.end_location && a.end_location !== a.location ? `–${a.end_location}` : ""}
                </span>
                {at?.page != null && (
                  <span className="marginalia-go">
                    {at.exact ? "" : "≈ "}p. {at.page + 1} →
                  </span>
                )}
              </div>
              {a.approximate && (
                <div className="faint italic text-[0.8rem] -mt-0.5 mb-1">where you were reading when you wrote it</div>
              )}
              {a.text && <blockquote>{a.type === "highlight" ? a.text : `…${a.text.slice(0, 140)}…`}</blockquote>}
              {a.note && <p className="mt-1 text-[0.98rem]">{a.note}</p>}
              {a.image_url && <img src={a.image_url} alt={`Handwritten note at location ${a.location}`} />}
              {a.created && <div className="faint italic text-[0.8rem] mt-1">{formatDate(a.created)}</div>}
            </div>
          ))}
        </>
      )}
      {data.warnings.length > 0 && list.length > 0 && (
        <ul className="mt-4 faint italic text-[0.85rem] list-none">
          {data.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
