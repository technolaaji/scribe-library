/**
 * App-wide settings. Adding a new section usually means:
 *   1. add a route to the engine (engine/server.py) if it needs new data,
 *   2. add a page under /app,
 *   3. add an entry to `nav` below.
 */
export const site = {
  name: "Scribe Library",
  description: "Your Kindle Scribe notebooks, books and screenshots, kept and read offline.",

  nav: [
    { href: "/", label: "Library" },
    { href: "/shelves", label: "Shelves" },
    { href: "/notebooks", label: "Notebooks" },
    { href: "/books", label: "Books" },
    { href: "/screenshots", label: "Screenshots" },
    { href: "/changes", label: "Changes" },
    { href: "/device", label: "Device" },
  ],
} as const;
