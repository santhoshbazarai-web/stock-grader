import { Suspense } from "react";

import { ScreenerScan } from "@/components/pages/screener-scan";

export const metadata = { title: "Screener · Stock Grader" };

export default function ScreenerPage() {
  return (
    <Suspense>
      <ScreenerScan />
    </Suspense>
  );
}
