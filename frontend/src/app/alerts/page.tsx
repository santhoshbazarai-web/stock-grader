import { Suspense } from "react";

import { AlertsPage } from "@/components/pages/alerts";

export const metadata = { title: "Alerts · Stock Grader" };

export default function Page() {
  return (
    <Suspense>
      <AlertsPage />
    </Suspense>
  );
}
