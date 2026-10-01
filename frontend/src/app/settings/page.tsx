import { Suspense } from "react";

import { Settings } from "@/components/pages/settings";

export const metadata = { title: "Settings · Stock Grader" };

export default function SettingsPage() {
  return (
    <Suspense>
      <Settings />
    </Suspense>
  );
}
