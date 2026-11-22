import type { Metadata } from "next";
import { PageHeader } from "app/components/ui";
import { ChangeLog } from "app/components/change-log";

export const metadata: Metadata = { title: "Changes" };

export default function Changes() {
  return (
    <>
      <PageHeader
        title="Changes"
        intro="What each sync brought in: new notebooks, pages written since, books added or removed."
      />
      <div className="mx-auto max-w-[44rem]">
        <ChangeLog />
      </div>
    </>
  );
}
