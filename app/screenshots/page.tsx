import type { Metadata } from "next";
import { PageHeader } from "app/components/ui";
import { Library } from "app/components/library";

export const metadata: Metadata = { title: "Screenshots" };

export default function Screenshots() {
  return (
    <>
      <PageHeader title="Screenshots" intro="Captured on the device. Deleting one here deletes it from the Kindle too." />
      <Library kind="screenshot" />
    </>
  );
}
