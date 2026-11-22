import type { Metadata } from "next";
import { PageHeader } from "app/components/ui";
import { Library } from "app/components/library";

export const metadata: Metadata = { title: "Books" };

export default function Books() {
  return (
    <>
      <PageHeader
        title="Books"
        intro="Purchased and sideloaded, with the highlights and handwritten notes you left in their margins."
      />
      <Library kind="book" />
    </>
  );
}
