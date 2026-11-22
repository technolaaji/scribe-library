import type { Metadata } from "next";
import { PageHeader } from "app/components/ui";
import { Shelves } from "app/components/shelves";

export const metadata: Metadata = { title: "Shelves" };

export default function ShelvesPage() {
  return (
    <>
      <PageHeader
        title="Shelves"
        intro="Beds for your garden of knowledge. Make a shelf, then plant notebooks and books on it."
      />
      <Shelves />
    </>
  );
}
