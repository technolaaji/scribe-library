/**
 * Typed client for the Python engine (engine/server.py).
 * All paths are relative: in dev, Next proxies them to the engine; in
 * production, the engine serves this UI itself.
 */

export type Kind = "notebook" | "book" | "screenshot";
export type Status = "new" | "updated" | "seen";

export type Item = {
  id: string;
  kind: Kind;
  name: string;
  /** What to show: your name if you gave one, else the device's. */
  label: string;
  display_name: string | null;
  shelf: string | null;
  note: string | null;
  status: Status;
  version: number;
  first_seen: string | null;
  last_changed: string | null;
  last_opened: string | null;
  modified: string;
  size_mb: number;
  // notebooks
  uuid?: string | null;
  has_thumbnail?: boolean;
  // books
  title?: string;
  authors?: string[];
  format?: string;
  extension?: string;
  source?: string;
  has_cover?: boolean;
  // screenshots
  filename?: string;
  captured_at?: string;
};

export type Shelf = {
  id: number;
  name: string;
  description: string | null;
  created_at: string;
  count: number;
  notebooks: number;
  books: number;
  screenshots: number;
};

export type Summary = {
  unseen: number;
  baseline_at: string | null;
  last_change_at: string | null;
};

export type LibraryResponse = { items: Item[]; shelves: Shelf[]; summary: Summary };

export type Change = {
  id: number;
  item_id: string;
  change: "added" | "updated" | "removed" | "restored" | "renamed";
  at: string;
  detail: string | null;
  kind: Kind;
  name: string;
  renamed: number;
};

export type ItemDetail = Item & { history: Change[] };

export type Pages = Item & {
  pages: string[];
  pdf_url?: string;
  viewable?: boolean;
  fallback_pdf?: boolean;
  download_url?: string;
  message?: string;
  error?: string;
};

export type Annotation = {
  type: "highlight" | "note" | "handwritten_note" | "bookmark" | string;
  start: { eid: number; offset: number; pid: number | null; raw: string };
  end: { eid: number; offset: number; pid: number | null; raw: string };
  created: string | null;
  modified: string | null;
  note: string | null;
  canvas_id: string | null;
  text: string | null;
  location: number | null;
  end_location: number | null;
  image: string | null;
  image_url?: string;
  /** position inferred from where you were reading when you wrote the note */
  approximate?: boolean;
};

/** Where an annotation sits in the reader: page and spot (fractions of the page). */
export type AnnotationPlace = {
  page: number | null;
  exact: boolean;
  x: number | null;
  y: number | null;
  left: number | null;
  end_page: number | null;
  end_y: number | null;
  /** highlights: one [page, x0, y0, x1, y1] per highlighted line */
  rects: number[][];
  /** notes: index of the highlight the note was written inside */
  highlight: number | null;
};

export type Annotations = {
  title: string | null;
  asin: string | null;
  yjr: string | null;
  nbk: string | null;
  annotations: Annotation[];
  counts: Record<string, number>;
  warnings: string[];
  error?: string;
};

export type Status_ = {
  device_connected: boolean;
  device_name: string | null;
  library_root: string;
  registry_path: string | null;
  notebook_count: number;
  book_count: number;
  screenshot_count: number;
  in_book_notebook_count: number;
  converter_ready: boolean;
  converter_message: string;
  book_reader_ready: boolean;
  book_reader_message: string;
  cache_size_mb: number;
};

export type SyncResult = {
  ok: boolean;
  kind: "notebooks" | "books" | "screenshots";
  device: string;
  found: number;
  copied: number;
  skipped: number;
  errors: string[];
  log: string[];
  changes?: { added: number; updated: number; removed: number; restored: number };
  error?: string;
};

async function json<T>(res: Response): Promise<T> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok && !(body && typeof body === "object")) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return body as T;
}

async function get<T>(url: string): Promise<T> {
  return json<T>(await fetch(url, { cache: "no-store" }));
}

async function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  return json<T>(
    await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    }),
  );
}

