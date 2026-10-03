import type { AlertType } from "@/lib/types";

export const ALERT_TYPES: { value: AlertType; label: string; needs?: "price" | "days" }[] = [
  { value: "enters_buy_zone", label: "Price enters the buy zone" },
  { value: "crosses_fv", label: "Price crosses fair value" },
  { value: "crosses_top_band", label: "Price crosses the top band" },
  { value: "crosses_invalidation", label: "Price crosses the invalidation level" },
  { value: "price_above", label: "Price rises above…", needs: "price" },
  { value: "price_below", label: "Price falls below…", needs: "price" },
  { value: "results_date", label: "Results date is near", needs: "days" },
];
