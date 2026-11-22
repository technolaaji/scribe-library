"use client";

import { useState } from "react";
import { coverUrl, displayName, formatDate, kindLabel, timeAgo, type Item } from "lib/engine";
import { StatusPill } from "app/components/ui";

/* A cover image, or a generated leather binding when there isn't one. */
export function Cover({ item }: { item: Item }) {
  const url = coverUrl(item);
  const [failed, setFailed] = useState(false);
  if (url && !failed) {
    return <img src={url} alt="" loading="lazy" onError={() => setFailed(true)} />;
  }
  const sub =
    item.kind === "book"
      ? (item.authors ?? []).join(", ") || item.format
      : item.kind === "notebook"
        ? "Notebook"
        : "Screenshot";
  return (
    <div className="binding">
      <span className="binding-title">{displayName(item)}</span>
      {sub && <span className="binding-sub">{sub}</span>}
    </div>
  );
}

/* One item on the shelf. */
export function Volume({ item, onOpen }: { item: Item; onOpen: () => void }) {
  const meta =
    item.kind === "book"
      ? [(item.authors ?? [])[0], item.format].filter(Boolean).join(" · ")
      : item.kind === "screenshot"
        ? formatDate(item.captured_at)
        : item.status === "updated"
          ? `Updated ${timeAgo(item.last_changed)}`
          : `Edited ${formatDate(item.modified)}`;

  return (
    <button className="volume" data-kind={item.kind} onClick={onOpen} title={displayName(item)}>
      <div className="volume-cover">
        <Cover item={item} />
        {item.status !== "seen" && (
          <span className="volume-flag">
            <StatusPill status={item.status} />
          </span>
        )}
      </div>
      <div className="min-w-0">
        <div className="volume-title">{displayName(item)}</div>
        <div className="volume-meta">{meta || kindLabel[item.kind]}</div>
      </div>
    </button>
  );
}
