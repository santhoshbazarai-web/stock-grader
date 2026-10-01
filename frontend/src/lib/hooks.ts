"use client";

import { useEffect, useState } from "react";

/** Bumps whenever the `.dark` class on <html> flips, so canvas charts re-read colours. */
export function useThemeVersion(): number {
  const [v, setV] = useState(0);
  useEffect(() => {
    const obs = new MutationObserver(() => setV((x) => x + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);
  return v;
}
