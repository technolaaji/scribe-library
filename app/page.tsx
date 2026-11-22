import { PageHeader } from "app/components/ui";
import { Library } from "app/components/library";

export default function Home() {
  return (
    <>
      <PageHeader
        title="The Library"
        intro="Every notebook, book and screenshot from your Scribe. Open one to read it, name it, and put it on a shelf."
      />
      <Library />
    </>
  );
}
