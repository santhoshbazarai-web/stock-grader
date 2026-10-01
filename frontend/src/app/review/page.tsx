import { Suspense } from "react";

import { Review } from "@/components/pages/review";

export const metadata = { title: "Review · Stock Grader" };

export default function ReviewPage() {
  return (
    <Suspense>
      <Review />
    </Suspense>
  );
}
