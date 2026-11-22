import type { Metadata } from "next";
import { PageHeader } from "app/components/ui";
import { Device } from "app/components/device";

export const metadata: Metadata = { title: "Device" };

export default function DevicePage() {
  return (
    <>
      <PageHeader title="The Device" intro="Plug in your Scribe and unlock it. Sync pulls everything over USB; nothing leaves this machine." />
      <div className="mx-auto max-w-[44rem]">
        <Device />
      </div>
    </>
  );
}