export const engine = {
  library: () => get<LibraryResponse>("/api/library"),
  status: () => get<Status_>("/api/status"),
  changes: (limit = 300) => get<{ changes: Change[]; summary: Summary }>(`/api/changes?limit=${limit}`),
  item: (id: string) => get<ItemDetail>(`/api/items/${id}`),
  update: (id: string, fields: Partial<Pick<Item, "display_name" | "shelf" | "note">>) =>
    send<ItemDetail>(`/api/items/${id}`, "PATCH", fields),
  opened: (id: string) => send<{ ok: boolean }>(`/api/items/${id}/opened`, "POST"),
  markAllSeen: () => send<{ ok: boolean }>("/api/items/mark_all_seen", "POST"),
  pages: (item: Pick<Item, "id" | "kind">) =>
    get<Pages>(item.kind === "notebook" ? `/api/notebook/${item.id}` : `/api/book/${item.id}`),
  annotations: (id: string) => get<Annotations>(`/api/book/${id}/annotations`),
  /** reader page (0-based) of each annotation, in the same order */
  annotationPages: (id: string) =>
    get<{ pages: AnnotationPlace[]; ready: boolean }>(
      `/api/book/${id}/annotation_pages`,
    ),
  sync: (what: "notebooks" | "books" | "screenshots") =>
    send<SyncResult>(`/api/sync_${what}`, "POST"),
  shelves: () => get<{ shelves: Shelf[] }>("/api/shelves"),
  createShelf: (name: string, description?: string) =>
    send<{ shelf?: Shelf; error?: string }>("/api/shelves", "POST", { name, description }),
  updateShelf: (id: number, fields: { name?: string; description?: string }) =>
    send<{ shelf?: Shelf; error?: string }>(`/api/shelves/${id}`, "PATCH", fields),
  deleteShelf: (id: number) => send<{ ok: boolean }>(`/api/shelves/${id}`, "DELETE"),
  shelveItems: (id: number, change: { add?: string[]; remove?: string[] }) =>
    send<{ shelf?: Shelf; error?: string }>(`/api/shelves/${id}/items`, "POST", change),
  deleteScreenshot: (id: string) =>
    send<{ ok: boolean; local_deleted: boolean; device_deleted: boolean; errors: string[] }>(
      `/api/screenshot/${id}/delete`,
      "POST",
    ),
  clearCache: () => send<{ ok: boolean }>("/api/clear_cache", "POST"),
  upload: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return json<{ ok: boolean; target?: string; bytes?: number; errors?: string[]; error?: string; log?: string[] }>(
      await fetch("/api/upload", { method: "POST", body: fd }),
    );
  },
};

/** Where an item's cover image lives, or null for a generated cover. */
export function coverUrl(item: Item): string | null {
  if (item.kind === "screenshot") return `/screenshot/${item.id}`;
  if (item.kind === "notebook") return item.has_thumbnail ? `/thumbnail/${item.id}` : null;
  // The engine extracts a book's cover from its metadata on first request
  // (404 → the generated binding is shown instead).
  return `/cover/${item.id}`;
}

export const kindLabel: Record<Kind, string> = {
  notebook: "Notebook",
  book: "Book",
  screenshot: "Screenshot",
};

/** The name to show, with notebooks' bare UUIDs made readable. */
export function displayName(item: Item): string {
  if (item.display_name) return item.display_name;
  if (item.kind === "notebook" && item.uuid) return `Untitled ${item.uuid.slice(0, 8)}`;
  return item.label || item.name;
}

export function formatDate(iso: string | null | undefined, style: "short" | "long" = "short") {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: style === "long" ? "long" : "short",
    year: "numeric",
  });
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return formatDate(iso);
}

export function toRoman(n: number): string {
  const map: [number, string][] = [
    [1000, "M"], [900, "CM"], [500, "D"], [400, "CD"], [100, "C"], [90, "XC"],
    [50, "L"], [40, "XL"], [10, "X"], [9, "IX"], [5, "V"], [4, "IV"], [1, "I"],
  ];
  let out = "";
  for (const [v, s] of map) while (n >= v) { out += s; n -= v; }
  return out;
}
