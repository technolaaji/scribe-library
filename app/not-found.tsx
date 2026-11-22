import Link from "next/link";
import { PageHeader } from "app/components/ui";

export default function NotFound() {
  return (
    <PageHeader title="Not on these shelves" intro={<Link className="link" href="/">Back to the library</Link>} />
  );
}
