import { Suspense } from "react";

import { NotificationCentre } from "@/components/pages/notifications";

export const metadata = { title: "Notifications · Stock Grader" };

export default function NotificationsPage() {
  return (
    <Suspense>
      <NotificationCentre />
    </Suspense>
  );
}
