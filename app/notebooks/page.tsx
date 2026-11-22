import type { Metadata } from "next";
import { PageHeader } from "app/components/ui";
import { Library } from "app/components/library";

export const metadata: Metadata = { title: "Notebooks" };

export default function Notebooks() {
  return (
    <>
      <PageHeader
        title="Notebooks"
        intro="Your handwriting. The Scribe names notebooks with bare ids; give them names and shelves here, and they stay yours across syncs."
      />
      <Library kind="notebook" />
    </>
  );
}
