import { Suspense } from "react";

import { Screener } from "@/components/pages/screener";

export const metadata = { title: "Screener · Stock Grader" };

export default function ScreenerPage() {
  return (
    <Suspense>
      <Screener />
    </Suspense>
  );
}
